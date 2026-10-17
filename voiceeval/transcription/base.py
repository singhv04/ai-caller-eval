"""Transcript model + provider interface.

Each channel is transcribed independently, so every utterance is attributed to
"human" or "ai" by construction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

import numpy as np

SPEAKER_ALIASES = {
    "human": "human", "user": "human", "customer": "human", "caller": "human", "client": "human",
    "ai": "ai", "agent": "ai", "bot": "ai", "assistant": "ai", "system": "ai", "voicebot": "ai",
}


def normalize_speaker(s: str) -> str:
    key = (s or "").strip().lower()
    if key not in SPEAKER_ALIASES:
        raise ValueError(f"Unknown speaker label '{s}' (use human/ai)")
    return SPEAKER_ALIASES[key]


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Utterance:
    speaker: str
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    language: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Transcript:
    utterances: list[Utterance]
    source: str = "unknown"

    def side(self, speaker: str) -> list[Utterance]:
        return [u for u in self.utterances if u.speaker == speaker]

    def text_in(self, speaker: str, start: float, end: float) -> str:
        """Text of `speaker` overlapping the window (word-level when available)."""
        parts = []
        for u in self.side(speaker):
            if u.end < start or u.start > end:
                continue
            if u.words:
                parts.extend(w.text for w in u.words if w.end > start and w.start < end)
            else:
                parts.append(u.text)
        return " ".join(parts).strip()

    def to_dict(self) -> dict:
        return {"source": self.source, "utterances": [u.to_dict() for u in self.utterances]}

    @staticmethod
    def from_dict(d: dict) -> "Transcript":
        utts = []
        for u in d.get("utterances", []):
            words = [Word(**w) for w in u.get("words", [])]
            utts.append(Utterance(u["speaker"], u["start"], u["end"], u["text"], words, u.get("language")))
        return Transcript(sorted(utts, key=lambda x: x.start), d.get("source", "cache"))


def parse_sidecar(obj) -> Transcript:
    """Accept common transcript shapes:

    - {"human": [utt...], "ai": [utt...]}
    - {"utterances": [{"speaker": "human", "start": 0.1, "end": 1.2, "text": "...", "words": [...]}]}
    - [ {"speaker": ..., ...}, ... ]
    """
    raw: list[tuple[str, dict]] = []
    if isinstance(obj, dict) and ("human" in obj or "ai" in obj):
        for spk in ("human", "ai"):
            raw += [(spk, u) for u in obj.get(spk, [])]
    else:
        items = obj.get("utterances", []) if isinstance(obj, dict) else obj
        raw = [(u.get("speaker") or u.get("role") or u.get("channel"), u) for u in items]
    utts = []
    for spk, u in raw:
        words = [Word(str(w.get("text", w.get("word", ""))).strip(), float(w["start"]), float(w["end"]))
                 for w in u.get("words", [])]
        utts.append(Utterance(normalize_speaker(str(spk)), float(u["start"]), float(u["end"]),
                              str(u.get("text", "")).strip(), words, u.get("language")))
    return Transcript(sorted(utts, key=lambda x: x.start), "sidecar")


class Transcriber(Protocol):
    name: str

    def transcribe_channel(self, x: np.ndarray, sr: int, speaker: str) -> list[Utterance]: ...
