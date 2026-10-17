"""HTTP API + static web UI."""
from __future__ import annotations

import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..analysis.context import CallContext
from ..audio.loader import AudioError
from ..config import get_settings, list_profiles, load_profile
from ..db import Recording, Run, RunItem, session_scope
from ..engine.ingest import ingest_recording, save_upload
from ..engine.service import create_run, get_executor, recording_to_dict
from ..judge.base import (PROVIDERS, JudgeConfig, JudgeUnavailable, _ensure_providers, _provider_key,
                          build_judge, judge_status, load_store, save_store)
from ..engine.readiness import readiness
from ..metrics import PACKS, PRESETS, all_metrics, preset_metrics
from ..metrics.base import REQ_JUDGE, REQ_TRANSCRIPT
from ..storage import get_storage
from ..transcription.providers import available_asr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_executor().recover()
    yield


app = FastAPI(title="VoiceEval", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    return {"status": "ok", "executor": s.executor, "judge": judge_status(),
            "asr": {"provider": available_asr(), "configured": s.transcriber}}


@app.get("/api/metrics")
def metrics() -> dict:
    js = judge_status()["available"]
    asr = available_asr() is not None
    out = []
    for m in all_metrics():
        d = m.describe()
        warn = []
        if REQ_JUDGE in m.requires and not js:
            warn.append("LLM judge not configured")
        if REQ_TRANSCRIPT in m.requires and not asr:
            warn.append("Needs sidecar transcript (no ASR installed)")
        d["warnings"] = warn
        out.append(d)
    presets = {k: {"label": v["label"], "metrics": preset_metrics(k)} for k, v in PRESETS.items()}
    return {"metrics": out, "presets": presets, "packs": PACKS}


class ReadinessQuery(BaseModel):
    recording_ids: list[str]


@app.post("/api/readiness")
def post_readiness(body: ReadinessQuery) -> dict:
    with session_scope() as s:
        recs = [recording_to_dict(r) for r in s.scalars(select(Recording).where(Recording.id.in_(body.recording_ids)))]
    return readiness(recs)


@app.get("/api/profiles")
def profiles() -> list[str]:
    return list_profiles()


@app.get("/api/profiles/{name}")
def profile(name: str) -> dict:
    try:
        return load_profile(name).data
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------------------
# LLM judge settings (provider plugins)
# ---------------------------------------------------------------------------

def _mask(key: str | None) -> str | None:
    if not key:
        return None
    return key[:4] + "…" + key[-4:] if len(key) > 12 else "…" + key[-2:]


@app.get("/api/settings/llm")
def get_llm_settings() -> dict:
    _ensure_providers()
    store = load_store()
    out = []
    for name, cls in PROVIDERS.items():
        pconf = (store.get("providers", {}) or {}).get(name, {}) or {}
        key, src = _provider_key(name, store)
        out.append({**cls.describe(), "model": pconf.get("model") or "", "base_url": pconf.get("base_url") or "",
                    "effort": pconf.get("effort") or "", "key_masked": _mask(key), "key_source": src})
    return {"selected": store.get("provider") or get_settings().judge or "auto", "providers": out,
            "status": judge_status()}


class ProviderSettings(BaseModel):
    api_key: str | None = None        # None = keep current; "" = remove
    model: str | None = None
    base_url: str | None = None
    effort: str | None = None


class LLMSettings(BaseModel):
    selected: str | None = None       # auto | none | provider name
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)


@app.put("/api/settings/llm")
def put_llm_settings(body: LLMSettings) -> dict:
    _ensure_providers()
    store = load_store()
    if body.selected is not None:
        if body.selected not in ("auto", "none", *PROVIDERS):
            raise HTTPException(400, f"Unknown provider {body.selected}")
        store["provider"] = body.selected
    provs = store.setdefault("providers", {})
    for name, ps in body.providers.items():
        if name not in PROVIDERS:
            raise HTTPException(400, f"Unknown provider {name}")
        cur = provs.setdefault(name, {})
        for field_name in ("api_key", "model", "base_url", "effort"):
            v = getattr(ps, field_name)
            if v is None:
                continue
            v = v.strip()
            if v:
                cur[field_name] = v
            else:
                cur.pop(field_name, None)
    save_store(store)
    return get_llm_settings()


class ProviderTest(BaseModel):
    provider: str
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


