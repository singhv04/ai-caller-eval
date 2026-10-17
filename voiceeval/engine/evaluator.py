"""Evaluate one recording for a set of metrics, and aggregate many."""
from __future__ import annotations

import logging
import traceback
from types import SimpleNamespace

from ..analysis.context import CallContext, MissingInput
from ..config import Profile
from ..metrics import get_metric
from ..metrics.base import REQ_JUDGE, Metric, MetricResult

log = logging.getLogger(__name__)


def resolve_metrics(metric_ids: list[str], profile: Profile) -> list[Metric]:
    """Requested metrics plus their dependencies, in dependency order."""
    ordered: list[Metric] = []
    seen: set[str] = set()

    def visit(mid: str, stack: tuple = ()):
        if mid in seen:
            return
        if mid in stack:
            raise ValueError(f"Metric dependency cycle: {' -> '.join(stack + (mid,))}")
        m = get_metric(mid)
        for dep in m.dependencies(profile):
            visit(dep, stack + (mid,))
        seen.add(mid)
        ordered.append(m)

    for mid in metric_ids:
        visit(mid)
    return ordered


def evaluate_recording(recording: dict, metric_ids: list[str], profile: Profile,
                       options: dict | None = None) -> tuple[dict[str, dict], dict]:
    ctx = CallContext(recording, profile, options)
    metrics = resolve_metrics(metric_ids, profile)
    for m in metrics:
        if REQ_JUDGE in m.requires or REQ_JUDGE in m.optional:
            sec = m.judge_section(profile)
            if sec is not None:
                ctx.judge_sections[m.id] = sec

    for m in metrics:
        try:
            res = m.compute(ctx)
            res = m.with_band(res, profile)
        except MissingInput as e:
            res = m.na(str(e), kind=e.kind)
        except Exception as e:  # one broken metric must not sink the whole call
            log.exception("metric %s failed on %s", m.id, recording.get("id"))
            res = MetricResult(m.id, "error", reason=f"{type(e).__name__}: {e}",
                               details={"traceback": traceback.format_exc(limit=4)})
        ctx.results[m.id] = res

    return {k: v.to_dict() for k, v in ctx.results.items()}, ctx.analysis_summary()


def aggregate_results(metric_ids: list[str], items: list[dict], profile: Profile, options: dict) -> dict[str, dict]:
    """items: [{recording: {...}, results: {metric_id: result dict}}]"""
    contexts = [
        SimpleNamespace(results={k: MetricResult.from_dict(v) for k, v in it["results"].items()},
                        tags=it["recording"].get("tags") or {}, meta=it["recording"].get("meta") or {},
                        options=options)
        for it in items
    ]
    out = {}
    for mid in metric_ids:
        m = get_metric(mid)
        per = [c.results[mid] for c in contexts if mid in c.results]
        try:
            out[mid] = m.aggregate(per, contexts, profile).to_dict()
        except Exception as e:
            log.exception("aggregate %s failed", mid)
            out[mid] = MetricResult(mid, "error", reason=f"{type(e).__name__}: {e}").to_dict()
    return out
