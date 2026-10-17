"""Dialogue quality & experience metrics (framework priorities 5-8, 25, 26, 28-32)."""
from __future__ import annotations

import re

from .base import MissingInput, REQ_AUDIO, REQ_JUDGE, REQ_TRANSCRIPT, JudgeSection, Metric, register
from .util import BOOL, INT, STR, TURN, arr, ev, obj

OUTCOMES = ["resolved", "partially_resolved", "unresolved", "transferred", "abandoned"]


@register
class TaskCompletion(Metric):
    id = "task_completion_rate"
    priority = 6
    name = "Task completion rate"
    category = "Dialogue quality"
    question = "Can users actually get things done?"
    value_range = "0–100%; higher better; target calibrated by task difficulty."
    formula = "Successfully completed tasks / Attempted tasks × 100"
    terminology = "Each caller goal is a task; success must satisfy the scenario's objective (meta.expected_outcome if given)."
    direction = "higher"
    good, bad = 85, 60
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "Identify every distinct goal the caller tried to accomplish. A goal is completed only if the objective "
            "was actually achieved in the call (confirmed action/answer), not merely promised. If call_metadata has "
            "expected_outcome, use it as the success criterion. Also classify the overall call outcome.",
            obj({"user_goals": arr({"goal": STR, "completed": BOOL, "turn": TURN, "explanation": STR}),
                 "outcome": {"type": "string", "enum": OUTCOMES}}),
        )

    def compute(self, ctx):
        j = ctx.judge_for(self.id)
        goals = j["user_goals"]
        res = self.ratio(sum(g["completed"] for g in goals), len(goals), method="llm_judge",
                         details={"outcome": j["outcome"], "goals": goals},
                         evidence=[{**ctx.turn_span(g["turn"]), "note": f"{'✓' if g['completed'] else '✗ Not achieved:'} {g['goal']}"
                                    + ("" if g["completed"] else f" — {g['explanation']}"), **({"ok": True} if g["completed"] else {})}
                                   for g in goals])
        res.details["outcome"] = j["outcome"]
        return res


@register
class Hallucination(Metric):
    id = "hallucination_rate"
    priority = 7
    name = "Hallucination rate"
    category = "Dialogue quality"
    question = "How often it confidently says made-up things."
    value_range = "0–100%; ideal ~0%, especially in high-stakes cases."
    formula = "Unsupported factual claims / All evaluated factual claims × 100"
    terminology = "Claims checked against the profile knowledge base (authoritative ground truth) and the call itself."
    direction = "lower"
    good, bad = 2, 10
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "Extract every checkable factual claim the AI makes (policies, prices, dates, account facts, procedures, "
            "availability). Verdict: `supported` (backed by the knowledge base or by information established in the "
            "call), `contradicted` (conflicts with them), or `unsupported` (stated as fact but has no basis). "
            "Skip pleasantries and questions.",
            obj({"claims": arr({"turn": TURN, "claim": STR,
                                "verdict": {"type": "string", "enum": ["supported", "unsupported", "contradicted"]},
                                "explanation": STR})}),
        )

    def compute(self, ctx):
        claims = ctx.judge_for(self.id)["claims"]
        bad = [c for c in claims if c["verdict"] != "supported"]
        return self.ratio(len(bad), len(claims), method="llm_judge",
                          details={"claims_evaluated": len(claims), "contradicted": sum(c["verdict"] == "contradicted" for c in claims)},
                          evidence=[{**ctx.turn_span(c["turn"]), "note": f"[{c['verdict']}] {c['claim']} — {c['explanation']}"} for c in bad])


