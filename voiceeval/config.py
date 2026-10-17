"""Runtime settings (environment driven) and evaluation profiles (YAML driven)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:  # empty placeholders in .env must not mask real environment values
            os.environ.setdefault(key.strip(), value)


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    executor: str
    local_workers: int
    transcriber: str
    whisper_model: str
    whisper_device: str
    judge: str
    judge_model: str
    judge_effort: str
    profiles_dir: Path

    @staticmethod
    def from_env() -> "Settings":
        data_dir = Path(os.environ.get("VOICEEVAL_DATA_DIR", ROOT_DIR / "data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return Settings(
            data_dir=data_dir,
            database_url=os.environ.get("DATABASE_URL", f"sqlite:///{data_dir / 'voiceeval.db'}"),
            executor=os.environ.get("VOICEEVAL_EXECUTOR", "local"),
            local_workers=int(os.environ.get("VOICEEVAL_LOCAL_WORKERS", "4")),
            transcriber=os.environ.get("VOICEEVAL_TRANSCRIBER", "auto"),
            whisper_model=os.environ.get("VOICEEVAL_WHISPER_MODEL", "base.en"),
            whisper_device=os.environ.get("VOICEEVAL_WHISPER_DEVICE", "auto"),
            judge=os.environ.get("VOICEEVAL_JUDGE", ""),
            judge_model=os.environ.get("VOICEEVAL_JUDGE_MODEL", "claude-opus-5-5"),
            judge_effort=os.environ.get("VOICEEVAL_JUDGE_EFFORT", "medium"),
            profiles_dir=Path(os.environ.get("VOICEEVAL_PROFILES_DIR", ROOT_DIR / "config" / "profiles")),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


# ---------------------------------------------------------------------------
# Evaluation profiles
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Profile:
    """A named bundle of business context, policies, thresholds and gates.

    Profiles let one platform evaluate many different voice agents: each agent
    (or scenario family) gets its own YAML in config/profiles/. Any profile may
    `extends:` another to inherit its settings.
    """

    name: str
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def metric_override(self, metric_id: str) -> dict:
        return (self.data.get("metric_overrides") or {}).get(metric_id, {}) or {}

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(json.dumps(self.data, sort_keys=True, default=str).encode()).hexdigest()[:12]


def list_profiles() -> list[str]:
    d = get_settings().profiles_dir
    return sorted(p.stem for p in d.glob("*.yaml"))


def load_profile(name: str = "default", overrides: dict | None = None) -> Profile:
    d = get_settings().profiles_dir
    path = d / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Profile '{name}' not found in {d}")
    data = yaml.safe_load(path.read_text()) or {}
    parent = data.pop("extends", None)
    if parent:
        data = _deep_merge(load_profile(parent).data, data)
    if overrides:
        data = _deep_merge(data, overrides)
    return Profile(name=name, data=data)
