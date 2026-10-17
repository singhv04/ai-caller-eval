"""Shared helpers for metric implementations."""
from __future__ import annotations

import numpy as np


def obj(props: dict) -> dict:
    """Strict JSON-schema object: every property required, no extras (structured-output friendly)."""
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(props: dict) -> dict:
    return {"type": "array", "items": obj(props)}


TURN = {"type": "integer", "description": "Transcript turn index (the N in TN); -1 if not tied to a turn"}
STR = {"type": "string"}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}
SEVERITY = {"type": "string", "enum": ["low", "medium", "high"]}
SCORE_1_5 = {"type": "integer", "enum": [1, 2, 3, 4, 5]}


def percentile(vals, q) -> float:
    return float(np.percentile(np.asarray(vals, dtype=float), q))


def latency_summary(samples_ms: list[float]) -> dict:
    a = np.asarray(samples_ms, dtype=float)
    return {
        "p50_ms": round(float(np.percentile(a, 50)), 1),
        "p90_ms": round(float(np.percentile(a, 90)), 1),
        "p95_ms": round(float(np.percentile(a, 95)), 1),
        "mean_ms": round(float(a.mean()), 1),
        "max_ms": round(float(a.max()), 1),
        "n": int(a.size),
    }


def mean_ci(vals) -> dict:
    a = np.asarray(vals, dtype=float)
    if a.size == 0:
        return {}
    half = 1.96 * a.std(ddof=1) / np.sqrt(a.size) if a.size > 1 else None
    return {"mean": round(float(a.mean()), 3), "n": int(a.size),
            "ci95": [round(float(a.mean() - half), 3), round(float(a.mean() + half), 3)] if half is not None else None}


def ev(start: float, end: float | None = None, note: str = "", **kw) -> dict:
    d = {"start": round(float(start), 2), "end": round(float(end if end is not None else start), 2), "note": note}
    d.update(kw)
    return d
