"""Prosody & perceptual metrics (framework priorities 33, 37, 40-46, 52-54).

Listener-based metrics (MOS, human-likeness, ...) are read from
`meta.annotations.ratings.<key>` — lists of 1-5 scores from blind raters.
Signal-based prosody metrics are computed from the AI channel + word timings.
"""
from __future__ import annotations

import re

import numpy as np

from ..audio.features import count_syllables, dtw_cost, f0_contour, slice_audio, zscore_voiced
from .base import REQ_ANNOTATIONS, REQ_AUDIO, REQ_TRANSCRIPT, Metric, register
from .util import ev, mean_ci

import soundfile as sf


class RatingMetric(Metric):
    """Mean of blind listener ratings on a 1-5 scale."""

    unit = "score"
    direction = "higher"
    good, bad = 4, 2.5
    requires = (REQ_ANNOTATIONS,)
    aggregation = "samples"
    rating_key = ""
    value_range = "1–5; low 1–2, medium 3, high 4–5; ideal ≥4."

    def summarize_samples(self, samples):
        d = mean_ci(samples)
        return d["mean"], d

    def compute(self, ctx):
        ratings = (ctx.annotations.get("ratings") or {}).get(self.rating_key)
        if not ratings:
            return self.na(f"No listener ratings in annotations.ratings.{self.rating_key}")
        vals = [float(r) for r in ratings]
        v, d = self.summarize_samples(vals)
        return self.ok(v, method="annotation", samples=vals, details=d)


@register
class VoiceConsistency(RatingMetric):
    id = "voice_consistency"
    priority = 37
    name = "Voice consistency"
    category = "Speech output"
    question = "Does it keep sounding like the same speaker?"
    formula = "Mean listener consistency rating (1–5)"
    terminology = "Same voice identity, accent and timbre across turns; blind ratings."
    rating_key = "voice_consistency"


@register
class ProsodyMOS(RatingMetric):
    id = "prosody_naturalness_mos"
    priority = 40
    name = "Prosody naturalness MOS"
    category = "Perceptual"
    question = "Overall, does the *way* it speaks sound human?"
    formula = "Σ listener prosody ratings / Number of ratings"
    terminology = "Blind listeners rate pitch, stress, rhythm and pauses together on a 1–5 scale."
    rating_key = "prosody_mos"


@register
class ExpressiveAppropriateness(RatingMetric):
    id = "expressive_appropriateness"
    priority = 43
    name = "Expressive appropriateness"
    category = "Acoustic / prosody"
    question = "Does it sound cheerful, calm, serious, or empathetic at the right time?"
    formula = "Mean listener rating (1–5)"
    terminology = "Listeners rate whether emotional delivery matches the context."
    rating_key = "expressive_appropriateness"


@register
class NaturalnessMOS(RatingMetric):
    id = "overall_naturalness_mos"
    priority = 52
    name = "Overall voice naturalness MOS"
    category = "Perceptual"
    question = "Does the voice feel natural overall?"
    formula = "Σ overall naturalness ratings / Number of ratings"
    terminology = "Blind listeners score complete clips/interactions; keep scale and listening setup consistent."
    rating_key = "naturalness_mos"


@register
class HumanLikeness(RatingMetric):
    id = "human_likeness_rating"
    priority = 54
    name = "Human-likeness rating"
    category = "Perceptual"
    question = "How much does it sound like a person rather than a machine?"
    formula = "Σ human-likeness ratings / Number of ratings"
    terminology = "Blind 1–5 rating of perceived human-likeness, distinct from usefulness or correctness."
    rating_key = "human_likeness"


@register
class ABPreference(Metric):
    id = "ab_preference_win_rate"
    priority = 53
    name = "A/B preference win rate"
    category = "Perceptual"
    question = "Which of two voices do people actually prefer?"
    value_range = "0–100%; <50 loses, 50 parity, >50 wins."
    formula = "AI-A preferred / Non-tied A/B judgments × 100"
    terminology = "annotations.ab_preference = {a_wins, b_wins, ties}; this recording is system A vs a baseline."
    direction = "higher"
    good, bad = 55, 45
    requires = (REQ_ANNOTATIONS,)

    def compute(self, ctx):
        ab = ctx.annotations.get("ab_preference")
        if not ab:
            return self.na("No annotations.ab_preference")
        return self.ratio(ab.get("a_wins", 0), ab.get("a_wins", 0) + ab.get("b_wins", 0), method="annotation",
                          details={"ties": ab.get("ties", 0)})


