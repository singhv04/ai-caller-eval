"""Audio ingest + loading.

Ingest normalises any input (stereo file, or a pair of mono files) into one
canonical stereo 16-bit WAV per recording. Loading splits that WAV into the
human and AI channels using the per-recording channel mapping.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ANALYSIS_SR = 16_000
MAX_NATIVE_SR = 48_000


class AudioError(ValueError):
    pass


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=channels,sample_rate:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise AudioError(f"Could not read audio: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise AudioError("No audio stream found")
    return {
        "channels": int(streams[0]["channels"]),
        "sample_rate": int(streams[0]["sample_rate"]),
        "duration": float(data.get("format", {}).get("duration") or 0),
    }


def _ffmpeg(args: list[str]) -> None:
    out = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise AudioError(f"ffmpeg failed: {out.stderr.strip()[:300]}")


def normalize_stereo(src: Path, dest: Path) -> dict:
    """Convert a 2-channel recording of any format into canonical PCM WAV."""
    info = probe(src)
    if info["channels"] != 2:
        raise AudioError(
            f"Expected a 2-channel (human/AI) recording, got {info['channels']} channel(s). "
            "Upload a stereo file, two mono files, or a mono mix (auto speaker separation)."
        )
    sr = min(info["sample_rate"], MAX_NATIVE_SR)
    _ffmpeg(["-i", str(src), "-ac", "2", "-ar", str(sr), "-c:a", "pcm_s16le", str(dest)])
    return probe(dest)


def diarize_mono(src: Path, dest: Path) -> tuple[dict, dict]:
    """Mono fallback: split one mixed channel into [human, ai] virtual channels."""
    from .diarize import diarize_two_speakers, mono_to_virtual_stereo

    info = probe(src)
    sr = min(info["sample_rate"], MAX_NATIVE_SR)
    tmp = dest.with_suffix(".mono.wav")
    _ffmpeg(["-i", str(src), "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(tmp)])
    x, sr = sf.read(str(tmp), dtype="float32")
    tmp.unlink(missing_ok=True)
    d = diarize_two_speakers(x, sr)
    ai = d["ai"]
    stereo = mono_to_virtual_stereo(x, sr, d["speakers"], human=1 - ai, ai=ai)
    sf.write(str(dest), stereo, sr, subtype="PCM_16")
    diar = {"method": "pitch_timbre_kmeans", "human": d["stats"][1 - ai], "ai": d["stats"][ai],
            "note": "Mono recording split into virtual channels; overlap/barge-in timing is approximate. "
                    "If human/AI look swapped, change the human channel to 'right'."}
    return probe(dest), diar


def merge_mono_pair(left: Path, right: Path, dest: Path) -> dict:
    """Merge two mono files (channel 0 = left, channel 1 = right) into canonical stereo WAV."""
    sr = min(max(probe(left)["sample_rate"], probe(right)["sample_rate"]), MAX_NATIVE_SR)
    _ffmpeg([
        "-i", str(left), "-i", str(right),
        "-filter_complex",
        f"[0:a]aformat=channel_layouts=mono,aresample={sr}[l];"
        f"[1:a]aformat=channel_layouts=mono,aresample={sr}[r];"
        "[l][r]amerge=inputs=2,pan=stereo|c0=c0|c1=c1[a]",
        "-map", "[a]", "-c:a", "pcm_s16le", str(dest),
    ])
    return probe(dest)


@dataclass
class DualChannelAudio:
    """Both sides of a call, at native rate and at the 16 kHz analysis rate."""

    human_native: np.ndarray
    ai_native: np.ndarray
    native_sr: int
    human: np.ndarray
    ai: np.ndarray
    sr: int = ANALYSIS_SR

    @property
    def duration(self) -> float:
        return len(self.human) / self.sr

    def side(self, speaker: str, native: bool = False) -> np.ndarray:
        if native:
            return self.human_native if speaker == "human" else self.ai_native
        return self.human if speaker == "human" else self.ai


def _to_analysis_rate(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == ANALYSIS_SR:
        return x.astype(np.float32)
    g = np.gcd(sr, ANALYSIS_SR)
    return resample_poly(x, ANALYSIS_SR // g, sr // g).astype(np.float32)


def load_dual_channel(path: Path, human_channel: int = 0, ai_channel: int = 1) -> DualChannelAudio:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] < 2:
        raise AudioError("Recording is not dual-channel")
    h, a = data[:, human_channel], data[:, ai_channel]
    return DualChannelAudio(
        human_native=h, ai_native=a, native_sr=sr,
        human=_to_analysis_rate(h, sr), ai=_to_analysis_rate(a, sr),
    )


def write_temp(data: bytes, suffix: str) -> Path:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.write(data)
    f.close()
    return Path(f.name)
