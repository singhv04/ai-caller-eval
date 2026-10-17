"""Run orchestration and work execution.

A run = N recordings × M metrics. Each recording is a RunItem — the unit of
work. Items are claimed atomically (UPDATE ... WHERE status='pending'), so any
number of executors can share one database:

* LocalExecutor  — thread pool inside the API process (single-box default)
* worker_loop    — `voiceeval worker` processes on any number of machines
                   (set VOICEEVAL_EXECUTOR=worker on the API, shared DB + storage)

When the last item of a run finishes, whichever executor finished it
aggregates the run.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select, update

from ..config import get_settings, load_profile
from ..db import Recording, Run, RunItem, session_scope, utcnow
from ..metrics import get_metric
from .evaluator import aggregate_results, evaluate_recording

log = logging.getLogger(__name__)


def recording_to_dict(r: Recording) -> dict:
    return {
        "id": r.id, "name": r.name, "original_filename": r.original_filename, "audio_key": r.audio_key,
        "transcript_key": r.transcript_key, "duration_s": r.duration_s, "sample_rate": r.sample_rate,
        "human_channel": r.human_channel, "ai_channel": r.ai_channel, "tags": r.tags or {}, "meta": r.meta or {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ---------------------------------------------------------------------------
# run lifecycle
# ---------------------------------------------------------------------------

def create_run(recording_ids: list[str], metric_ids: list[str], profile: str = "default",
               name: str = "", options: dict | None = None) -> str:
    if not recording_ids:
        raise ValueError("Select at least one recording")
    if not metric_ids:
        raise ValueError("Select at least one metric")
    for mid in metric_ids:
        get_metric(mid)  # validates
    load_profile(profile)  # validates
    with session_scope() as s:
        found = set(s.scalars(select(Recording.id).where(Recording.id.in_(recording_ids))))
        missing = set(recording_ids) - found
        if missing:
            raise ValueError(f"Unknown recordings: {sorted(missing)}")
        run = Run(name=name or f"Run {utcnow():%Y-%m-%d %H:%M}", profile=profile,
                  metric_ids=list(dict.fromkeys(metric_ids)), options=options or {}, status="pending")
        s.add(run)
        s.flush()
        for rid in dict.fromkeys(recording_ids):
            s.add(RunItem(run_id=run.id, recording_id=rid))
        run_id = run.id
    get_executor().dispatch(run_id)
    return run_id


def claim_item(item_id: str, worker: str) -> bool:
    with session_scope() as s:
        res = s.execute(update(RunItem).where(RunItem.id == item_id, RunItem.status == "pending")
                        .values(status="running", worker=worker, started_at=utcnow()))
        if res.rowcount != 1:
            return False
        run_id = s.get(RunItem, item_id).run_id
        s.execute(update(Run).where(Run.id == run_id, Run.status == "pending").values(status="running"))
        return True


def process_item(item_id: str, worker: str, claimed: bool = False) -> None:
    if not claimed and not claim_item(item_id, worker):
        return
    with session_scope() as s:
        item = s.get(RunItem, item_id)
        run = item.run
        rec = recording_to_dict(item.recording)
        metric_ids, profile_name, options, run_id = list(run.metric_ids), run.profile, dict(run.options or {}), run.id
    t0 = time.time()
    try:
        profile = load_profile(profile_name, options.get("profile_overrides"))
        results, analysis = evaluate_recording(rec, metric_ids, profile, options)
        status, error = "done", None
    except Exception as e:
        log.exception("item %s failed", item_id)
        results, analysis, status, error = {}, {}, "failed", f"{type(e).__name__}: {e}"
    analysis["elapsed_s"] = round(time.time() - t0, 2)
    with session_scope() as s:
        item = s.get(RunItem, item_id)
        item.status, item.error, item.results, item.analysis = status, error, results, analysis
        item.finished_at = utcnow()
    maybe_finalize(run_id)


def maybe_finalize(run_id: str) -> None:
    with session_scope() as s:
        open_items = s.scalars(select(RunItem.id).where(RunItem.run_id == run_id,
                                                        RunItem.status.in_(["pending", "running"]))).first()
        if open_items:
            return
        # only one executor wins the right to aggregate
        res = s.execute(update(Run).where(Run.id == run_id, Run.status.in_(["pending", "running"]))
                        .values(status="aggregating"))
        if res.rowcount != 1:
            return
    finalize_run(run_id)


def finalize_run(run_id: str) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        items = [{"recording": recording_to_dict(i.recording), "results": i.results or {}, "status": i.status}
                 for i in run.items]
        metric_ids, profile_name, options = list(run.metric_ids), run.profile, dict(run.options or {})
    profile = load_profile(profile_name, options.get("profile_overrides"))
    done = [i for i in items if i["status"] == "done"]
    summary = aggregate_results(metric_ids, done, profile, options) if done else {}
    with session_scope() as s:
        run = s.get(Run, run_id)
        run.summary = summary
        run.status = "done" if done else "failed"
        run.finished_at = utcnow()


# ---------------------------------------------------------------------------
# executors
# ---------------------------------------------------------------------------

class LocalExecutor:
    def __init__(self, workers: int):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="voiceeval")
        self.name = f"local:{socket.gethostname()}:{os.getpid()}"

    def dispatch(self, run_id: str) -> None:
        with session_scope() as s:
            ids = list(s.scalars(select(RunItem.id).where(RunItem.run_id == run_id, RunItem.status == "pending")))
        for item_id in ids:
            self.pool.submit(self._safe, item_id)

    def _safe(self, item_id: str) -> None:
        try:
            process_item(item_id, self.name)
        except Exception:
            log.exception("executor crashed on %s", item_id)

    def recover(self) -> None:
        """On startup: requeue items orphaned by a previous crash and resume pending runs."""
        with session_scope() as s:
            s.execute(update(RunItem).where(RunItem.status == "running", RunItem.worker.like("local:%"))
                      .values(status="pending", worker=None))
            run_ids = set(s.scalars(select(RunItem.run_id).where(RunItem.status == "pending")))
            stuck = list(s.scalars(select(Run.id).where(Run.status == "aggregating")))
        for rid in run_ids:
            self.dispatch(rid)
        for rid in stuck:
            threading.Thread(target=finalize_run, args=(rid,), daemon=True).start()


class ExternalWorkerExecutor:
    """API side when evaluation happens in separate `voiceeval worker` processes."""

    def dispatch(self, run_id: str) -> None:  # workers poll the DB
        return

    def recover(self) -> None:
        return


_executor = None


def get_executor():
    global _executor
    if _executor is None:
        s = get_settings()
        _executor = ExternalWorkerExecutor() if s.executor == "worker" else LocalExecutor(s.local_workers)
    return _executor


def worker_loop(concurrency: int = 2, poll_s: float = 1.0, stale_minutes: float = 60, once: bool = False) -> None:
    name = f"worker:{socket.gethostname()}:{os.getpid()}"
    pool = ThreadPoolExecutor(max_workers=concurrency)
    inflight: set = set()
    log.info("%s started (concurrency=%d)", name, concurrency)
    while True:
        # requeue items whose worker died
        with session_scope() as s:
            s.execute(update(RunItem).where(RunItem.status == "running",
                                            RunItem.started_at < utcnow() - timedelta(minutes=stale_minutes))
                      .values(status="pending", worker=None))
        inflight = {f for f in inflight if not f.done()}
        free = concurrency - len(inflight)
        if free > 0:
            with session_scope() as s:
                ids = list(s.scalars(select(RunItem.id).where(RunItem.status == "pending").limit(free * 2)))
            for item_id in ids:
                if len(inflight) >= concurrency:
                    break
                if claim_item(item_id, name):
                    inflight.add(pool.submit(_process_claimed, item_id, name))
        if once and not inflight:
            return
        time.sleep(poll_s)


def _process_claimed(item_id: str, worker: str) -> None:
    try:
        process_item(item_id, worker, claimed=True)
    except Exception:
        log.exception("worker failed on %s", item_id)