@register
class InstructionAdherence(Metric):
    id = "instruction_adherence"
    priority = 8
    name = "Instruction adherence"
    category = "Dialogue quality"
    question = "Does it follow what the user asked?"
    value_range = "0–100%; near 100% for critical constraints."
    formula = "Compliant responses / Instruction-bearing turns × 100"
    terminology = "Covers user instructions (format, language, scope, pace) and operator instructions from the profile."
    direction = "higher"
    good, bad = 95, 80
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "List each instruction or constraint the AI was given — explicit user requests about how to respond "
            "(repeat, slow down, spell it, language, scope, don't call me again...) and the operator instructions in "
            "the business context that were relevant in this call — and whether the AI complied.",
            obj({"instructions": arr({"turn": TURN, "source": {"type": "string", "enum": ["user", "operator"]},
                                      "instruction": STR, "complied": BOOL, "explanation": STR})}),
        )

    def compute(self, ctx):
        items = ctx.judge_for(self.id)["instructions"]
        return self.ratio(sum(i["complied"] for i in items), len(items), method="llm_judge",
                          evidence=[{**ctx.turn_span(i["turn"]), "note": f"[{i['source']}] {i['instruction']} — {i['explanation']}"}
                                    for i in items if not i["complied"]])


@register
class IntentUnderstanding(Metric):
    id = "intent_understanding_accuracy"
    priority = 25
    name = "Intent understanding accuracy"
    category = "Dialogue quality"
    question = "Does it understand what users want?"
    value_range = "0–100%; ≥95% pilot target for constrained intents."
    formula = "Correctly identified intents / Annotated intents × 100"
    terminology = "LLM judge labels each user intent and compares with what the AI acted on (human labels can override via annotations.intents)."
    direction = "higher"
    good, bad = 95, 80
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "For each user turn that expresses an intent (request, question, complaint, confirmation), state the true "
            "user intent, the intent the AI acted on in its next response, and whether they match.",
            obj({"intents": arr({"turn": TURN, "user_intent": STR, "ai_interpretation": STR, "correct": BOOL})}),
        )

    def compute(self, ctx):
        ann = ctx.annotations.get("intents")
        if ann and "correct" in ann and "total" in ann:
            return self.ratio(ann["correct"], ann["total"], method="annotation")
        items = ctx.judge_for(self.id)["intents"]
        return self.ratio(sum(i["correct"] for i in items), len(items), method="llm_judge",
                          evidence=[{**ctx.turn_span(i["turn"]), "note": f"wanted: {i['user_intent']} / AI: {i['ai_interpretation']}"}
                                    for i in items if not i["correct"]])


@register
class ContextRetention(Metric):
    id = "context_retention_accuracy"
    priority = 26
    name = "Context retention accuracy"
    category = "Dialogue quality"
    question = "Does it remember what was said earlier in the call?"
    value_range = "0–100%; ≥95% for tested context windows."
    formula = "Correct context-dependent answers / Context probes × 100"
    terminology = "Probes = AI turns that require recalling earlier information (names, numbers, prior answers, state after interruption)."
    direction = "higher"
    good, bad = 95, 80
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "Find AI turns whose correct content depends on information from earlier turns (details the caller already "
            "gave, earlier answers, where the conversation was before an interruption). For each, did the AI use the "
            "earlier context correctly (no re-asking for given info, no contradictions)?",
            obj({"probes": arr({"turn": TURN, "refers_to_turn": TURN, "correct": BOOL, "explanation": STR})}),
        )

    def compute(self, ctx):
        items = ctx.judge_for(self.id)["probes"]
        return self.ratio(sum(i["correct"] for i in items), len(items), method="llm_judge",
                          evidence=[{**ctx.turn_span(i["turn"]), "note": i["explanation"]} for i in items if not i["correct"]])


@register
class ClarificationAppropriateness(Metric):
    id = "clarification_appropriateness"
    priority = 28
    name = "Clarification appropriateness"
    category = "Dialogue quality"
    question = "Does it ask sensible follow-up questions?"
    value_range = "0–100%; evaluate ambiguity balance."
    formula = "Correct clarification decisions / Ambiguous or clear cases × 100"
    terminology = "Correct = asks when the request is ambiguous, does not ask when it is already clear."
    direction = "higher"
    good, bad = 90, 70
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "For each user request, decide whether it was ambiguous/underspecified or clear, whether the AI asked a "
            "clarifying question, and whether that decision was appropriate.",
            obj({"decisions": arr({"turn": TURN, "situation": {"type": "string", "enum": ["ambiguous", "clear"]},
                                   "asked_clarification": BOOL, "appropriate": BOOL, "explanation": STR})}),
        )

    def compute(self, ctx):
        items = ctx.judge_for(self.id)["decisions"]
        return self.ratio(sum(i["appropriate"] for i in items), len(items), method="llm_judge",
                          details={"ambiguous_cases": sum(i["situation"] == "ambiguous" for i in items)},
                          evidence=[{**ctx.turn_span(i["turn"]), "note": i["explanation"]} for i in items if not i["appropriate"]])


