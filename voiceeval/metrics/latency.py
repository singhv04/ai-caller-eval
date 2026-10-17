"""Latency & turn-taking metrics (framework priorities 9-16, 19).

Ground truth for "when did the human stop / start" comes from the human
channel and "when did the AI start / stop" from the AI channel, so these are
measured directly from audio. Metrics that need internal system timestamps
(endpoint decision, first audio packet, barge-in detection) read them from
the recording's optional event log (`meta.events`).
"""
from __future__ import annotations

import numpy as np

from .base import REQ_AUDIO, REQ_EVENTS, Metric, register
from .util import ev, latency_summary


def _events(ctx, kind: str) -> list[float]:
    return sorted(float(e["t"]) for e in ctx.events if e.get("type") == kind and "t" in e)


@register
class TurnaroundTime(Metric):
    id = "turnaround_time"
    priority = 9
    name = "Turnaround time (TAT)"
    category = "Latency & turn-taking"
    question = "How long the user waits after finishing before AI starts speaking."
    value_range = "≥0 ms; provisional ideal p50 <800 ms and p95 <1500 ms for interactive use."
    formula = "t(first audible AI speech) − t(user speech end)"
    terminology = "Measured per human→AI handoff from the two channels; includes endpointing, reasoning and synthesis."
    unit = "ms"
    direction = "lower"
    good, bad = 800, 1500
    aggregation = "samples"
    implementation_note = "Headline value is p50; p95 and the full distribution are in details."

    def summarize_samples(self, samples):
        s = latency_summary(samples)
        return s["p50_ms"], s

    def compute(self, ctx):
        gaps = [r for r in ctx.timeline.responses if not r["overlapped"]]
        if not gaps:
            return self.na("No human→AI handoffs detected", kind="not_occurred")
        samples = [r["gap_ms"] for r in gaps]
        value, details = self.summarize_samples(samples)
        details["p95_target_met"] = details["p95_ms"] < 1500
        evidence = [ev(r["human_end"], r["ai_start"], f"{r['gap_ms']:.0f} ms") for r in
                    sorted(gaps, key=lambda r: -r["gap_ms"])[:5]]
        return self.ok(value, samples=samples, details=details, evidence=evidence)


@register
class TimeToFirstAudio(Metric):
    id = "time_to_first_audio"
    priority = 10
    name = "Time to first audio (TTFA)"
    category = "Latency & turn-taking"
    question = "How quickly generated audio starts arriving."
    value_range = "≥0 ms; provisional p50 <500 ms; separate from TAT."
    formula = "t(first audio packet) − t(request accepted)"
    terminology = "Needs client-side events `tts_request` and `first_audio_packet` in meta.events."
    unit = "ms"
    direction = "lower"
    good, bad = 500, 1000
    requires = (REQ_EVENTS,)
    aggregation = "samples"

    def summarize_samples(self, samples):
        s = latency_summary(samples)
        return s["p50_ms"], s

    def compute(self, ctx):
        req, first = _events(ctx, "tts_request"), _events(ctx, "first_audio_packet")
        samples = []
        for t in req:
            nxt = next((f for f in first if f >= t), None)
            if nxt is not None:
                samples.append((nxt - t) * 1000)
        if not samples:
            return self.na("No tts_request/first_audio_packet events in meta.events")
        v, d = self.summarize_samples(samples)
        return self.ok(v, samples=samples, details=d)


@register
class EndOfTurnDelay(Metric):
    id = "end_of_turn_detection_delay"
    priority = 11
    name = "End-of-turn detection delay"
    category = "Latency & turn-taking"
    question = "How long AI waits to recognize the user has stopped."
    value_range = "≥0 ms; lower better unless it cuts users off; evaluate jointly with premature endpoint rate."
    formula = "t(endpoint decision) − t(actual user speech end)"
    terminology = "Actual speech end from the human channel; endpoint decisions from `endpoint` events."
    unit = "ms"
    direction = "lower"
    good, bad = 300, 800
    requires = (REQ_AUDIO, REQ_EVENTS)
    aggregation = "samples"

    def summarize_samples(self, samples):
        s = latency_summary(samples)
        return s["p50_ms"], s

    def compute(self, ctx):
        eps = _events(ctx, "endpoint")
        if not eps:
            return self.na("No `endpoint` events in meta.events")
        human_ends = [t.end for t in ctx.timeline.turns_of("human")]
        samples = []
        for e in eps:
            prior = [h for h in human_ends if h <= e + 0.05]
            if prior and e - prior[-1] < 5:
                samples.append(max(0.0, e - prior[-1]) * 1000)
        if not samples:
            return self.na("Could not align endpoint events with user turns", kind="not_occurred")
        v, d = self.summarize_samples(samples)
        return self.ok(v, samples=samples, details=d)


@register
class PrematureEndpointRate(Metric):
    id = "premature_endpoint_rate"
    priority = 12
    name = "Premature endpoint rate"
    category = "Latency & turn-taking"
    question = "How often AI assumes the person is done while they are still speaking."
    value_range = "0–100%; ideal ~0%; report by pause length and speaking style."
    formula = "Early endpoints / True user turns × 100"
    terminology = ("Proxy: the AI starts talking inside a short user pause and the user resumes within "
                   "the resume window — i.e. the AI split an unfinished utterance.")
    direction = "lower"
    good, bad = 2, 10

    def compute(self, ctx):
        tl = ctx.timeline
        n = len(tl.turns_of("human"))
        pe = tl.premature_endpoints
        return self.ratio(len(pe), n, method="proxy",
                          evidence=[ev(p["ai_onset"], p["user_resumed_at"], f"user pause {p['user_pause_ms']:.0f} ms")
                                    for p in pe],
                          details={"events": pe})


