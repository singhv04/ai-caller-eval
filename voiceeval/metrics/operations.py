"""Experience & operations metrics fed by metadata (framework priorities 55-57)."""
from __future__ import annotations

from .base import REQ_BATCH, REQ_JUDGE, REQ_METADATA, Metric, register


@register
class CSAT(Metric):
    id = "csat"
    priority = 55
    name = "User satisfaction (CSAT)"
    category = "Experience"
    question = "Did users like the interaction?"
    value_range = "0–100%; benchmark against comparable service."
    formula = "Positive survey responses / All valid survey responses × 100"
    terminology = "meta.survey = {score, scale} (top-two box is positive) or {positive, total}."
    direction = "higher"
    good, bad = 80, 60
    requires = (REQ_METADATA,)

    def compute(self, ctx):
        s = ctx.meta.get("survey")
        if not s:
            return self.na("No post-call survey in meta.survey")
        if "positive" in s:
            return self.ratio(s["positive"], s["total"], method="metadata")
        scale = int(s.get("scale", 5))
        return self.ratio(int(float(s["score"]) >= scale - 1), 1, method="metadata", details={"score": s["score"]})


@register
class Availability(Metric):
    id = "availability_uptime"
    priority = 56
    name = "Availability / uptime"
    category = "Operations"
    question = "Is the voice assistant reliably available?"
    value_range = "0–100%; SLA-dependent, e.g. ≥99.9%."
    formula = "Available service minutes / Scheduled service minutes × 100"
    terminology = "Service-level, not recording-level: pass run options ops.available_minutes / ops.scheduled_minutes."
    direction = "higher"
    good, bad = 99.9, 99.0
    scope = "batch"
    aggregation = "custom"
    requires = (REQ_METADATA, REQ_BATCH)

    def compute(self, ctx):
        return self.na("Batch metric — computed from run options", kind="needs_more_calls")

    def aggregate(self, results, contexts=None, profile=None):
        ops = (contexts[0].options.get("ops") if contexts else None) or {}
        if not ops.get("scheduled_minutes"):
            return self.na("Provide run options ops.available_minutes and ops.scheduled_minutes (from monitoring)")
        return self.with_band(self.ratio(ops["available_minutes"], ops["scheduled_minutes"], method="metadata"), profile)


@register
class CostPerCompleted(Metric):
    id = "cost_per_completed_conversation"
    scale = 1.0
    priority = 57
    name = "Cost per completed conversation"
    category = "Operations"
    question = "How much does a successful call cost?"
    value_range = "≥0 currency; lower better at fixed quality."
    formula = "Total voice system cost / Successfully completed conversations"
    terminology = "meta.cost (number or {total}) incl. ASR, LLM, TTS, infra, telephony; completion from task_completion_rate."
    unit = "$"
    direction = "lower"
    requires = (REQ_METADATA, REQ_JUDGE)
    depends_on = ("task_completion_rate",)
    aggregation = "custom"

    def _cost(self, ctx):
        c = ctx.meta.get("cost")
        if isinstance(c, dict):
            return float(c.get("total", sum(v for v in c.values() if isinstance(v, (int, float)))))
        return float(c) if c is not None else None

    def compute(self, ctx):
        cost = self._cost(ctx)
        if cost is None:
            return self.na("No meta.cost on this recording")
        tc = ctx.dependency("task_completion_rate")
        completed = int(tc.status == "ok" and tc.details.get("outcome") == "resolved")
        res = self.ratio(cost, completed, method="composite")
        res.numerator, res.denominator = cost, completed  # keep cost for pooled aggregation even when not completed
        return res

    def aggregate(self, results, contexts=None, profile=None):
        withcost = [r for r in results if r.numerator is not None]
        if not withcost:
            return self.na("No recordings with meta.cost")
        num = sum(r.numerator for r in withcost)
        den = sum(r.denominator or 0 for r in withcost)
        if den == 0:
            return self.na(f"No completed conversations (total cost {num:.2f})", kind="not_occurred")
        return self.ratio(num, den, method="composite", details={"recordings": len(withcost)})