@register
class PronunciationAccuracy(Metric):
    id = "pronunciation_accuracy"
    scale = 1.0
    priority = 33
    name = "Pronunciation accuracy"
    category = "Acoustic / quality"
    question = "Does it pronounce names and words correctly?"
    value_range = "0–1; low <0.9, medium 0.9–0.97, high >0.97 illustrative; ideal near 1."
    formula = "1 − (Pronunciation errors / Evaluated words)"
    terminology = "annotations.pronunciation = {errors, evaluated_words} from independent annotators."
    unit = ""
    direction = "higher"
    good, bad = 0.97, 0.9
    requires = (REQ_ANNOTATIONS,)

    def compute(self, ctx):
        p = ctx.annotations.get("pronunciation")
        if not p:
            return self.na("No annotations.pronunciation")
        words = p["evaluated_words"]
        return self.ratio(words - p["errors"], words, method="annotation")


@register
class EmphasisAccuracy(Metric):
    id = "emphasis_stress_accuracy"
    priority = 42
    name = "Emphasis / stress accuracy"
    category = "Acoustic / prosody"
    question = "Does the voice emphasize the important word?"
    value_range = "0–100%; low <60, medium 60–84, high ≥85 illustrative; ideal ≥90% after validation."
    formula = "Correct prominent targets / Annotated targets × 100"
    terminology = "annotations.emphasis = {correct, total}; listener-verified prominence placement."
    direction = "higher"
    good, bad = 85, 60
    requires = (REQ_ANNOTATIONS,)

    def compute(self, ctx):
        e = ctx.annotations.get("emphasis")
        if not e:
            return self.na("No annotations.emphasis")
        return self.ratio(e["correct"], e["total"], method="annotation")


@register
class IntonationSimilarity(Metric):
    id = "intonation_contour_similarity"
    priority = 41
    name = "Intonation contour similarity"
    category = "Acoustic / prosody"
    question = "Does pitch rise and fall at sensible points in the sentence?"
    value_range = "≥0; low cost = closer to reference; valid alternatives may differ."
    formula = "DTW(z(F0_AI), z(F0_human)) / L"
    terminology = ("annotations.prosody_references = [{start, end, reference_path}] — AI clip in this call and a human "
                   "reading of the same text. Best (minimum) cost across references per clip.")
    unit = "cost"
    direction = "lower"
    good, bad = 0.5, 1.0
    requires = (REQ_AUDIO, REQ_ANNOTATIONS)
    aggregation = "mean"

    def compute(self, ctx):
        refs = ctx.annotations.get("prosody_references")
        if not refs:
            return self.na("No annotations.prosody_references")
        a = ctx.audio
        costs, evidence = [], []
        for r in refs:
            clip = slice_audio(a.ai, a.sr, r["start"], r["end"])
            z_ai = zscore_voiced(f0_contour(clip, a.sr))
            paths = r.get("reference_paths") or [r["reference_path"]]
            best = None
            for p in paths:
                data, sr = sf.read(ctx.storage.local_path(p) if not p.startswith("/") else p, dtype="float32", always_2d=True)
                z_ref = zscore_voiced(f0_contour(data[:, 0], sr))
                if z_ai.size and z_ref.size:
                    c, L = dtw_cost(z_ai[::2], z_ref[::2])
                    best = c / L if best is None else min(best, c / L)
            if best is not None:
                costs.append(best)
                evidence.append(ev(r["start"], r["end"], f"DTW cost {best:.2f}"))
        if not costs:
            return self.na("Could not extract pitch contours", kind="not_occurred")
        return self.ok(float(np.mean(costs)), details={"per_clip": costs}, evidence=evidence)


def _ai_words_by_turn(ctx):
    tr = ctx.require_transcript()
    words = [w for u in tr.side("ai") for w in u.words]
    if not words:
        return None
    turns = ctx.timeline.turns_of("ai")
    out = []
    for t in turns:
        tw = [w for w in words if t.start - 0.15 <= w.start <= t.end + 0.15]
        if len(tw) > 1:
            out.append((t, tw))
    if not turns:  # no VAD turns: treat each utterance as a turn
        out = [(u, u.words) for u in tr.side("ai") if len(u.words) > 1]
    return out


