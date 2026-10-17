"""Signal-level feature extractors used by speech-output and prosody metrics."""
from __future__ import annotations

import re

import numpy as np

try:
    import pyloudnorm as _pyln
except Exception:  # pragma: no cover - optional
    _pyln = None


def slice_audio(x: np.ndarray, sr: int, start: float, end: float) -> np.ndarray:
    return x[max(0, int(start * sr)): max(0, int(end * sr))]


def clipped_samples(x: np.ndarray, level: float = 0.999, min_run: int = 2) -> int:
    """Count samples sitting at full scale in runs of >= min_run (a flat-topped waveform)."""
    hot = np.abs(x) >= level
    if not hot.any():
        return 0
    # run-length encode
    padded = np.concatenate([[0], hot.astype(np.int8), [0]])
    d = np.diff(padded)
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    lengths = ends - starts
    return int(lengths[lengths >= min_run].sum())


def digital_dropouts(x: np.ndarray, sr: int, min_ms: float = 15, max_ms: float = 250,
                     energy_ctx_ms: float = 30, silence_level: float = 1e-4) -> list[tuple[float, float]]:
    """Find near-digital-silence gaps that abruptly cut through active speech.

    Natural pauses ramp down; transport/synthesis dropouts jump from speech
    energy straight to (near) zero and back.
    """
    quiet = np.abs(x) < silence_level
    padded = np.concatenate([[0], quiet.astype(np.int8), [0]])
    d = np.diff(padded)
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    ctx = int(sr * energy_ctx_ms / 1000)
    out = []
    for s, e in zip(starts, ends):
        dur_ms = (e - s) / sr * 1000
        if not (min_ms <= dur_ms <= max_ms):
            continue
        before = x[max(0, s - ctx): s]
        after = x[e: e + ctx]
        if before.size < ctx // 2 or after.size < ctx // 2:
            continue
        if np.sqrt(np.mean(before ** 2)) > 0.02 and np.sqrt(np.mean(after ** 2)) > 0.02:
            out.append((s / sr, e / sr))
    return out


def click_count(x: np.ndarray, sr: int, k: float = 25.0, min_abs: float = 0.5, window_ms: float = 10) -> int:
    """Count impulsive discontinuities (clicks/pops).

    Uses the second difference (a high-pass that is small for smooth speech
    waveforms) and flags samples far above the local average of that signal,
    so loud speech does not trigger it but a step or spike does.
    """
    if x.size < sr // 10:
        return 0
    e = np.abs(np.diff(x.astype(np.float64), n=2))
    w = max(3, int(sr * window_ms / 1000))
    nb = int(np.ceil(e.size / w))
    padded = np.pad(e, (0, nb * w - e.size), mode="edge").reshape(nb, w)
    block = np.median(padded, axis=1)                      # robust: a 1-3 sample spike can't move it
    block = np.maximum.reduce([block, np.roll(block, 1), np.roll(block, -1)])  # include neighbours
    local = np.repeat(block, w)[: e.size] + 1e-6
    idx = np.where((e > min_abs) & (e > k * local))[0]
    if idx.size == 0:
        return 0
    gap = int(sr * 0.005)  # collapse hits within 5 ms into one event
    return int(1 + np.sum(np.diff(idx) > gap))


def integrated_loudness(x: np.ndarray, sr: int) -> float | None:
    if x.size < int(0.4 * sr):
        return None
    if _pyln is not None:
        try:
            val = _pyln.Meter(sr).integrated_loudness(x.astype(np.float64))
            return float(val) if np.isfinite(val) else None
        except Exception:
            pass
    rms = np.sqrt(np.mean(x.astype(np.float64) ** 2) + 1e-12)
    return float(20 * np.log10(rms) - 0.691)  # rough LUFS approximation


def f0_contour(x: np.ndarray, sr: int, fmin: float = 60, fmax: float = 400,
               frame_ms: float = 40, hop_ms: float = 10, voicing: float = 0.45) -> np.ndarray:
    """Normalised-autocorrelation pitch tracker. Returns F0 per frame (NaN = unvoiced)."""
    n, hop = int(sr * frame_ms / 1000), int(sr * hop_ms / 1000)
    lag_min, lag_max = int(sr / fmax), int(sr / fmin)
    out = []
    for i in range(0, max(0, len(x) - n), hop):
        f = x[i: i + n].astype(np.float64)
        f = f - f.mean()
        energy = np.dot(f, f)
        if energy < 1e-6:
            out.append(np.nan)
            continue
        ac = np.correlate(f, f, mode="full")[n - 1:]
        ac /= ac[0]
        seg = ac[lag_min: min(lag_max, len(ac) - 1)]
        if seg.size == 0:
            out.append(np.nan)
            continue
        lag = int(np.argmax(seg)) + lag_min
        out.append(sr / lag if ac[lag] >= voicing else np.nan)
    return np.array(out)


def zscore_voiced(f0: np.ndarray) -> np.ndarray:
    v = f0[np.isfinite(f0)]
    if v.size < 5:
        return np.array([])
    logv = np.log(v)
    return (logv - logv.mean()) / (logv.std() + 1e-9)


def dtw_cost(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    """Classic DTW; returns (total cost, path length)."""
    n, m = len(a), len(b)
    D = np.full((n + 1, m + 1), np.inf)
    L = np.zeros((n + 1, m + 1), dtype=np.int32)
    D[0, 0] = 0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            c = abs(a[i - 1] - b[j - 1])
            choices = (D[i - 1, j], D[i, j - 1], D[i - 1, j - 1])
            k = int(np.argmin(choices))
            prev = [(i - 1, j), (i, j - 1), (i - 1, j - 1)][k]
            D[i, j] = c + choices[k]
            L[i, j] = L[prev] + 1
    return float(D[n, m]), int(L[n, m])


_VOWEL_GROUPS = re.compile(r"[aeiouy]+")


def count_syllables(text: str) -> int:
    """Heuristic English syllable counter (good enough for rate metrics)."""
    total = 0
    for word in re.findall(r"[a-zA-Z']+", text.lower()):
        word = word.replace("'", "")
        groups = len(_VOWEL_GROUPS.findall(word))
        if word.endswith("e") and not word.endswith(("le", "ee")) and groups > 1:
            groups -= 1
        total += max(1, groups)
    # digits are spoken as words: approximate 1.5 syllables per digit
    total += int(round(1.5 * len(re.findall(r"\d", text))))
    return total
