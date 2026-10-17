"""Metric plugin framework.

A metric is a small class: catalogue info (from the evaluation framework),
what inputs it needs, a `compute(ctx)` that scores one call, and an
aggregation rule for scoring a batch of calls. Register it with `@register`
and it shows up in the API, the UI and the CLI automatically.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

if TYPE_CHECKING:
    from ..analysis.context import CallContext
    from ..config import Profile

# Requirement keys a metric can declare. The UI shows them as badges and the
# engine uses them to explain why a metric was skipped.
REQ_AUDIO = "audio"
REQ_TRANSCRIPT = "transcript"
REQ_JUDGE = "llm_judge"
REQ_ANNOTATIONS = "annotations"
REQ_EVENTS = "event_log"
REQ_METADATA = "metadata"
REQ_BATCH = "multiple_recordings"


# Why a result is empty — drives how the UI explains it.
NA_NOT_OCCURRED = "not_occurred"        # the situation never happened in the call (e.g. no interruptions)
NA_NEEDS_LLM = "needs_llm"              # LLM judge not configured
NA_NEEDS_TRANSCRIPT = "needs_transcript"
NA_NEEDS_DATA = "needs_data"            # metadata / annotations / event logs not supplied
NA_NEEDS_MORE_CALLS = "needs_more_calls"  # batch metric needs more / differently tagged recordings
NA_NEEDS_CONFIG = "needs_config"        # profile lacks the required policy/gate definition


class MissingInput(Exception):
    """Raised by a metric when a required input is unavailable; becomes not_applicable."""

    def __init__(self, message: str, kind: str = NA_NEEDS_DATA):
        super().__init__(message)
        self.kind = kind


@dataclass
class MetricResult:
    metric_id: str
    status: str = "ok"                  # ok | not_applicable | error
    value: float | None = None
    unit: str = ""
    band: str | None = None             # good | warn | bad
    method: str = "measured"            # measured | proxy | llm_judge | annotation | composite | metadata
    numerator: float | None = None
    denominator: float | None = None
    samples: list[float] | None = None
    details: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    na_kind: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["value"] is not None:
            d["value"] = round(float(d["value"]), 4)
        return d

    @staticmethod
    def from_dict(d: dict) -> "MetricResult":
        return MetricResult(**{k: v for k, v in d.items() if k in MetricResult.__dataclass_fields__})


@dataclass
class JudgeSection:
    """A slice of the single per-call LLM judge request owned by one metric."""

    instructions: str
    schema: dict


class Metric:
    # ---- catalogue (from the evaluation framework) ----
    id: ClassVar[str]
    priority: ClassVar[int] = 99
    name: ClassVar[str]
    category: ClassVar[str]
    question: ClassVar[str] = ""          # simple-English meaning
    value_range: ClassVar[str] = ""       # value range / bands / ideal
    formula: ClassVar[str] = ""
    terminology: ClassVar[str] = ""
    unit: ClassVar[str] = "%"
    scale: ClassVar[float] = 100.0        # ratio multiplier (100 for %, 1 for rates/fractions)

    # ---- scoring ----
    direction: ClassVar[str] = "higher"   # higher | lower | none
    good: ClassVar[float | None] = None   # threshold for "good"
    bad: ClassVar[float | None] = None    # threshold for "bad"
    requires: ClassVar[tuple[str, ...]] = (REQ_AUDIO,)
    optional: ClassVar[tuple[str, ...]] = ()
    scope: ClassVar[str] = "call"         # call | batch
    aggregation: ClassVar[str] = "ratio"  # ratio | samples | mean | rate_of_flags | custom
    depends_on: ClassVar[tuple[str, ...]] = ()
    implementation_note: ClassVar[str] = ""

    def dependencies(self, profile: "Profile") -> tuple[str, ...]:
        return self.depends_on

    # ---- judge ----
    def judge_section(self, profile: "Profile") -> JudgeSection | None:
        return None

    # ---- compute ----
    def compute(self, ctx: "CallContext") -> MetricResult:
        raise NotImplementedError

    def summarize_samples(self, samples: list[float]) -> tuple[float, dict]:
        """For aggregation == 'samples': (headline value, extra details)."""
        arr = np.asarray(samples, dtype=float)
        return float(np.median(arr)), {"mean": float(arr.mean()), "n": int(arr.size)}

    def aggregate(self, results: list[MetricResult], contexts: list["CallContext"] | None = None,
                  profile: "Profile | None" = None) -> MetricResult:
        ok = [r for r in results if r.status == "ok"]
        if not ok:
            reasons = sorted({r.reason for r in results if r.reason})
            kinds = [r.na_kind for r in results if r.na_kind]
            kind = max(set(kinds), key=kinds.count) if kinds else NA_NEEDS_DATA
            return self.na("; ".join(reasons) or "No applicable recordings", kind=kind)
        method = ok[0].method
        if self.aggregation == "ratio" and all(r.denominator is not None for r in ok):
            num = sum(r.numerator or 0 for r in ok)
            den = sum(r.denominator or 0 for r in ok)
            if den == 0:
                return self.na("Nothing to evaluate across these calls", kind=NA_NOT_OCCURRED)
            res = self.ratio(num, den, method=method)
        elif self.aggregation == "samples":
            samples = [s for r in ok for s in (r.samples or [])]
            if not samples:
                return self.na("No samples", kind=NA_NOT_OCCURRED)
            value, details = self.summarize_samples(samples)
            res = self.ok(value, method=method, samples=None, details=details)
        else:
            vals = [r.value for r in ok if r.value is not None]
            res = self.ok(float(np.mean(vals)), method=method, details={"std": float(np.std(vals))})
        res.details["recordings"] = len(ok)
        res.details["not_applicable"] = len(results) - len(ok)
        return self.with_band(res, profile)

    # ---- helpers ----
    def ok(self, value: float, *, method: str = "measured", unit: str | None = None, **kw) -> MetricResult:
        return MetricResult(self.id, "ok", float(value), unit if unit is not None else self.unit, method=method, **kw)

    def ratio(self, num: float, den: float, *, method: str = "measured", scale: float | None = None, **kw) -> MetricResult:
        scale = self.scale if scale is None else scale
        if den <= 0:
            return self.na("Nothing to evaluate in this call", method=method, kind=NA_NOT_OCCURRED)
        return MetricResult(self.id, "ok", num / den * scale, self.unit, method=method,
                            numerator=float(num), denominator=float(den), **kw)

    def na(self, reason: str, kind: str = NA_NEEDS_DATA, **kw) -> MetricResult:
        return MetricResult(self.id, "not_applicable", None, self.unit, reason=reason, na_kind=kind, **kw)

    def thresholds(self, profile: "Profile | None") -> tuple[float | None, float | None]:
        ov = profile.metric_override(self.id) if profile else {}
        return ov.get("good", self.good), ov.get("bad", self.bad)

    def with_band(self, res: MetricResult, profile: "Profile | None") -> MetricResult:
        if res.status != "ok" or res.value is None or self.direction == "none":
            return res
        good, bad = self.thresholds(profile)
        if good is None and bad is None:
            return res
        v = res.value
        if self.direction == "lower":
            res.band = "good" if good is not None and v <= good else "bad" if bad is not None and v >= bad else "warn"
        else:
            res.band = "good" if good is not None and v >= good else "bad" if bad is not None and v <= bad else "warn"
        return res

    @classmethod
    def target_text(cls) -> str:
        if cls.direction == "none" or cls.good is None:
            return ""
        u = "" if cls.unit in ("", "score") else (cls.unit if cls.unit == "%" else f" {cls.unit}")
        g = f"{cls.good:g}{u}"
        return f"≤ {g}" if cls.direction == "lower" else f"≥ {g}"

    @classmethod
    def describe(cls) -> dict:
        return {
            "target": cls.target_text(),
            "id": cls.id, "priority": cls.priority, "name": cls.name, "category": cls.category,
            "question": cls.question, "value_range": cls.value_range, "formula": cls.formula,
            "terminology": cls.terminology, "unit": cls.unit, "direction": cls.direction,
            "good": cls.good, "bad": cls.bad, "requires": list(cls.requires), "optional": list(cls.optional),
            "scope": cls.scope, "depends_on": list(cls.depends_on),
            "implementation_note": cls.implementation_note,
        }


REGISTRY: dict[str, Metric] = {}


def register(cls: type[Metric]) -> type[Metric]:
    if cls.id in REGISTRY:
        raise ValueError(f"Duplicate metric id {cls.id}")
    REGISTRY[cls.id] = cls()
    return cls


def all_metrics() -> list[Metric]:
    return sorted(REGISTRY.values(), key=lambda m: (m.priority, m.id))


def get_metric(metric_id: str) -> Metric:
    if metric_id not in REGISTRY:
        raise KeyError(f"Unknown metric '{metric_id}'")
    return REGISTRY[metric_id]
