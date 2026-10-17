"""LLM provider plugins: request shape and response parsing, with mocked SDK clients (no network)."""
from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace as NS

os.environ.setdefault("VOICEEVAL_DATA_DIR", tempfile.mkdtemp(prefix="voiceeval-judge-"))

from voiceeval.config import load_profile  # noqa: E402
from voiceeval.judge import base  # noqa: E402
from voiceeval.judge.base import JudgeConfig, build_judge, parse_json_output  # noqa: E402
from voiceeval.metrics import all_metrics  # noqa: E402

SECTIONS = {m.id: m.judge_section(load_profile()) for m in all_metrics() if m.judge_section(load_profile())}
ANSWER = {mid: {} for mid in SECTIONS}


def test_parse_json_tolerates_fences():
    assert parse_json_output('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_output('Sure! {"a": 2} hope that helps') == {"a": 2}


def test_openai_provider_uses_strict_schema():
    j = build_judge(JudgeConfig("openai", "some-model", "sk-test"))
    seen = {}

    def create(**kw):
        seen.update(kw)
        msg = NS(content=json.dumps(ANSWER), refusal=None)
        return NS(choices=[NS(message=msg, finish_reason="stop")], model="some-model",
                  usage=NS(prompt_tokens=10, completion_tokens=5))

    j.client = NS(chat=NS(completions=NS(create=create)))
    out = j.evaluate("[T0 0.0s-1.0s] AI: hi", load_profile(), SECTIONS, "")
    assert seen["response_format"]["type"] == "json_schema" and seen["response_format"]["json_schema"]["strict"]
    assert set(SECTIONS) <= set(out) and out["_meta"]["provider"] == "openai"


def test_gemini_provider_uses_json_schema():
    j = build_judge(JudgeConfig("gemini", "gemini-x", "key"))
    seen = {}

    def generate_content(model, contents, config):
        seen["config"] = config
        return NS(text=json.dumps(ANSWER), candidates=[1], usage_metadata=NS(prompt_token_count=3, candidates_token_count=4))

    j.client = NS(models=NS(generate_content=generate_content))
    out = j.evaluate("[T0 0.0s-1.0s] AI: hi", load_profile(), SECTIONS, "")
    assert seen["config"].response_mime_type == "application/json"
    assert seen["config"].response_json_schema["required"] == list(SECTIONS)
    assert out["_meta"]["provider"] == "gemini"


def test_anthropic_provider_uses_output_format():
    j = build_judge(JudgeConfig("anthropic", "claude-opus-5-5", "sk-ant-test", effort="medium"))
    seen = {}

    def create(**kw):
        seen.update(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(ANSWER))], model="claude-opus-5-5",
                  usage=NS(input_tokens=1, output_tokens=1))

    j.client = NS(beta=NS(messages=NS(create=create)))
    out = j.evaluate("[T0 0.0s-1.0s] AI: hi", load_profile(), SECTIONS, "")
    assert seen["output_config"]["format"]["type"] == "json_schema" and seen["output_config"]["effort"] == "medium"
    assert out["_meta"]["provider"] == "anthropic"


def test_resolution_prefers_settings_then_env(monkeypatch):
    monkeypatch.setattr(base, "load_store", lambda: {"provider": "gemini", "providers": {"gemini": {"api_key": "g-key", "model": "gemini-x"}}})
    cfg, _ = base.resolve_config()
    assert (cfg.provider, cfg.model, cfg.key_source) == ("gemini", "gemini-x", "settings")
    monkeypatch.setattr(base, "load_store", lambda: {"provider": "auto"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(base.Path, "home", lambda: base.Path("/nonexistent"))
    cfg, _ = base.resolve_config()
    assert cfg.provider == "openai" and cfg.key_source == "env:OPENAI_API_KEY"
