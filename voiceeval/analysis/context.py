"""Per-call evaluation context.

Expensive artifacts (decoded audio, VAD, transcript, judge verdicts) are
computed lazily — only when a selected metric needs them — and the slow ones
(ASR, LLM) are cached in storage so re-running other metrics is cheap.
"""
from __future__ import annotations

import hashlib
import json
import threading
from functools import cached_property
from typing import Any

from ..audio.loader import DualChannelAudio, load_dual_channel
from ..audio.vad import Segment, build_vad
from ..config import Profile
from ..judge.base import JudgeUnavailable, get_judge
from ..metrics.base import NA_NEEDS_LLM, NA_NEEDS_TRANSCRIPT, JudgeSection, MetricResult, MissingInput
from ..storage import get_storage
from ..transcription.base import Transcript, parse_sidecar
from ..transcription.providers import get_asr
from .timeline import Timeline, TimelineParams

_judge_locks: dict[str, threading.Lock] = {}


class CallContext:
    def __init__(self, recording: dict, profile: Profile, options: dict | None = None):
        self.recording = recording
        self.profile = profile
        self.options = options or {}
        self.storage = get_storage()
        self.results: dict[str, MetricResult] = {}
        self.judge_sections: dict[str, JudgeSection] = {}
        self.warnings: list[str] = []

    # ---- recording info --------------------------------------------------
    @property
    def id(self) -> str:
        return self.recording["id"]

    @property
    def meta(self) -> dict:
        return self.recording.get("meta") or {}

    @property
    def tags(self) -> dict:
        return self.recording.get("tags") or {}

    @property
    def annotations(self) -> dict:
        return self.meta.get("annotations") or {}

    @property
    def events(self) -> list[dict]:
        return self.meta.get("events") or []

    def cache_key(self, kind: str, extra: str = "") -> str:
        return f"cache/{self.id}/{kind}{('-' + extra) if extra else ''}.json"

    # ---- audio / VAD / timeline -----------------------------------------
    @cached_property
    def audio(self) -> DualChannelAudio:
        path = self.storage.local_path(self.recording["audio_key"])
        return load_dual_channel(path, self.recording["human_channel"], self.recording["ai_channel"])

    @cached_property
    def vad(self) -> dict[str, list[Segment]]:
        cfg = self.profile.get("audio.vad", {}) or {}
        fp = hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:10]
        key = self.cache_key("vad", fp)
        if self.storage.exists(key):
            d = self.storage.get_json(key)
            return {k: [Segment(**s) for s in v] for k, v in d.items()}
        vad = build_vad(cfg)
        a = self.audio
        out = {"human": vad.detect(a.human, a.sr), "ai": vad.detect(a.ai, a.sr)}
        self.storage.put_json(key, {k: [s.to_dict() for s in v] for k, v in out.items()})
        return out

    @cached_property
    def timeline(self) -> Timeline:
        params = TimelineParams.from_profile(self.profile.get("timeline", {}))
        dur = self.recording.get("duration_s") or self.audio.duration
        return Timeline(self.vad["human"], self.vad["ai"], dur, params)

    # ---- transcript --------------------------------------------------------
    @cached_property
    def transcript(self) -> Transcript | None:
        if self.recording.get("transcript_key"):
            return parse_sidecar(self.storage.get_json(self.recording["transcript_key"]))
        key = self.cache_key("transcript")
        if self.storage.exists(key):
            return Transcript.from_dict(self.storage.get_json(key))
        asr = get_asr()
        if asr is None:
            return None
        a = self.audio
        utts = asr.transcribe_channel(a.human, a.sr, "human") + asr.transcribe_channel(a.ai, a.sr, "ai")
        tr = Transcript(sorted(utts, key=lambda u: u.start), asr.name)
        self.storage.put_json(key, tr.to_dict())
        return tr

    def require_transcript(self) -> Transcript:
        tr = self.transcript
        if tr is None or not tr.utterances:
            raise MissingInput("No transcript: upload a sidecar transcript or enable an ASR provider "
                               "(pip install faster-whisper)", NA_NEEDS_TRANSCRIPT)
        return tr

    @cached_property
    def turn_texts(self) -> list[dict]:
        """Timeline turns annotated with transcript text (the unit the judge reasons about)."""
        tr = self.require_transcript()
        rows = []
        if self.timeline.turns:
            for t in self.timeline.turns:
                text = tr.text_in(t.speaker, t.start - 0.15, t.end + 0.15)
                if text:
                    rows.append({"speaker": t.speaker, "start": t.start, "end": t.end, "text": text})
        if not rows:  # fall back to transcript utterances (e.g. VAD found nothing usable)
            rows = [{"speaker": u.speaker, "start": u.start, "end": u.end, "text": u.text} for u in tr.utterances]
        for i, r in enumerate(rows):
            r["index"] = i
        return rows

    def transcript_text(self) -> str:
        return "\n".join(
            f"[T{r['index']} {r['start']:.1f}s-{r['end']:.1f}s] {'HUMAN' if r['speaker'] == 'human' else 'AI'}: {r['text']}"
            for r in self.turn_texts
        )

    def turn_span(self, index: int | None) -> dict:
        if index is None or not (0 <= index < len(self.turn_texts)):
            return {}
        r = self.turn_texts[index]
        return {"start": round(r["start"], 2), "end": round(r["end"], 2), "speaker": r["speaker"], "turn": index}

    # ---- judge -------------------------------------------------------------
    def _call_facts(self) -> str:
        facts = {k: v for k, v in self.tags.items()}
        if self.meta.get("scenario"):
            facts["scenario"] = self.meta["scenario"]
        if self.meta.get("expected_outcome"):
            facts["expected_outcome"] = self.meta["expected_outcome"]
        return json.dumps(facts) if facts else ""

    @cached_property
    def judge(self) -> dict:
        if not self.judge_sections:
            raise MissingInput("No judge-based metric requested")
        judge = get_judge()  # raises JudgeUnavailable
        text = self.transcript_text()
        fp = hashlib.sha1(json.dumps({
            "t": text, "p": self.profile.fingerprint, "m": judge.model, "f": self._call_facts(),
            "s": {k: [v.instructions, v.schema] for k, v in sorted(self.judge_sections.items())},
        }, sort_keys=True).encode()).hexdigest()[:16]
        key = self.cache_key("judge", fp)
        lock = _judge_locks.setdefault(key, threading.Lock())
        with lock:
            if self.storage.exists(key) and not self.options.get("refresh_judge"):
                return self.storage.get_json(key)
            out = judge.evaluate(text, self.profile, self.judge_sections, self._call_facts())
            self.storage.put_json(key, out)
            return out

    def judge_for(self, metric_id: str) -> dict:
        try:
            data = self.judge
        except JudgeUnavailable as e:
            raise MissingInput(f"LLM judge unavailable: {e}", NA_NEEDS_LLM) from e
        if metric_id not in data:
            raise MissingInput("Judge returned no section for this metric")
        return data[metric_id]

    def dependency(self, metric_id: str) -> MetricResult:
        res = self.results.get(metric_id)
        if res is None:
            raise MissingInput(f"Dependency '{metric_id}' was not computed")
        return res

    # ---- summary for the UI ---------------------------------------------
    def analysis_summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"warnings": self.warnings}
        if "timeline" in self.__dict__:
            out["timeline"] = self.timeline.to_dict()
        if "transcript" in self.__dict__ and self.transcript is not None:
            out["transcript_source"] = self.transcript.source
            try:
                out["turns"] = self.turn_texts
            except MissingInput:
                pass
        if "judge" in self.__dict__:
            out["judge_meta"] = self.judge.get("_meta")
        return out
