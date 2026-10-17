"""Conversational metrics (framework priorities 17, 18, 27)."""
from __future__ import annotations


from .base import MissingInput, REQ_JUDGE, REQ_TRANSCRIPT, JudgeSection, Metric, register
from .util import SCORE_1_5, STR, TURN, arr, ev, mean_ci, obj


@register
class TurnTakingAppropriateness(Metric):
    id = "turn_taking_appropriateness"
    priority = 17
    name = "Turn-taking appropriateness"
    category = "Conversational"
    question = "Does it know when to speak and when to listen?"
    value_range = "0–100%; low <70, medium 70–89, high ≥90 illustrative; ideal ≥95% after validation."
    formula = "Appropriate handoffs / Total handoffs × 100"
    terminology = ("Automated proxy for the human rating: a human→AI handoff is appropriate when the AI waits for the "
                   "user to finish and replies within the max response gap; a barge-in is appropriate when the AI "
                   "yields within the max-yield window.")
    direction = "higher"
    good, bad = 90, 70

    def compute(self, ctx):
        tl = ctx.timeline
        p = tl.p
        evidence = []
        total = appropriate = 0
        for r in tl.responses:
            total += 1
            good = (not r["overlapped"]) and r["gap_ms"] <= p.max_response_gap_s * 1000
            appropriate += good
            if not good:
                why = ("AI started talking before the caller had finished" if r["overlapped"]
                       else f"Slow reply: caller waited {r['gap_ms'] / 1000:.2f} s")
                evidence.append(ev(r["human_end"], r["ai_start"], why))
        for b in tl.barge_ins:
            total += 1
            appropriate += b["stopped"]
            if not b["stopped"]:
                evidence.append(ev(b["onset"], b["ai_stop"], f"Caller interrupted but the AI kept talking for {b['stop_latency_ms'] / 1000:.1f} s"))
        return self.ratio(appropriate, total, method="proxy", evidence=evidence)


@register
class UnwantedOverlapTime(Metric):
    id = "unwanted_overlap_time"
    priority = 18
    name = "Unwanted overlap rate (talk time)"
    category = "Conversational"
    question = "Does it talk over you unnecessarily?"
    value_range = "0–100%; ideal near 0%, excluding acceptable backchannels."
    formula = "Unwanted overlapping talk time / Total conversation time × 100"
    terminology = ("Unwanted = AI starting over the user, plus AI speech continuing past the barge-in yield target "
                   "after the user interrupts. Backchannels excluded.")
    direction = "lower"
    good, bad = 1, 5

    def compute(self, ctx):
        tl = ctx.timeline
        s, e = tl.conversation_span
        span = e - s
        unwanted = [o for o in tl.overlaps if o["unwanted_s"] > 0]
        total_unwanted = sum(o["unwanted_s"] for o in unwanted)
        return self.ratio(total_unwanted, span, details={
            "unwanted_overlap_s": round(total_unwanted, 2), "conversation_s": round(span, 2),
            "all_overlap_s": round(sum(o["end"] - o["start"] for o in tl.overlaps), 2)},
            evidence=[ev(o["start"], o["end"], {"ai_talk_over": f"AI talked over the caller ({o['end'] - o['start']:.1f} s overlap)",
                                                "barge_in": f"AI kept talking {o['unwanted_s']:.1f} s after the caller cut in"}.get(o["kind"], o["kind"]))
                      for o in unwanted])


@register
class ContextualResponseQuality(Metric):
    id = "contextual_response_quality"
    priority = 27
    name = "Contextual response quality"
    category = "Conversational"
    question = "Does it respond like someone following the conversation?"
    value_range = "1–5; low 1–2, medium 3, high 4–5; ideal ≥4."
    formula = "Mean rubric rating (1–5)"
    terminology = "LLM judge rates every AI turn for relevance, coherence, continuity and conversational fit."
    unit = "score"
    direction = "higher"
    good, bad = 4, 2.5
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)
    aggregation = "samples"

    def summarize_samples(self, samples):
        d = mean_ci(samples)
        return d["mean"], d

    def judge_section(self, profile):
        return JudgeSection(
            "Rate EVERY AI turn 1-5 for how well it fits the conversation so far: relevance to the latest user turn, "
            "coherence, continuity with earlier turns, and natural conversational fit for a phone call "
            "(5 = excellent, 3 = acceptable but flawed, 1 = off-topic or incoherent). Give a short note for scores ≤3.",
            obj({"ai_turn_ratings": arr({"turn": TURN, "score": SCORE_1_5, "note": STR})}),
        )

    def compute(self, ctx):
        rows = ctx.judge_for(self.id)["ai_turn_ratings"]
        if not rows:
            raise MissingInput("Judge rated no AI turns")
        scores = [r["score"] for r in rows]
        v, d = self.summarize_samples(scores)
        low = [r for r in rows if r["score"] <= 3]
        return self.ok(v, method="llm_judge", samples=scores, details=d,
                       evidence=[{**ctx.turn_span(r["turn"]), "note": f"{r['score']}/5 — {r['note']}"} for r in low])
