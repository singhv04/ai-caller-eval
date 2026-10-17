"""LLM-as-judge core: prompt, combined schema, provider registry and resolution.

All judge-based metrics selected for a run are folded into ONE structured
request per call: each metric contributes an instruction block and a JSON
schema section, and the response is validated against the combined schema.
That keeps cost at one LLM call per recording regardless of how many dialogue
metrics are selected.

The model behind the judge is a plugin. Built-in providers (see providers.py):
Anthropic Claude, OpenAI, Google Gemini, and any OpenAI-compatible endpoint
(OpenRouter, Groq, Together, Azure OpenAI, Ollama, vLLM, …). Add another with
`@register_provider` — it only has to implement `complete_json`.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..config import Profile, get_settings
from ..metrics.base import JudgeSection


class JudgeUnavailable(RuntimeError):
    """No usable judge (not configured, missing package, bad credentials)."""


SYSTEM_PROMPT = """You are a meticulous quality evaluator for AI voice agents. You review the transcript of one \
phone call between a HUMAN caller and an AI voice agent and score it against a set of metric rubrics.

The transcript comes from separate audio channels, so speaker attribution is reliable. Transcription can contain \
ASR noise; do not penalise the AI for obvious transcription artefacts. Turns are referenced by their index, e.g. T7.

Be evidence-based: every finding must cite the turn index where it occurs. When a rubric does not apply to this \
call (e.g. no escalation was ever needed), return empty lists rather than inventing cases. Judge only what is in \
the transcript and the provided business context. Respond with JSON only."""


def build_user_prompt(transcript_text: str, profile: Profile, sections: dict[str, JudgeSection], call_facts: str) -> str:
    ctx = profile.data
    agent = ctx.get("agent", {})
    policies = ctx.get("policies", {})
    parts = [
        "<business_context>",
        f"Agent role: {agent.get('role', 'AI voice agent')}",
        f"Agent goal: {agent.get('goal', 'Resolve the caller request')}",
    ]
    if agent.get("instructions"):
        parts.append(f"Agent operating instructions:\n{agent['instructions']}")
    if policies:
        parts.append("Policies:\n" + json.dumps(policies, indent=2))
    if ctx.get("knowledge_base"):
        parts.append(f"Knowledge base (authoritative ground truth):\n{ctx['knowledge_base']}")
    parts.append("</business_context>")
    if call_facts:
        parts += ["<call_metadata>", call_facts, "</call_metadata>"]
    parts += ["<transcript>", transcript_text, "</transcript>", "<rubrics>"]
    for mid, sec in sections.items():
        parts.append(f"## {mid}\n{sec.instructions.strip()}")
    parts.append("</rubrics>")
    parts.append("Return a JSON object with one key per rubric above, following the provided schema.")
    return "\n".join(parts)


def combined_schema(sections: dict[str, JudgeSection]) -> dict:
    return {
        "type": "object",
        "properties": {mid: sec.schema for mid, sec in sections.items()},
        "required": list(sections),
        "additionalProperties": False,
    }


def parse_json_output(text: str) -> dict:
    """Parse model JSON output; tolerate markdown fences from providers without strict JSON mode."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{"):
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            t = t[start: end + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Judge returned invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError("Judge returned JSON that is not an object")
    return data


# ---------------------------------------------------------------------------
# Provider plugin interface
# ---------------------------------------------------------------------------

@dataclass
class JudgeConfig:
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    effort: str | None = None
    key_source: str = ""          # "settings" | "env:NAME" | "profile" | ""


class BaseJudge:
    """Subclass, set the class attributes, implement `complete_json`, decorate with @register_provider."""

    name: ClassVar[str]
    label: ClassVar[str]
    key_envs: ClassVar[tuple[str, ...]] = ()
    default_model: ClassVar[str] = ""
    package: ClassVar[str] = ""           # importable module the provider needs
    pip_name: ClassVar[str] = ""
    needs_base_url: ClassVar[bool] = False
    key_optional: ClassVar[bool] = False
    supports_effort: ClassVar[bool] = False
    effort_levels: ClassVar[tuple[str, ...]] = ()
    model_hint: ClassVar[str] = ""
    docs_url: ClassVar[str] = ""

    def __init__(self, cfg: JudgeConfig):
        self.cfg = cfg
        self.model = cfg.model

    # -- to implement --------------------------------------------------------
    def complete_json(self, system: str, user: str, schema: dict, schema_name: str = "evaluation") -> tuple[str, dict]:
        """Return (json_text, usage_meta). Raise JudgeUnavailable on auth/config errors."""
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []

    # -- shared --------------------------------------------------------------
    def evaluate(self, transcript_text: str, profile: Profile, sections: dict[str, JudgeSection],
                 call_facts: str) -> dict:
        prompt = build_user_prompt(transcript_text, profile, sections, call_facts)
        text, usage = self.complete_json(SYSTEM_PROMPT, prompt, combined_schema(sections))
        data = parse_json_output(text)
        data["_meta"] = {"provider": self.name, "model": self.model, **usage}
        return data

    def ping(self) -> dict:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "model": {"type": "string"}},
                  "required": ["ok", "model"], "additionalProperties": False}
        text, usage = self.complete_json("Reply with JSON only.", 'Return {"ok": true, "model": "<your model name>"}.',
                                         schema, "ping")
        return {**parse_json_output(text), **usage}

    @classmethod
    def describe(cls) -> dict:
        return {"name": cls.name, "label": cls.label, "key_envs": list(cls.key_envs), "default_model": cls.default_model,
                "needs_base_url": cls.needs_base_url, "key_optional": cls.key_optional,
                "supports_effort": cls.supports_effort, "effort_levels": list(cls.effort_levels),
                "model_hint": cls.model_hint, "docs_url": cls.docs_url,
                "installed": bool(cls.package) and importlib.util.find_spec(cls.package.split(".")[0]) is not None
                and (importlib.util.find_spec(cls.package) is not None),
                "pip_name": cls.pip_name}