@register
class RepairSuccess(Metric):
    id = "repair_success_rate"
    priority = 29
    name = "Repair success rate"
    category = "Dialogue quality"
    question = "Can it recover gracefully when it gets something wrong?"
    value_range = "0–100%; target ≥90% pilot."
    formula = "Successful recoveries / Misunderstanding or error events × 100"
    terminology = "Recovery must fix the issue within `dialogue.max_repair_turns` turns (profile)."
    direction = "higher"
    good, bad = 90, 70
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "List every misunderstanding or error event (AI misheard/misunderstood, gave a wrong answer, user corrected "
            "it, system error). For each: was it recovered, and how many turns did recovery take?",
            obj({"error_events": arr({"turn": TURN, "description": STR, "recovered": BOOL, "turns_to_recover": INT})}),
        )

    def compute(self, ctx):
        items = ctx.judge_for(self.id)["error_events"]
        if not items:
            return self.na("No misunderstanding/error events in this call", kind="not_occurred", method="llm_judge")
        mx = int(ctx.profile.get("dialogue.max_repair_turns", 3))
        okc = [i for i in items if i["recovered"] and i["turns_to_recover"] <= mx]
        return self.ratio(len(okc), len(items), method="llm_judge",
                          evidence=[{**ctx.turn_span(i["turn"]), "note": f"{'Recovered' if i in okc else 'Not recovered'}: {i['description']}",
                                     **({"ok": True} if i in okc else {})} for i in items])


@register
class TurnsToResolution(Metric):
    id = "turns_to_resolution"
    priority = 30
    name = "Turns to resolution"
    category = "Dialogue quality"
    question = "How many exchanges it takes to solve a problem."
    value_range = "≥1; fewer can be better but not at the expense of accuracy or empathy."
    formula = "Total user + AI turns in completed sessions / Completed sessions"
    terminology = "Turns from the channel timeline; completion from task_completion_rate outcome."
    unit = "turns"
    direction = "none"
    requires = (REQ_AUDIO, REQ_TRANSCRIPT, REQ_JUDGE)
    depends_on = ("task_completion_rate",)
    aggregation = "mean"

    def compute(self, ctx):
        tc = ctx.dependency("task_completion_rate")
        if tc.status != "ok":
            return self.na(f"Task completion unavailable: {tc.reason}", kind=tc.na_kind or "needs_data")
        if tc.details.get("outcome") != "resolved":
            return self.na(f"Session not completed (outcome: {tc.details.get('outcome')})", kind="not_occurred")
        return self.ok(len(ctx.timeline.turns), method="composite")


def _norm_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


@register
class RepetitionRate(Metric):
    id = "repetition_rate"
    priority = 31
    name = "Repetition / redundancy rate"
    category = "Dialogue quality"
    question = "Does AI keep repeating itself?"
    value_range = "0–100%; lower ideal."
    formula = "Unnecessary repeated AI turns / AI turns × 100"
    terminology = "LLM judge labels redundancy; without a judge, near-duplicate AI turns (word Jaccard ≥ 0.75) are counted."
    direction = "lower"
    good, bad = 5, 15
    requires = (REQ_TRANSCRIPT,)
    optional = (REQ_JUDGE,)

    def judge_section(self, profile):
        return JudgeSection(
            "List AI turns that unnecessarily repeat content the AI already said (not counting legitimate "
            "confirmations/read-backs or repeats the user asked for).",
            obj({"redundant_turns": arr({"turn": TURN, "repeats_turn": TURN, "explanation": STR})}),
        )

    def compute(self, ctx):
        ai = [r for r in ctx.turn_texts if r["speaker"] == "ai"]
        try:
            items = ctx.judge_for(self.id)["redundant_turns"]
            turns = {i["turn"] for i in items}
            return self.ratio(len(turns), len(ai), method="llm_judge",
                              evidence=[{**ctx.turn_span(i["turn"]), "note": i["explanation"]} for i in items])
        except MissingInput:
            pass
        dup, evidence = 0, []
        for i, r in enumerate(ai):
            w = _norm_words(r["text"])
            if len(w) < 4:
                continue
            for prev in ai[:i]:
                pw = _norm_words(prev["text"])
                if pw and len(w & pw) / len(w | pw) >= 0.75:
                    dup += 1
                    evidence.append({**ctx.turn_span(r["index"]), "note": f"near-duplicate of T{prev['index']}"})
                    break
        return self.ratio(dup, len(ai), method="proxy", evidence=evidence)