@register
class PausePlacementF1(Metric):
    id = "pause_placement_f1"
    priority = 44
    name = "Pause placement F1"
    category = "Acoustic / prosody"
    question = "Does it pause where a person would naturally pause?"
    value_range = "0–1; low <0.6, medium 0.6–0.84, high ≥0.85 illustrative."
    formula = "2PR/(P+R); P=TP/(TP+FP); R=TP/(TP+FN)"
    terminology = ("Proxy: expected pauses = punctuation (clause/sentence) boundaries in the AI text; detected pauses = "
                   "inter-word gaps ≥ pause threshold from word timestamps. annotations.pause_placement={tp,fp,fn} overrides.")
    unit = ""
    direction = "higher"
    good, bad = 0.85, 0.6
    requires = (REQ_TRANSCRIPT,)
    aggregation = "custom"

    def _f1(self, tp, fp, fn):
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        return (2 * p * r / (p + r) if p + r else 0.0), p, r

    def compute(self, ctx):
        ann = ctx.annotations.get("pause_placement")
        if ann:
            tp, fp, fn = ann["tp"], ann["fp"], ann["fn"]
            method = "annotation"
        else:
            groups = _ai_words_by_turn(ctx)
            if not groups:
                return self.na("No word-level timestamps for the AI channel")
            thr = float(ctx.profile.get("speech.pause_threshold_ms", 200)) / 1000
            tp = fp = fn = 0
            for _, words in groups:
                for w, nxt in zip(words, words[1:]):
                    expected = bool(re.search(r"[,.;:?!—–]$", w.text))
                    detected = (nxt.start - w.end) >= thr
                    tp += expected and detected
                    fp += detected and not expected
                    fn += expected and not detected
            method = "proxy"
        if tp + fp + fn == 0:
            return self.na("No pauses expected or detected", kind="not_occurred")
        f1, p, r = self._f1(tp, fp, fn)
        return self.ok(f1, method=method, details={"tp": tp, "fp": fp, "fn": fn, "precision": round(p, 3), "recall": round(r, 3)})

    def aggregate(self, results, contexts=None, profile=None):
        ok = [r for r in results if r.status == "ok"]
        if not ok:
            return self.na("No applicable recordings", kind="not_occurred")
        tp, fp, fn = (sum(r.details[k] for r in ok) for k in ("tp", "fp", "fn"))
        f1, p, r = self._f1(tp, fp, fn)
        return self.with_band(self.ok(f1, method=ok[0].method, details={"tp": tp, "fp": fp, "fn": fn,
                                                                        "precision": round(p, 3), "recall": round(r, 3)}), profile)


@register
class ArticulationRate(Metric):
    id = "articulation_rate"
    scale = 1.0
    priority = 45
    name = "Articulation rate"
    category = "Acoustic / prosody"
    question = "How fast the voice speaks while actually talking."
    value_range = ">0 syllables/s; ideal matches matched human rate and listener comprehension."
    formula = "Spoken syllables / Speaking time excluding silence"
    terminology = "Syllables from the AI transcript; speaking time = voiced time on the AI channel (pauses excluded)."
    unit = "syl/s"
    direction = "none"
    requires = (REQ_AUDIO, REQ_TRANSCRIPT)

    def compute(self, ctx):
        tr = ctx.require_transcript()
        syl = count_syllables(" ".join(u.text for u in tr.side("ai")))
        secs = ctx.timeline.speech_time("ai")
        return self.ratio(syl, secs, method="proxy")


@register
class PauseDuration(Metric):
    id = "pause_duration"
    priority = 46
    name = "Pause duration"
    category = "Acoustic / prosody"
    question = "Are pauses comfortably long, not awkwardly short or long?"
    value_range = "≥0 s; ideal depends on clause boundary and task."
    formula = "Σ pause durations / number of pauses"
    terminology = "Pauses = silences ≥ threshold (default 200 ms) inside AI turns, from the AI channel VAD."
    unit = "s"
    direction = "none"
    aggregation = "samples"

    def summarize_samples(self, samples):
        a = np.asarray(samples)
        return float(a.mean()), {"mean_s": round(float(a.mean()), 3), "median_s": round(float(np.median(a)), 3),
                                 "max_s": round(float(a.max()), 3), "n": int(a.size)}

    def compute(self, ctx):
        thr = float(ctx.profile.get("speech.pause_threshold_ms", 200)) / 1000
        pauses = []
        for t in ctx.timeline.turns_of("ai"):
            segs = t.segments
            pauses += [b.start - a.end for a, b in zip(segs, segs[1:]) if b.start - a.end >= thr]
        if not pauses:
            return self.na("No intra-turn pauses on the AI channel", kind="not_occurred")
        v, d = self.summarize_samples(pauses)
        return self.ok(v, samples=pauses, details=d)