PROVIDERS: dict[str, type[BaseJudge]] = {}


def register_provider(cls: type[BaseJudge]) -> type[BaseJudge]:
    PROVIDERS[cls.name] = cls
    return cls


def _ensure_providers() -> None:
    from . import providers  # noqa: F401  (registers built-ins)


# ---------------------------------------------------------------------------
# Settings store (UI-managed) + environment fallback
# ---------------------------------------------------------------------------

_lock = threading.Lock()


def _store_path() -> Path:
    return get_settings().data_dir / "llm_settings.json"


def load_store() -> dict:
    p = _store_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_store(data: dict) -> None:
    p = _store_path()
    with _lock:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)  # contains API keys
        tmp.replace(p)
    global _judge, _judge_fp
    _judge, _judge_fp = None, None


def _provider_key(name: str, store: dict) -> tuple[str | None, str]:
    cls = PROVIDERS[name]
    k = (store.get("providers", {}).get(name, {}) or {}).get("api_key")
    if k:
        return k, "settings"
    for env in cls.key_envs:
        if os.environ.get(env):
            return os.environ[env], f"env:{env}"
    if name == "anthropic" and (os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE")
                                or (Path.home() / ".config" / "anthropic").exists()):
        return None, "profile"  # SDK resolves OAuth profile / token itself
    return None, ""


def resolve_config() -> tuple[JudgeConfig | None, str]:
    """Pick provider/model/key from UI settings, then environment. Returns (config, reason_if_none)."""
    _ensure_providers()
    store = load_store()
    env_choice = get_settings().judge
    choice = store.get("provider") or env_choice or "auto"
    if choice == "none":
        return None, "LLM judge is turned off"
    order = [choice] if choice != "auto" else ["anthropic", "openai", "gemini", "openai_compatible"]
    for name in order:
        if name not in PROVIDERS:
            return None, f"Unknown LLM provider '{name}'"
        cls = PROVIDERS[name]
        pconf = store.get("providers", {}).get(name, {}) or {}
        key, src = _provider_key(name, store)
        base_url = pconf.get("base_url") or (os.environ.get("VOICEEVAL_JUDGE_BASE_URL") if cls.needs_base_url else None)
        usable = bool(key) or src == "profile" or (cls.key_optional and base_url)
        if not usable:
            if choice != "auto":
                return None, f"No API key for {cls.label} — add one in Settings"
            continue
        model = (pconf.get("model")
                 or os.environ.get(f"VOICEEVAL_{name.upper()}_MODEL")
                 or (os.environ.get("VOICEEVAL_JUDGE_MODEL") if env_choice == name else None)
                 or cls.default_model)
        if not model:
            return None, f"Choose a model for {cls.label} in Settings"
        if cls.needs_base_url and not base_url:
            return None, f"Set the base URL for {cls.label} in Settings"
        effort = pconf.get("effort") or (get_settings().judge_effort if name == "anthropic" else None)
        return JudgeConfig(name, model, key, base_url, effort, src), ""
    return None, "No LLM API key configured — add one for Anthropic, OpenAI, Gemini or an OpenAI-compatible provider in Settings"


def judge_status() -> dict:
    cfg, reason = resolve_config()
    if cfg is None:
        return {"available": False, "reason": reason}
    cls = PROVIDERS[cfg.provider]
    if not cls.describe()["installed"]:
        return {"available": False, "reason": f"Install the '{cls.pip_name}' package for {cls.label}",
                "provider": cfg.provider}
    return {"available": True, "provider": cfg.provider, "label": cls.label, "model": cfg.model,
            "effort": cfg.effort, "key_source": cfg.key_source}


_judge: BaseJudge | None = None
_judge_fp: str | None = None


def build_judge(cfg: JudgeConfig) -> BaseJudge:
    _ensure_providers()
    return PROVIDERS[cfg.provider](cfg)


def get_judge() -> BaseJudge:
    global _judge, _judge_fp
    st = judge_status()
    if not st["available"]:
        raise JudgeUnavailable(st["reason"])
    cfg, _ = resolve_config()
    fp = hashlib.sha1(json.dumps(cfg.__dict__, sort_keys=True, default=str).encode()).hexdigest()
    if _judge is None or _judge_fp != fp:
        _judge, _judge_fp = build_judge(cfg), fp
    return _judge
