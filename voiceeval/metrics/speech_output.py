"""Speech-output quality of the AI channel (framework priorities 34-39).

These read the isolated AI channel at its native sample rate, so they measure
exactly what the AI emitted — no human audio contamination.
"""
from __future__ import annotations

import numpy as np

from ..audio.features import clipped_samples, click_count, count_syllables, digital_dropouts, integrated_loudness, slice_audio
from .base import REQ_AUDIO, REQ_TRANSCRIPT, Metric, register
from .util import ev


def _ai_turn_audio(ctx):
    a = ctx.audio
    for t in ctx.timeline.turns_of("ai"):
        yield t, slice_audio(a.ai_native, a.native_sr, t.start, t.end)


@register
class SynthesisArtifactRate(Metric):
    id = "synthesis_artifact_rate"
    priority = 34
    name = "Synthesis artifact rate"
    category = "Speech output"
    question = "How often generated speech sounds broken."
    value_range = "0–100%; ideal ~0%."
    formula = "AI clips with audible glitches / All AI clips × 100"
    terminology = "Clip = one AI turn. Glitch = impulsive click/pop or abrupt digital dropout detected in the AI channel."
    direction = "lower"
    good, bad = 1, 5

    def compute(self, ctx):
        sr = ctx.audio.native_sr
        bad, n, evidence = 0, 0, []
        for t, x in _ai_turn_audio(ctx):
            n += 1
            clicks, drops = click_count(x, sr), digital_dropouts(x, sr)
            if clicks or drops:
                bad += 1
                evidence.append(ev(t.start, t.end, f"{clicks} click(s), {len(drops)} dropout(s)"))
        return self.ratio(bad, n, method="proxy", evidence=evidence)


@register
class AudioDropoutRate(Metric):
    id = "audio_dropout_rate"
    scale = 1.0
    priority = 35
    name = "Audio dropout rate"
    category = "Speech output"
    question = "How often the voice cuts out."
    value_range = "≥0 per minute; ideal ~0."
    formula = "Missing or corrupted playback intervals / Playback minutes"
    terminology = "Abrupt near-digital-silence gaps (15–250 ms) cutting through AI speech."
    unit = "/min"
    direction = "lower"
    good, bad = 0.1, 1.0

    def compute(self, ctx):
        sr = ctx.audio.native_sr
        drops, evidence = 0, []
        for t, x in _ai_turn_audio(ctx):
            for s, e in digital_dropouts(x, sr):
                drops += 1
                evidence.append(ev(t.start + s, t.start + e, f"{(e - s) * 1000:.0f} ms gap"))
        minutes = ctx.timeline.speech_time("ai") / 60
        return self.ratio(drops, minutes, evidence=evidence[:50])


@register
class ClippingRate(Metric):
    id = "clipping_rate"
    priority = 36
    name = "Clipping rate"
    category = "Speech output"
    question = "Does the voice distort at loud moments?"
    value_range = "0–100%; ideal ~0%."
    formula = "Clipped audio samples / Total samples × 100"
    terminology = "Clipped = consecutive samples at digital full scale, measured over AI speech."
    direction = "lower"
    good, bad = 0.01, 0.1

    def compute(self, ctx):
        clipped = total = 0
        peak = 0.0
        evidence = []
        for t, x in _ai_turn_audio(ctx):
            c = clipped_samples(x)
            clipped += c
            total += x.size
            peak = max(peak, float(np.max(np.abs(x))) if x.size else 0.0)
            if c:
                evidence.append(ev(t.start, t.end, f"{c} clipped samples"))
        return self.ratio(clipped, total, evidence=evidence,
                          details={"peak_dbfs": round(20 * np.log10(peak + 1e-12), 2)})


@register
class LoudnessConsistency(Metric):
    id = "loudness_consistency"
    priority = 38
    name = "Loudness consistency"
    category = "Speech output"
    question = "Does volume jump unexpectedly?"
    value_range = "≥0 LU; lower variation generally better; avoid flattening intentional expression."
    formula = "SD(integrated loudness per AI turn, LUFS)"
    terminology = "ITU-R BS.1770 integrated loudness per AI turn ≥0.4 s."
    unit = "LU"
    direction = "lower"
    good, bad = 2, 4
    aggregation = "mean"

    def compute(self, ctx):
        sr = ctx.audio.native_sr
        per = [(t, integrated_loudness(x, sr)) for t, x in _ai_turn_audio(ctx)]
        per = [(t, l) for t, l in per if l is not None and np.isfinite(l)]
        if len(per) < 2:
            return self.na("Need ≥2 AI turns of ≥0.4 s", kind="not_occurred")
        vals = np.array([l for _, l in per])
        med = np.median(vals)
        outliers = [(t, l) for t, l in per if abs(l - med) > 3]
        return self.ok(float(vals.std(ddof=1)), details={"per_turn_lufs": [round(float(v), 2) for v in vals],
                                                         "median_lufs": round(float(med), 2)},
                       evidence=[ev(t.start, t.end, f"{l:.1f} LUFS (median {med:.1f})") for t, l in outliers])


def ai_speaking_rate(ctx, speaker: str = "ai") -> tuple[float, int, float] | None:
    """(syllables per second of speech, syllables, speech seconds) for one side."""
    tr = ctx.require_transcript()
    text = " ".join(u.text for u in tr.side(speaker))
    seconds = ctx.timeline.speech_time(speaker)
    syl = count_syllables(text)
    if seconds < 1 or syl == 0:
        return None
    return syl / seconds, syl, seconds


@register
class SpeakingRateDeviation(Metric):
    id = "speaking_rate_deviation"
    priority = 39
    name = "Speaking-rate deviation"
    category = "Speech output"
    question = "Does it speak too fast or too slowly?"
    value_range = "≥0 syllables/sec; closer to a matched human range is ideal; no universal cutoff."
    formula = "abs(AI syllables/sec − reference syllables/sec)"
    terminology = "Reference = profile speech.reference_rate_sps if set, otherwise the human caller's own rate in this call."
    unit = "syl/s"
    direction = "lower"
    good, bad = 0.5, 1.5
    requires = (REQ_AUDIO, REQ_TRANSCRIPT)
    aggregation = "mean"

    def compute(self, ctx):
        ai = ai_speaking_rate(ctx, "ai")
        if ai is None:
            return self.na("Not enough AI speech", kind="not_occurred")
        ref = ctx.profile.get("speech.reference_rate_sps")
        source = "profile"
        if ref is None:
            h = ai_speaking_rate(ctx, "human")
            if h is None:
                return self.na("No reference rate (set speech.reference_rate_sps) and too little human speech")
            ref, source = h[0], "human_caller"
        return self.ok(abs(ai[0] - ref), method="proxy",
                       details={"ai_rate_sps": round(ai[0], 2), "reference_sps": round(float(ref), 2), "reference_source": source})
