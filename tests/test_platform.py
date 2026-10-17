"""End-to-end tests on a small synthetic dual-channel call (no external services)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

_tmp = tempfile.mkdtemp(prefix="voiceeval-test-")
os.environ["VOICEEVAL_DATA_DIR"] = _tmp
os.environ["VOICEEVAL_TRANSCRIBER"] = "sidecar"
os.environ["VOICEEVAL_JUDGE"] = "none"

from voiceeval.analysis.context import CallContext  # noqa: E402
from voiceeval.config import load_profile  # noqa: E402
from voiceeval.engine import evaluator  # noqa: E402
from voiceeval.engine.ingest import ingest_recording  # noqa: E402
from voiceeval.judge.base import combined_schema  # noqa: E402
from voiceeval.metrics import all_metrics  # noqa: E402
from voiceeval.metrics.base import REQ_JUDGE  # noqa: E402

SR = 16_000


def tone(dur, f, amp=0.3):
    t = np.arange(int(dur * SR)) / SR
    # voiced-like signal with harmonics so the pitch tracker and VAD both fire
    return (amp * (np.sin(2 * np.pi * f * t) + 0.5 * np.sin(4 * np.pi * f * t))).astype(np.float32)


# (speaker, start, end)
SCRIPT = [
    ("ai", 0.5, 3.0), ("human", 3.6, 5.0),      # TAT 0.6 s... (ai starts 5.8)
    ("ai", 5.8, 9.0), ("human", 8.0, 9.5),      # barge-in at 8.0, AI stops at 9.0 -> 1000 ms
    ("ai", 10.2, 12.0), ("human", 12.5, 14.0),
    ("ai", 13.6, 15.0),                          # AI talks over the user at 13.6
]


@pytest.fixture(scope="module")
def recording():
    total = 16.0
    st = np.zeros((int(total * SR), 2), dtype=np.float32)
    for spk, s, e in SCRIPT:
        ch = 0 if spk == "human" else 1
        st[int(s * SR): int(e * SR), ch] += tone(e - s, 140 if spk == "human" else 210)
    st += 1e-4 * np.random.default_rng(0).standard_normal(st.shape).astype(np.float32)
    path = Path(_tmp) / "call.wav"
    sf.write(path, st, SR, subtype="PCM_16")
    transcript = {"utterances": [
        {"speaker": spk, "start": s, "end": e,
         "text": "Hello, I am your virtual assistant and this call is recorded." if spk == "ai" else "I want to book a cleaning.",
         "words": []} for spk, s, e in SCRIPT]}
    return ingest_recording(name="synthetic", stereo_path=path, human_channel="left", transcript=transcript,
                            tags={"accent": "us", "noise_condition": "clean"},
                            meta={"ai_asr_transcript": "i want to book a cleaning " * 3, "cost": 0.1,
                                  "survey": {"score": 5, "scale": 5}})


def test_timeline(recording):
    ctx = CallContext(recording, load_profile())
    tl = ctx.timeline
    assert [t.speaker for t in tl.turns] == ["ai", "human", "ai", "human", "ai", "human", "ai"]
    gaps = [r["gap_ms"] for r in tl.responses if not r["overlapped"]]
    assert any(abs(g - 800) < 80 for g in gaps)
    assert len(tl.barge_ins) == 1
    assert abs(tl.barge_ins[0]["stop_latency_ms"] - 1000) < 80
    assert len(tl.ai_interruptions) == 1


def test_all_metrics_run_without_errors(recording):
    ids = [m.id for m in all_metrics()]
    results, analysis = evaluator.evaluate_recording(recording, ids, load_profile(), {})
    errors = {k: v["reason"] for k, v in results.items() if v["status"] == "error"}
    assert not errors, errors
    assert results["turnaround_time"]["status"] == "ok"
    assert results["unwanted_overlap_turns"]["value"] > 0
    assert results["consent_disclosure_compliance"]["value"] == 100  # phrase-match fallback
    agg = evaluator.aggregate_results(ids, [{"recording": recording, "results": results}], load_profile(), {})
    assert not [k for k, v in agg.items() if v["status"] == "error"]


def test_judge_schema_is_strict():
    profile = load_profile()
    sections = {m.id: m.judge_section(profile) for m in all_metrics() if m.judge_section(profile)}
    schema = combined_schema(sections)

    def walk(node):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert set(node["required"]) == set(node["properties"])
            for v in node["properties"].values():
                walk(v)
        if node.get("type") == "array":
            walk(node["items"])

    walk(schema)


def _fake_from_schema(node):
    t = node.get("type")
    if "enum" in node:
        return node["enum"][0]
    if t == "object":
        return {k: _fake_from_schema(v) for k, v in node["properties"].items()}
    if t == "array":
        return [_fake_from_schema(node["items"]), _fake_from_schema(node["items"])]
    return {"string": "x", "integer": 1, "boolean": True}.get(t)


class FakeJudge:
    name, model = "fake", "fake-model"

    def evaluate(self, transcript_text, profile, sections, call_facts):
        assert "[T0" in transcript_text
        return {mid: _fake_from_schema(sec.schema) for mid, sec in sections.items()}


def test_judge_metrics_with_stub(recording, monkeypatch):
    import voiceeval.analysis.context as ctxmod

    monkeypatch.setattr(ctxmod, "get_judge", lambda: FakeJudge())
    ids = [m.id for m in all_metrics() if REQ_JUDGE in m.requires or REQ_JUDGE in m.optional]
    results, _ = evaluator.evaluate_recording(recording, ids, load_profile(), {"refresh_judge": True})
    errors = {k: v["reason"] for k, v in results.items() if v["status"] == "error"}
    assert not errors, errors
    judged = [k for k, v in results.items() if v["method"] == "llm_judge" and v["status"] == "ok"]
    assert len(judged) >= 12, judged


def test_api_run_flow(recording):
    from fastapi.testclient import TestClient

    from voiceeval.api.main import app

    with TestClient(app) as c:
        assert c.get("/api/health").json()["status"] == "ok"
        assert len(c.get("/api/metrics").json()["metrics"]) == 57
        run = c.post("/api/runs", json={"recording_ids": [recording["id"]], "preset": "audio_only"}).json()
        import time
        for _ in range(100):
            r = c.get(f"/api/runs/{run['id']}").json()
            if r["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert r["status"] == "done"
        assert r["summary"]["turnaround_time"]["status"] == "ok"
        assert "turnaround_time" in c.get(f"/api/runs/{run['id']}/export.csv").text
