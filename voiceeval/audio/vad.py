"""Voice activity detection, run independently on each channel.

Because the human and the AI are recorded on separate channels, "who is
speaking when" reduces to VAD per channel — no diarization needed.

`EnergyVAD` is dependency-free and works well on clean telephony channels.
Swap in a neural VAD (Silero, webrtcvad, pyannote) by implementing `VAD.detect`
and registering it in `VAD_REGISTRY`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

import numpy as np


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)


class VAD(Protocol):
    def detect(self, x: np.ndarray, sr: int) -> list[Segment]: ...


class EnergyVAD:
    """Adaptive-threshold energy VAD with hysteresis and hangover."""

    def __init__(self, frame_ms: int = 20, threshold_db: float = 12.0, floor_dbfs: float = -55.0,
                 min_speech_ms: int = 120, merge_gap_ms: int = 180, pad_ms: int = 30):
        self.frame_ms = frame_ms
        self.threshold_db = threshold_db
        self.floor_dbfs = floor_dbfs
        self.min_speech_ms = min_speech_ms
        self.merge_gap_ms = merge_gap_ms
        self.pad_ms = pad_ms

    def frame_db(self, x: np.ndarray, sr: int) -> np.ndarray:
        n = int(sr * self.frame_ms / 1000)
        frames = len(x) // n
        if frames == 0:
            return np.array([])
        f = x[: frames * n].reshape(frames, n).astype(np.float64)
        rms = np.sqrt(np.mean(f * f, axis=1) + 1e-12)
        return 20 * np.log10(rms + 1e-12)

    def detect(self, x: np.ndarray, sr: int) -> list[Segment]:
        db = self.frame_db(x, sr)
        if db.size == 0:
            return []
        noise_floor = float(np.percentile(db, 10))
        on_thr = max(noise_floor + self.threshold_db, self.floor_dbfs)
        off_thr = on_thr - 4.0  # hysteresis
        active = np.zeros_like(db, dtype=bool)
        state = False
        for i, v in enumerate(db):
            state = v >= on_thr if not state else v >= off_thr
            active[i] = state

        fs = self.frame_ms / 1000
        segs: list[Segment] = []
        start = None
        for i, a in enumerate(active):
            if a and start is None:
                start = i
            elif not a and start is not None:
                segs.append(Segment(start * fs, i * fs))
                start = None
        if start is not None:
            segs.append(Segment(start * fs, len(active) * fs))

        merged: list[Segment] = []
        for s in segs:
            if merged and s.start - merged[-1].end <= self.merge_gap_ms / 1000:
                merged[-1].end = s.end
            else:
                merged.append(Segment(s.start, s.end))
        pad = self.pad_ms / 1000
        dur = len(x) / sr
        return [
            Segment(round(max(0.0, s.start - pad), 3), round(min(dur, s.end + pad), 3))
            for s in merged if s.duration * 1000 >= self.min_speech_ms
        ]


VAD_REGISTRY = {"energy": EnergyVAD}


def build_vad(cfg: dict | None) -> VAD:
    cfg = dict(cfg or {})
    kind = cfg.pop("type", "energy")
    return VAD_REGISTRY[kind](**cfg)
