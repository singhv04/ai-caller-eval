"""Command line: serve, worker, ingest, run, metrics."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_serve(a):
    import uvicorn

    uvicorn.run("voiceeval.api.main:app", host=a.host, port=a.port, reload=a.reload)


def cmd_worker(a):
    from .engine.service import worker_loop

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker_loop(concurrency=a.concurrency, poll_s=a.poll, once=a.once)


def cmd_ingest(a):
    from .engine.ingest import ingest_file

    paths = []
    for p in a.paths:
        p = Path(p)
        if p.is_dir():
            paths += sorted(x for x in p.iterdir() if x.suffix.lower() in {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus"})
        else:
            paths.append(p)
    for p in paths:
        rec = ingest_file(p, human_channel=a.human_channel)
        print(f"{rec['id']}  {rec['name']}  {rec['duration_s']:.1f}s  transcript={'yes' if rec['transcript_key'] else 'no'}")


def cmd_metrics(a):
    from .metrics import PRESETS, all_metrics, preset_metrics

    if a.markdown:
        print("| # | Metric id | Name | Category | Needs | Method notes |")
        print("|---|---|---|---|---|---|")
        for m in all_metrics():
            print(f"| {m.priority} | `{m.id}` | {m.name} | {m.category} | {', '.join(m.requires)}"
                  f"{' (+' + ', '.join(m.optional) + ')' if m.optional else ''} | {m.terminology} |")
        return
    for m in all_metrics():
        print(f"{m.priority:>3}  {m.id:<36} {m.category:<24} needs: {', '.join(m.requires)}")
    print("\nPresets:", ", ".join(f"{k} ({len(preset_metrics(k))})" for k in PRESETS))


def cmd_run(a):
    from sqlalchemy import select

    from .db import Recording, session_scope
    from .engine.service import create_run
    from .metrics import all_metrics, preset_metrics

    with session_scope() as s:
        all_ids = list(s.scalars(select(Recording.id)))
    rec_ids = all_ids if a.recordings == ["all"] else a.recordings
    if a.metrics == ["all"]:
        metric_ids = [m.id for m in all_metrics()]
    else:
        metric_ids = []
        for m in a.metrics:
            metric_ids += preset_metrics(m[7:]) if m.startswith("preset:") else [m]
    options = json.loads(a.options) if a.options else {}
    run_id = create_run(rec_ids, metric_ids, a.profile, a.name or "", options)
    print(f"run {run_id} created ({len(rec_ids)} recordings × {len(metric_ids)} metrics)")
    if not a.wait:
        return
    from .db import Run

    while True:
        with session_scope() as s:
            run = s.get(Run, run_id)
            status = run.status
            summary = run.summary
        if status in ("done", "failed"):
            break
        time.sleep(0.5)
    print(f"status: {status}")
    for mid in metric_ids:
        r = summary.get(mid, {})
        val = "—" if r.get("value") is None else f"{r['value']:.2f} {r.get('unit', '')}"
        print(f"  {mid:<36} {r.get('status', ''):<15} {val:<16} {r.get('band') or ''}  {r.get('reason') or ''}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="voiceeval")
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("serve", help="Run the API + web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("worker", help="Run an evaluation worker (VOICEEVAL_EXECUTOR=worker)")
    s.add_argument("--concurrency", type=int, default=2)
    s.add_argument("--poll", type=float, default=1.0)
    s.add_argument("--once", action="store_true", help="Exit when the queue is empty")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("ingest", help="Ingest stereo recordings (files or directories)")
    s.add_argument("paths", nargs="+")
    s.add_argument("--human-channel", default="left", choices=["left", "right"])
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("metrics", help="List metrics")
    s.add_argument("--markdown", action="store_true")
    s.set_defaults(fn=cmd_metrics)

    s = sub.add_parser("run", help="Evaluate recordings")
    s.add_argument("--recordings", nargs="+", default=["all"])
    s.add_argument("--metrics", nargs="+", default=["preset:core"], help="ids, preset:<name>, or all")
    s.add_argument("--profile", default="default")
    s.add_argument("--name")
    s.add_argument("--options", help="JSON run options")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=cmd_run)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
