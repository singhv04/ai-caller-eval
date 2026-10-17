"""Robustness metrics (framework priorities 47-51).

47/48 are batch metrics: they compare WER across groups of recordings using
recording tags (`noise_condition: clean|noisy`, `accent: <group>`).
"""
from __future__ import annotations

from collections import defaultdict

from .base import REQ_BATCH, REQ_JUDGE, REQ_METADATA, REQ_TRANSCRIPT, JudgeSection, Metric, register
from .util import BOOL, STR, TURN, arr, ev, obj

FAILURE_END_REASONS = {"error", "dropped", "timeout", "crash", "frozen", "failed", "disconnected"}


def _pooled_wer(items) -> float | None:
    num = sum(r.numerator or 0 for r in items)
    den = sum(r.denominator or 0 for r in items)
    return num / den * 100 if den else None


class _BatchWER(Metric):
    category = "Robustness"
    unit = "pp"
    direction = "lower"
    scope = "batch"
    aggregation = "custom"
    requires = (REQ_METADATA, REQ_BATCH)
    depends_on = ("word_error_rate",)
    tag = ""

    def compute(self, ctx):
        return self.na("Batch metric — computed across the selected recordings", kind="needs_more_calls")

    def groups(self, contexts) -> dict[str, list]:
        g = defaultdict(list)
        for c in contexts or []:
            r = c.results.get("word_error_rate")
            key = c.tags.get(self.tag)
            if r is not None and r.status == "ok" and key:
                g[str(key)].append(r)
        return g


@register
class NoiseRobustnessDelta(_BatchWER):
    id = "noise_robustness_delta"
    priority = 47
    name = "Noise robustness delta"
    question = "How much background noise hurts understanding."
    value_range = "Can be negative; near 0 ideal; smaller positive gap better."
    formula = "WER(noisy) − WER(clean)"
    terminology = "Recordings tagged noise_condition=clean / noisy; pooled WER per group; percentage-point gap."
    good, bad = 2, 10
    tag = "noise_condition"

    def aggregate(self, results, contexts=None, profile=None):
        g = self.groups(contexts)
        if "clean" not in g or "noisy" not in g:
            return self.na("Need recordings tagged noise_condition=clean and =noisy with WER available", kind="needs_more_calls")
        clean, noisy = _pooled_wer(g["clean"]), _pooled_wer(g["noisy"])
        return self.with_band(self.ok(noisy - clean, details={"wer_clean": clean, "wer_noisy": noisy,
                                                              "n_clean": len(g["clean"]), "n_noisy": len(g["noisy"])}), profile)


@register
class AccentPerformanceGap(_BatchWER):
    id = "accent_performance_gap"
    priority = 48
    name = "Accent performance gap"
    question = "Does it work equally well across accents?"
    value_range = "≥0 percentage points; lower ideal; report each group separately."
    formula = "max(group error rate) − min(group error rate)"
    terminology = "Recordings tagged accent=<group>; pooled WER per group."
    good, bad = 3, 10
    tag = "accent"

    def aggregate(self, results, contexts=None, profile=None):
        g = self.groups(contexts)
        if len(g) < 2:
            return self.na("Need ≥2 accent groups (tag `accent`) with WER available", kind="needs_more_calls")
        per = {k: _pooled_wer(v) for k, v in g.items()}
        return self.with_band(self.ok(max(per.values()) - min(per.values()),
                                      details={"wer_by_group": per, "n_by_group": {k: len(v) for k, v in g.items()}}), profile)


@register
class MultilingualAccuracy(Metric):
    id = "multilingual_accuracy"
    priority = 49
    name = "Multilingual / code-switch accuracy"
    category = "Robustness"
    question = "Can it handle users switching languages?"
    value_range = "0–100%; validate each language pair."
    formula = "Correct multilingual turns / Tested multilingual turns × 100"
    terminology = "Switches within sentences and across turns."
    direction = "higher"
    good, bad = 90, 70
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "List user turns that use a different language than the call's main language, or mix languages. For each, "
            "did the AI understand and respond appropriately (right language per policy, correct content)? "
            "Empty list if the whole call is in one language.",
            obj({"multilingual_turns": arr({"turn": TURN, "languages": STR, "handled_correctly": BOOL, "explanation": STR})}),
        )

    def compute(self, ctx):
        items = ctx.judge_for(self.id)["multilingual_turns"]
        if not items:
            return self.na("Single-language call", kind="not_occurred", method="llm_judge")
        return self.ratio(sum(i["handled_correctly"] for i in items), len(items), method="llm_judge",
                          evidence=[{**ctx.turn_span(i["turn"]), "note": f"{i['languages']}: {i['explanation']}"} for i in items])


@register
class SessionFailureRate(Metric):
    id = "session_failure_rate"
    priority = 50
    name = "Session failure rate"
    category = "Robustness"
    question = "How often calls break entirely."
    value_range = "0–100%; ideal ~0%."
    formula = "Crashed or unrecoverable sessions / All sessions × 100"
    terminology = ("meta.end_reason when provided (error/dropped/timeout/...); otherwise proxy: the call ends on an "
                   "unanswered user turn followed by long silence (frozen agent).")
    direction = "lower"
    good, bad = 1, 5
    optional = (REQ_METADATA,)

    def compute(self, ctx):
        reason = str(ctx.meta.get("end_reason", "")).lower()
        if reason:
            failed = reason in FAILURE_END_REASONS
            return self.ratio(int(failed), 1, method="metadata", details={"end_reason": reason})
        tl = ctx.timeline
        if not tl.turns:
            return self.ratio(1, 1, method="proxy", details={"why": "no speech at all"})
        last = tl.turns[-1]
        trailing = tl.duration - max(s.end for v in tl.segments.values() for s in v)
        frozen = last.speaker == "human" and trailing >= tl.p.dead_air_s
        return self.ratio(int(frozen), 1, method="proxy",
                          details={"last_speaker": last.speaker, "trailing_silence_s": round(trailing, 2)},
                          evidence=[ev(last.end, tl.duration, "AI never answered; call froze")] if frozen else [])


@register
class NetworkRecovery(Metric):
    id = "network_interruption_recovery"
    priority = 51
    name = "Recovery after network interruption"
    category = "Robustness"
    question = "Does it recover from connection problems?"
    value_range = "0–100%; target ≥95% for supported reconnects."
    formula = "Successfully resumed sessions / Interrupted sessions × 100"
    terminology = ("Interruptions from `network_interruption` events, or mid-call dead air on both channels; resumed "
                   "= both parties speak again afterwards.")
    direction = "higher"
    good, bad = 95, 80

    def compute(self, ctx):
        tl = ctx.timeline
        gaps = [(e["t"], e["t"] + float(e.get("duration", 0))) for e in ctx.events if e.get("type") == "network_interruption"]
        method = "metadata" if gaps else "proxy"
        if not gaps:
            gaps = [(d["start"], d["end"]) for d in tl.dead_air]
        if not gaps:
            return self.na("No network interruption / dead air detected", kind="not_occurred")
        resumed = 0
        evidence = []
        for s, e in gaps:
            after = [t for t in tl.turns if t.start >= e - 0.1]
            ok = any(t.speaker == "ai" for t in after) and any(t.speaker == "human" for t in after)
            resumed += ok
            evidence.append(ev(s, e, f"{e - s:.1f} s of silence on both sides; conversation " + ("resumed" if ok else "did not resume"), ok=ok))
        return self.ratio(int(resumed > 0 and resumed == len(gaps)), 1, method=method, evidence=evidence,
                          details={"interruptions": len(gaps), "resumed": resumed})