@register
class AbandonmentRate(Metric):
    id = "conversation_abandonment_rate"
    priority = 32
    name = "Conversation abandonment rate"
    category = "Dialogue quality"
    question = "How often people give up before finishing."
    value_range = "0–100%; lower better; interpret with task difficulty."
    formula = "User-abandoned sessions / Started sessions × 100"
    terminology = "Per-call flag aggregated across selected recordings; intentional quick exits excluded."
    direction = "lower"
    good, bad = 5, 20
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "Did the caller give up / hang up before their need was handled (frustration, long waits, loops)? "
            "Mark intentional_quick_exit if they simply did not want the service (wrong number, not interested).",
            obj({"abandoned": BOOL, "intentional_quick_exit": BOOL, "reason": STR}),
        )

    def compute(self, ctx):
        j = ctx.judge_for(self.id)
        if j["intentional_quick_exit"]:
            return self.na("Intentional quick exit — excluded", kind="not_occurred", method="llm_judge")
        last = ctx.timeline.turns[-1] if ctx.timeline.turns else None
        evidence = [ev(last.start, last.end, j["reason"])] if (j["abandoned"] and last) else []
        return self.ratio(int(j["abandoned"]), 1, method="llm_judge", details={"reason": j["reason"]}, evidence=evidence)


@register
class EndToEndSuccess(Metric):
    id = "end_to_end_success_rate"
    priority = 5
    name = "End-to-end successful call rate"
    category = "Experience"
    question = "Did the entire experience work well?"
    value_range = "0–100%; set gates before testing."
    formula = "Calls meeting task, quality, latency and safety gates / All calls × 100"
    terminology = ("Gates are configured in the profile (`gates:`). A call succeeds only if every evaluated gate passes. "
                   "Unevaluable gates are skipped, fail the call (`strict`), or make the call N/A (`required`).")
    direction = "higher"
    good, bad = 80, 50
    requires = (REQ_AUDIO, REQ_TRANSCRIPT, REQ_JUDGE)

    def dependencies(self, profile) -> tuple[str, ...]:
        return tuple(dict.fromkeys(g["metric"] for g in profile.get("gates", []) or []))

    def compute(self, ctx):
        gates = ctx.profile.get("gates", []) or []
        if not gates:
            return self.na("Profile defines no gates", kind="needs_config")
        report, passed_all, evaluated = [], True, 0
        for g in gates:
            r = ctx.results.get(g["metric"])
            label = g.get("label") or g["metric"]
            if r is None or r.status != "ok":
                reason = r.reason if r else "not computed"
                if g.get("required"):
                    return self.na(f"Required gate '{label}' could not be evaluated: {reason}",
                                   kind=(r.na_kind if r else None) or "needs_data", details={"gates": report})
                if g.get("strict"):
                    passed_all = False
                report.append({"gate": label, "status": "fail" if g.get("strict") else "skipped", "reason": reason})
                continue
            v = r.details.get(g["field"]) if g.get("field") else r.value
            ok = v is not None and (("max" not in g or v <= g["max"]) and ("min" not in g or v >= g["min"]))
            if "equals" in g:
                ok = v == g["equals"]
            evaluated += 1
            passed_all &= ok
            report.append({"gate": label, "status": "pass" if ok else "fail", "value": v,
                           "rule": {k: g[k] for k in ("min", "max", "equals") if k in g}})
        if evaluated == 0:
            return self.na("No gate could be evaluated", kind="needs_config", details={"gates": report})
        return self.ratio(int(passed_all), 1, method="composite", details={"gates": report})
