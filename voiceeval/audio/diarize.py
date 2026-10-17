"""Mono fallback: split a single-channel call into human / AI virtual channels.

The platform is built for dual-channel recordings, where attribution is exact.
When only a mono mix exists, we diarize it with a lightweight 2-speaker
clustering of speech segments (pitch + timbre), then write a canonical stereo
file where each channel contains only that speaker's segments. Every
downstream component works unchanged; overlap/barge-in timing is approximate
because simultaneous speech can't be separated, and recordings are flagged
`meta.channel_source = "mono_diarized"`.

A neural diarizer (pyannote, NeMo) can replace `diarize_two_speakers` without
touching anything else.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import resample_poly

from .features import f0_contour
from .vad import EnergyVAD, Segment

SR = 16_000


def _features(y: np.ndarray) -> np.ndarray | None:
    f0 = f0_contour(y, SR)
    v = f0[np.isfinite(f0)]
    if v.size < 5:
        return None
    spec = np.abs(np.fft.rfft(y[: SR * 2] * np.hanning(min(len(y), SR * 2))))
    freqs = np.fft.rfftfreq(min(len(y), SR * 2), 1 / SR)
    centroid = float((spec * freqs).sum() / (spec.sum() + 1e-9))
    rms_db = 20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-9)
    return np.array([np.log(np.median(v)), centroid / 1000.0, rms_db])


def _mel_matrix(n_fft: int, n_mels: int = 24, fmin: float = 80, fmax: float = 7600) -> np.ndarray:
    mel = lambda f: 2595 * np.log10(1 + f / 700)
    imel = lambda m: 700 * (10 ** (m / 2595) - 1)
    pts = imel(np.linspace(mel(fmin), mel(fmax), n_mels + 2))
    bins = np.floor((n_fft + 1) * pts / SR).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for i in range(1, n_mels + 1):
        l, c, r = bins[i - 1], bins[i], bins[i + 1]
        if c > l:
            fb[i - 1, l:c] = (np.arange(l, c) - l) / (c - l)
        if r > c:
            fb[i - 1, c:r] = (r - np.arange(c, r)) / (r - c)
    return fb


def window_features(y: np.ndarray, win_s: float = 0.2, hop_s: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """Per-window [log F0, spectral-shape mel bands]. Returns (times, features); F0 = NaN when unvoiced."""
    n, h = int(win_s * SR), int(hop_s * SR)
    n_fft = 512
    fb = _mel_matrix(n_fft)
    f0 = f0_contour(y, SR, hop_ms=hop_s * 1000 / 2)
    times, feats = [], []
    for i in range(0, max(0, len(y) - n), h):
        w = y[i: i + n]
        frames = np.lib.stride_tricks.sliding_window_view(w, n_fft)[:: n_fft // 2] * np.hanning(n_fft)
        spec = (np.abs(np.fft.rfft(frames, axis=1)) ** 2).mean(0)
        mel = np.log(fb @ spec + 1e-10)
        mel = mel - mel.mean()  # spectral shape, independent of loudness
        k = int((i + n / 2) / (h / 2))
        fv = f0[max(0, k - 2): k + 3]
        fv = fv[np.isfinite(fv)]
        feats.append(np.concatenate([[np.log(np.median(fv)) if fv.size else np.nan], mel]))
        times.append((i + n / 2) / SR)
    return np.array(times), np.array(feats)


class _DiagGauss:
    def __init__(self, X: np.ndarray):
        self.mu = np.nanmean(X, 0)
        self.var = np.nanvar(X, 0) + 1e-3

    def loglik(self, X: np.ndarray) -> np.ndarray:
        d = (X - self.mu) ** 2 / self.var + np.log(self.var)
        return -0.5 * np.nansum(d, axis=1)  # NaN dims (unvoiced pitch) are skipped


def refine_speakers(y: np.ndarray, segs: list[Segment], labels: list[int],
                    min_run_s: float = 0.7, smooth_windows: int = 5) -> dict[int, list[Segment]]:
    """Re-label every 100 ms of speech with per-speaker Gaussian models trained on the initial clustering,
    then split segments wherever the other speaker holds the floor for ≥ min_run_s."""
    times, F = window_features(y)
    seg_of = np.full(times.size, -1)
    for j, s in enumerate(segs):
        seg_of[(times >= s.start) & (times < s.end)] = j
    init = np.array([labels[j] if j >= 0 else -1 for j in seg_of])
    speech = seg_of >= 0
    models = {}
    for k in (0, 1):
        Xk = F[speech & (init == k)]
        if len(Xk) < 10:
            return {k2: [s for s, l in zip(segs, labels) if l == k2] for k2 in (0, 1)}
        models[k] = _DiagGauss(Xk)
    for _ in range(2):  # a couple of EM-style refinement passes
        ll = np.stack([models[0].loglik(F), models[1].loglik(F)], 1)
        lab = ll.argmax(1)
        for k in (0, 1):
            if (speech & (lab == k)).sum() >= 10:
                models[k] = _DiagGauss(F[speech & (lab == k)])
    score = (ll[:, 1] - ll[:, 0])
    out: dict[int, list[Segment]] = {0: [], 1: []}
    min_w = max(1, int(round(min_run_s / 0.1)))
    for j, s in enumerate(segs):
        idx = np.where(seg_of == j)[0]
        if idx.size == 0:
            out[labels[j]].append(s)
            continue
        sc = np.convolve(score[idx], np.ones(smooth_windows) / smooth_windows, mode="same")
        lab = (sc > 0).astype(int)
        runs: list[list] = []
        for i, v in enumerate(lab):
            if runs and runs[-1][0] == v:
                runs[-1][2] = i
            else:
                runs.append([int(v), i, i])
        merged: list[list] = []
        for r in runs:
            if merged and (r[2] - r[1] + 1 < min_w or merged[-1][0] == r[0]):
                merged[-1][2] = r[2]
            else:
                merged.append(r)
        if len(merged) > 1 and merged[0][2] - merged[0][1] + 1 < min_w:  # short leading run joins the next
            merged[1][1] = merged[0][1]
            merged.pop(0)
        # adjacent runs share one exact boundary — padding here would fabricate overlaps / barge-ins
        bounds = [s.start] + [(times[idx[b]] + times[idx[b + 1]]) / 2 for _, _, b in merged[:-1]] + [s.end]
        for n_run, (v, _, _) in enumerate(merged):
            out[v].append(Segment(round(bounds[n_run], 3), round(bounds[n_run + 1], 3)))
    return out


def _kmeans2(X: np.ndarray, iters: int = 50) -> np.ndarray:
    # init with the extremes of the first (pitch) dimension
    c = np.stack([X[np.argmin(X[:, 0])], X[np.argmax(X[:, 0])]])
    labels = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        d = ((X[:, None, :] - c[None]) ** 2).sum(-1)
        new = d.argmin(1)
        if (new == labels).all() and _ > 0:
            break
        labels = new
        for k in (0, 1):
            if (labels == k).any():
                c[k] = X[labels == k].mean(0)
    return labels


def diarize_two_speakers(x: np.ndarray, sr: int) -> dict:
    """Returns {"speakers": {0: [Segment], 1: [Segment]}, "ai": 0|1, "stats": {...}}."""
    y = x if sr == SR else resample_poly(x, SR // np.gcd(sr, SR), sr // np.gcd(sr, SR)).astype(np.float32)
    # fine segmentation so quick exchanges are split into separate pieces
    segs = EnergyVAD(merge_gap_ms=120, min_speech_ms=150).detect(y, SR)
    feats, keep = [], []
    for s in segs:
        f = _features(y[int(s.start * SR): int(s.end * SR)])
        if f is not None:
            feats.append(f)
            keep.append(s)
    if len(feats) < 2:
        raise ValueError("Could not find two speakers in the mono recording")
    X = np.array(feats)
    Xn = (X - X.mean(0)) / (X.std(0) + 1e-9)
    Xn[:, 0] *= 2.0  # pitch is the most reliable cue
    Xn[:, 2] *= 0.5
    labels = _kmeans2(Xn)

    speakers = refine_speakers(y, keep, [int(l) for l in labels])
    # unvoiced segments (no pitch) inherit the label of the nearest labelled segment
    labelled = [(s, int(l)) for s, l in zip(keep, labels)]
    for s in segs:
        if s in keep:
            continue
        nearest = min(labelled, key=lambda p: min(abs(p[0].start - s.end), abs(s.start - p[0].end)))
        speakers[nearest[1]].append(s)
    for k in speakers:
        speakers[k].sort(key=lambda s: s.start)

    stats = {}
    for k in (0, 1):
        m = labels == k
        talk = sum(s.duration for s in speakers[k])
        stats[k] = {"segments": len(speakers[k]), "talk_s": round(float(talk), 1),
                    "median_f0_hz": round(float(np.exp(np.median(X[m, 0]))), 1) if m.any() else None,
                    "loudness_sd_db": round(float(X[m, 2].std()), 2) if m.sum() > 1 else None}
    # Heuristic: synthesized voices have steadier loudness; ties broken by talk time.
    def ai_score(k):
        sd = stats[k]["loudness_sd_db"] or 0.0
        total = stats[0]["talk_s"] + stats[1]["talk_s"] or 1
        return -sd + 4.0 * stats[k]["talk_s"] / total
    ai = 0 if ai_score(0) >= ai_score(1) else 1
    return {"speakers": speakers, "ai": ai, "stats": stats}


def mono_to_virtual_stereo(x: np.ndarray, sr: int, speakers: dict, human: int, ai: int) -> np.ndarray:
    """Stereo array [human, ai]: each channel keeps only that speaker's segments (with short fades)."""
    out = np.zeros((len(x), 2), dtype=np.float32)
    fade = int(0.01 * sr)
    for ch, spk in ((0, human), (1, ai)):
        for s in speakers[spk]:
            a, b = int(s.start * sr), min(len(x), int(s.end * sr))
            if b <= a:
                continue
            seg = x[a:b].copy()
            f = min(fade, len(seg) // 2)
            if f:
                seg[:f] *= np.linspace(0, 1, f)
                seg[-f:] *= np.linspace(1, 0, f)
            out[a:b, ch] = seg
    return out
