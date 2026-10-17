"""ASR providers. Add a new one (Deepgram, AssemblyAI, Whisper API...) by
implementing `transcribe_channel` and registering it in `PROVIDERS`."""
from __future__ import annotations

import importlib.util
from functools import lru_cache

import numpy as np

from ..config import get_settings
from .base import Utterance, Word


class FasterWhisperTranscriber:
    name = "faster_whisper"

    def __init__(self, model: str, device: str = "auto"):
        from faster_whisper import WhisperModel  # optional dependency

        self.model = WhisperModel(model, device=device, compute_type="int8" if device in ("auto", "cpu") else "default")

    def transcribe_channel(self, x: np.ndarray, sr: int, speaker: str) -> list[Utterance]:
        assert sr == 16_000, "faster-whisper expects 16 kHz audio"
        segments, info = self.model.transcribe(x.astype(np.float32), word_timestamps=True, vad_filter=True,
                                               condition_on_previous_text=False)
        out = []
        for seg in segments:
            words = [Word(w.word.strip(), float(w.start), float(w.end)) for w in (seg.words or [])]
            out.append(Utterance(speaker, float(seg.start), float(seg.end), seg.text.strip(), words, info.language))
        return out


PROVIDERS = {"faster_whisper": FasterWhisperTranscriber}


def available_asr() -> str | None:
    s = get_settings()
    if s.transcriber in ("none", "sidecar"):
        return None
    if s.transcriber == "faster_whisper" or (s.transcriber == "auto" and importlib.util.find_spec("faster_whisper")):
        return "faster_whisper"
    return None


@lru_cache(maxsize=1)
def get_asr():
    name = available_asr()
    if name is None:
        return None
    s = get_settings()
    return PROVIDERS[name](s.whisper_model, s.whisper_device)