@register
class LatencyJitter(Metric):
    id = "latency_jitter"
    priority = 13
    name = "Latency jitter"
    category = "Latency & turn-taking"
    question = "Whether response speed feels consistent."
    value_range = "≥0 ms; lower better; compare similar scenarios and devices."
    formula = "SD(TAT)  (also reports p95(TAT) − p50(TAT))"
    unit = "ms"
    direction = "lower"
    good, bad = 250, 600
    aggregation = "samples"

    def summarize_samples(self, samples):
        a = np.asarray(samples, dtype=float)
        return float(a.std(ddof=1)) if a.size > 1 else 0.0, {
            "sd_ms": round(float(a.std(ddof=1)), 1) if a.size > 1 else 0.0,
            "p95_minus_p50_ms": round(float(np.percentile(a, 95) - np.percentile(a, 50)), 1), "n": int(a.size)}

    def compute(self, ctx):
        samples = [r["gap_ms"] for r in ctx.timeline.responses if not r["overlapped"]]
        if len(samples) < 2:
            return self.na("Need at least 2 handoffs to measure jitter", kind="not_occurred")
        v, d = self.summarize_samples(samples)
        return self.ok(v, samples=samples, details=d)


@register
class BargeInStopLatency(Metric):
    id = "barge_in_stop_latency"
    priority = 14
    name = "Barge-in stop latency"
    category = "Latency & turn-taking"
    question = "How quickly AI stops talking when interrupted."
    value_range = "≥0 ms; provisional <500 ms; test noisy environments."
    formula = "t(AI audio stops) − t(user interruption onset)"
    terminology = ("Onset = human-channel speech starting while the AI channel is active (backchannels excluded); "
                   "stop = end of that stretch of AI speech.")
    unit = "ms"
    direction = "lower"
    good, bad = 500, 1500
    aggregation = "samples"

    def summarize_samples(self, samples):
        s = latency_summary(samples)
        return s["p50_ms"], s

    def compute(self, ctx):
        b = ctx.timeline.barge_ins
        if not b:
            return self.na("No user interruptions (barge-ins) in this call", kind="not_occurred")
        samples = [x["stop_latency_ms"] for x in b]
        v, d = self.summarize_samples(samples)
        d["did_not_stop"] = sum(not x["stopped"] for x in b)
        return self.ok(v, samples=samples, details=d,
                       evidence=[ev(x["onset"], x["ai_stop"], f"AI stopped after {x['stop_latency_ms']:.0f} ms") for x in b])


@register
class BargeInDetectionLatency(Metric):
    id = "barge_in_detection_latency"
    priority = 15
    name = "Barge-in detection latency"
    category = "Latency & turn-taking"
    question = "How fast AI notices someone interrupting it."
    value_range = "≥0 ms; provisional <300 ms under clean audio."
    formula = "t(interruption detected) − t(user interruption onset)"
    terminology = "Onset from the human channel; detection time from `barge_in_detected` events."
    unit = "ms"
    direction = "lower"
    good, bad = 300, 800
    requires = (REQ_AUDIO, REQ_EVENTS)
    aggregation = "samples"

    def summarize_samples(self, samples):
        s = latency_summary(samples)
        return s["p50_ms"], s

    def compute(self, ctx):
        det = _events(ctx, "barge_in_detected")
        if not det:
            return self.na("No `barge_in_detected` events in meta.events")
        samples = []
        for b in ctx.timeline.barge_ins:
            d = next((t for t in det if 0 <= t - b["onset"] <= 3), None)
            if d is not None:
                samples.append((d - b["onset"]) * 1000)
        if not samples:
            return self.na("No detection events aligned with barge-ins", kind="not_occurred")
        v, d = self.summarize_samples(samples)
        return self.ok(v, samples=samples, details=d)


@register
class InterruptionHandlingSuccess(Metric):
    id = "interruption_handling_success"
    priority = 16
    name = "Interruption handling success"
    category = "Latency & turn-taking"
    question = "Whether users can interrupt naturally."
    value_range = "0–100%; target ≥95% after validation."
    formula = "Correctly handled interruptions / All valid interruptions × 100"
    terminology = ("Audio proxy: AI stops within the max-yield window AND speaks again after the user finishes. "
                   "Context retention after interruption is covered by `context_retention_accuracy`.")
    direction = "higher"
    good, bad = 95, 80

    def compute(self, ctx):
        b = ctx.timeline.barge_ins
        if not b:
            return self.na("No user interruptions in this call", kind="not_occurred")
        okc = [x for x in b if x["stopped"] and x["ai_responded"]]
        return self.ratio(len(okc), len(b), method="proxy",
                          evidence=[ev(x["onset"], x["ai_stop"], "handled" if x in okc else "NOT handled") for x in b])


@register
class UnwantedOverlapTurns(Metric):
    id = "unwanted_overlap_turns"
    priority = 19
    name = "Unwanted overlap rate (turns)"
    category = "Latency & turn-taking"
    question = "How often AI talks over the user."
    value_range = "0–100%; ideal ~0%; annotate intentional overlap separately."
    formula = "Unwanted overlapping turns / Total turns × 100"
    terminology = "AI turns that start while the human channel is mid-utterance; cooperative backchannels excluded."
    direction = "lower"
    good, bad = 2, 10

    def compute(self, ctx):
        tl = ctx.timeline
        ints = tl.ai_interruptions
        return self.ratio(len(ints), len(tl.turns),
                          evidence=[ev(i["onset"], i["human_segment_end"], "AI started over the user") for i in ints])