def _test_config(body: ProviderTest) -> JudgeConfig:
    _ensure_providers()
    if body.provider not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider {body.provider}")
    cls = PROVIDERS[body.provider]
    if not cls.describe()["installed"]:
        raise HTTPException(400, f"Install the '{cls.pip_name}' package first: pip install {cls.pip_name}")
    store = load_store()
    pconf = (store.get("providers", {}) or {}).get(body.provider, {}) or {}
    key = body.api_key or _provider_key(body.provider, store)[0]
    return JudgeConfig(body.provider, body.model or pconf.get("model") or cls.default_model, key,
                       body.base_url or pconf.get("base_url"), None)


@app.post("/api/settings/llm/test")
def test_llm(body: ProviderTest) -> dict:
    cfg = _test_config(body)
    try:
        out = build_judge(cfg).ping()
        return {"ok": True, "model": cfg.model, "reply": out}
    except JudgeUnavailable as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # surface any provider error to the user
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/settings/llm/models")
def llm_models(body: ProviderTest) -> dict:
    cfg = _test_config(body)
    try:
        return {"ok": True, "models": build_judge(cfg).list_models()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}


# ---------------------------------------------------------------------------
# recordings
# ---------------------------------------------------------------------------

def _json_field(raw: str | None, field: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"{field} is not valid JSON: {e}")


@app.post("/api/recordings")
async def upload_recording(
    file: UploadFile | None = File(None),
    human_file: UploadFile | None = File(None),
    ai_file: UploadFile | None = File(None),
    transcript: UploadFile | None = File(None),
    name: str = Form(""),
    human_channel: str = Form("left"),
    tags: str = Form(""),
    meta: str = Form(""),
) -> dict:
    paths = []
    try:
        kw: dict[str, Any] = {}
        if file is not None and file.filename:
            p = save_upload(await file.read(), file.filename)
            paths.append(p)
            kw["stereo_path"] = p
            default_name = Path(file.filename).stem
        elif human_file is not None and ai_file is not None:
            kw["human_path"] = save_upload(await human_file.read(), human_file.filename)
            kw["ai_path"] = save_upload(await ai_file.read(), ai_file.filename)
            paths += [kw["human_path"], kw["ai_path"]]
            default_name = Path(human_file.filename).stem
        else:
            raise HTTPException(400, "Upload a stereo `file`, or both `human_file` and `ai_file`")
        tr = json.loads(await transcript.read()) if transcript is not None and transcript.filename else None
        return ingest_recording(name=name or default_name, human_channel=human_channel, transcript=tr,
                                tags=_json_field(tags, "tags"), meta=_json_field(meta, "meta"),
                                original_filename=(file.filename if file is not None and file.filename else ""), **kw)
    except (AudioError, ValueError) as e:
        raise HTTPException(400, str(e))
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


@app.get("/api/recordings")
def list_recordings() -> list[dict]:
    with session_scope() as s:
        recs = s.scalars(select(Recording).order_by(Recording.created_at.desc())).all()
        out = [recording_to_dict(r) for r in recs]
        # latest finished evaluation of each recording, for at-a-glance status
        latest: dict[str, RunItem] = {}
        for it in s.scalars(select(RunItem).where(RunItem.status == "done").order_by(RunItem.finished_at)):
            latest[it.recording_id] = it
        for r in out:
            it = latest.get(r["id"])
            if it is None:
                r["last_eval"] = None
                continue
            res = (it.results or {}).values()
            r["last_eval"] = {
                "run_id": it.run_id, "item_id": it.id, "finished_at": it.finished_at.isoformat() if it.finished_at else None,
                "good": sum(x.get("band") == "good" for x in res), "warn": sum(x.get("band") == "warn" for x in res),
                "bad": sum(x.get("band") == "bad" for x in res),
            }
        return out


def _get_recording(rid: str) -> dict:
    with session_scope() as s:
        r = s.get(Recording, rid)
        if r is None:
            raise HTTPException(404, "Recording not found")
        return recording_to_dict(r)


@app.get("/api/recordings/{rid}")
def get_recording(rid: str) -> dict:
    return _get_recording(rid)


class RecordingPatch(BaseModel):
    name: str | None = None
    human_channel: str | None = None
    tags: dict | None = None
    meta: dict | None = None


@app.patch("/api/recordings/{rid}")
def patch_recording(rid: str, body: RecordingPatch) -> dict:
    with session_scope() as s:
        r = s.get(Recording, rid)
        if r is None:
            raise HTTPException(404, "Recording not found")
        if body.name is not None:
            r.name = body.name
        if body.tags is not None:
            r.tags = body.tags
        if body.meta is not None:
            r.meta = body.meta
        if body.human_channel is not None:
            hc = 0 if body.human_channel in ("left", "0") else 1
            if hc != r.human_channel:
                r.human_channel, r.ai_channel = hc, 1 - hc
                get_storage().delete_prefix(f"cache/{rid}")  # analysis depends on channel mapping
        return recording_to_dict(r)


@app.delete("/api/recordings/{rid}")
def delete_recording(rid: str) -> dict:
    with session_scope() as s:
        r = s.get(Recording, rid)
        if r is None:
            raise HTTPException(404, "Recording not found")
        run_ids = set()
        for item in s.scalars(select(RunItem).where(RunItem.recording_id == rid)):
            run_ids.add(item.run_id)
            s.delete(item)
        s.delete(r)
        s.flush()
        for run_id in run_ids:  # a run with no calls left is meaningless
            if not s.scalars(select(RunItem.id).where(RunItem.run_id == run_id)).first():
                s.delete(s.get(Run, run_id))
    get_storage().delete_prefix(f"recordings/{rid}")
    get_storage().delete_prefix(f"cache/{rid}")
    return {"deleted": rid}


@app.get("/api/recordings/{rid}/audio")
def recording_audio(rid: str):
    rec = _get_recording(rid)
    return FileResponse(get_storage().local_path(rec["audio_key"]), media_type="audio/wav")


@app.get("/api/recordings/{rid}/preview")
def recording_preview(rid: str, profile: str = "default") -> dict:
    """Fast channel timeline (VAD + transcript if present) without running metrics."""
    rec = _get_recording(rid)
    ctx = CallContext(rec, load_profile(profile))
    out = {"timeline": ctx.timeline.to_dict()}
    if ctx.transcript is not None:
        out["turns"] = ctx.turn_texts
        out["transcript_source"] = ctx.transcript.source
    return out


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

class RunCreate(BaseModel):
    recording_ids: list[str]
    metric_ids: list[str] = Field(default_factory=list)
    preset: str | None = None
    profile: str = "default"
    name: str = ""
    options: dict = Field(default_factory=dict)


@app.post("/api/runs")
def post_run(body: RunCreate) -> dict:
    metric_ids = list(body.metric_ids)
    if body.preset:
        if body.preset not in PRESETS:
            raise HTTPException(400, f"Unknown preset {body.preset}")
        metric_ids += preset_metrics(body.preset)
    try:
        rid = create_run(body.recording_ids, list(dict.fromkeys(metric_ids)), body.profile, body.name, body.options)
    except (ValueError, KeyError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    return get_run(rid)


def _run_dict(run: Run, with_items: bool) -> dict:
    counts: dict[str, int] = {}
    for i in run.items:
        counts[i.status] = counts.get(i.status, 0) + 1
    d = {"id": run.id, "name": run.name, "profile": run.profile, "metric_ids": run.metric_ids,
         "options": run.options, "status": run.status, "summary": run.summary, "item_counts": counts,
         "total_items": len(run.items),
         "created_at": run.created_at.isoformat() if run.created_at else None,
         "finished_at": run.finished_at.isoformat() if run.finished_at else None}
    if with_items:
        d["items"] = [{
            "id": i.id, "recording_id": i.recording_id, "recording_name": i.recording.name if i.recording else "?",
            "status": i.status, "error": i.error, "results": i.results, "analysis": i.analysis,
            "tags": i.recording.tags if i.recording else {},
        } for i in run.items]
    return d


@app.get("/api/runs")
def list_runs() -> list[dict]:
    with session_scope() as s:
        runs = s.scalars(select(Run).order_by(Run.created_at.desc()).limit(200)).all()
        return [_run_dict(r, with_items=False) for r in runs]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        return _run_dict(run, with_items=True)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        s.delete(run)
    return {"deleted": run_id}


@app.get("/api/runs/{run_id}/export.csv")
def export_run_csv(run_id: str):
    run = get_run(run_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["recording", "metric", "status", "value", "unit", "band", "method", "numerator", "denominator", "reason"])
    for item in run["items"]:
        for mid in run["metric_ids"]:
            r = (item["results"] or {}).get(mid, {})
            w.writerow([item["recording_name"], mid, r.get("status"), r.get("value"), r.get("unit"), r.get("band"),
                        r.get("method"), r.get("numerator"), r.get("denominator"), r.get("reason")])
    for mid in run["metric_ids"]:
        r = (run["summary"] or {}).get(mid, {})
        w.writerow(["__ALL__", mid, r.get("status"), r.get("value"), r.get("unit"), r.get("band"), r.get("method"),
                    r.get("numerator"), r.get("denominator"), r.get("reason")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="run-{run_id}.csv"'})


@app.get("/api/runs/{run_id}/export.json")
def export_run_json(run_id: str):
    return get_run(run_id)


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
