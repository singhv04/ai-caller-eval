#!/usr/bin/env python3
"""Build the read-only GitHub Pages demo into site/.

Exports every API response the UI needs (from the local database, no server
required), compresses call audio to MP3, and copies the web UI in "static
mode". No API keys or secrets are written: LLM settings are sanitized and the
output is scanned for key patterns before finishing.

    python scripts/build_static_demo.py            # → site/
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from voiceeval.api.main import app  # noqa: E402
from voiceeval.storage import get_storage  # noqa: E402

SITE = ROOT / "site"
DATA = SITE / "data"
REPO_URL = "https://github.com/singhv04/ai-caller-eval"
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{10,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}")


def write(rel: str, obj) -> None:
    p = DATA / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, separators=(",", ":"), default=str))


def main() -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    DATA.mkdir(parents=True)
    c = TestClient(app)

    # --- system / catalogue (sanitized: no key material, no local paths)
    health = c.get("/api/health").json()
    judge = health["judge"]
    health["judge"] = {"available": judge.get("available", False), "label": judge.get("label", "LLM"),
                       "model": judge.get("model", ""), "reason": "Read-only demo"}
    health["executor"] = "static demo"
    write("health.json", health)
    write("metrics.json", c.get("/api/metrics").json())
    profiles = c.get("/api/profiles").json()
    write("profiles.json", profiles)
    for name in profiles:
        write(f"profiles/{name}.json", c.get(f"/api/profiles/{name}").json())
    llm = c.get("/api/settings/llm").json()
    for p in llm["providers"]:
        p.update(key_masked=None, key_source="", api_key=None)
    llm["status"] = {"available": False, "reason": "Read-only demo — connect a provider in your own installation"}
    write("settings/llm.json", llm)

    # --- recordings, previews, audio
    recs = c.get("/api/recordings").json()
    write("recordings.json", recs)
    write("readiness.json", c.post("/api/readiness", json={"recording_ids": [r["id"] for r in recs]}).json())
    (DATA / "audio").mkdir()
    storage = get_storage()
    for r in recs:
        write(f"recordings/{r['id']}.json", r)
        write(f"recordings/{r['id']}/preview.json", c.get(f"/api/recordings/{r['id']}/preview").json())
        src = storage.local_path(r["audio_key"])
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "2", "-b:a", "64k",
                        str(DATA / "audio" / f"{r['id']}.mp3")], check=True)
        print(f"  call  {r['name']}")

    # --- evaluations
    runs = c.get("/api/runs").json()
    write("runs.json", runs)
    for run in runs:
        write(f"runs/{run['id']}.json", c.get(f"/api/runs/{run['id']}").json())
        (DATA / "runs" / f"{run['id']}.csv").write_text(c.get(f"/api/runs/{run['id']}/export.csv").text)
        print(f"  run   {run['name']}")

    # --- UI in static mode
    for f in ("app.js", "styles.css"):
        shutil.copy(ROOT / "web" / f, SITE / f)
    html = (ROOT / "web" / "index.html").read_text()
    banner = (f'<div class="demo-banner">Read-only demo of <b>VoiceEval</b> with real evaluation results. '
              f'Uploading and running evaluations needs a local install — '
              f'<a href="{REPO_URL}">get it on GitHub</a>.</div>')
    html = html.replace("<body>", "<body>\n  " + banner)
    html = html.replace('<script src="app.js"></script>', '<script>window.VOICEEVAL_STATIC = true;</script>\n  <script src="app.js"></script>')
    (SITE / "index.html").write_text(html)
    with open(SITE / "styles.css", "a") as fh:
        fh.write("\n.demo-banner { background: var(--accent); color: #fff; text-align: center; padding: 8px 16px; font-size: 13px; }\n"
                 ".demo-banner a { color: #fff; text-decoration: underline; }\n")
    (SITE / ".nojekyll").write_text("")

    # --- safety: never publish secrets
    for p in SITE.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".js", ".html", ".csv", ".css") and SECRET_RE.search(p.read_text(errors="ignore")):
            raise SystemExit(f"Refusing to publish: possible secret in {p}")
    size = sum(p.stat().st_size for p in SITE.rglob("*") if p.is_file()) / 1e6
    print(f"site/ built: {len(recs)} calls, {len(runs)} evaluations, {size:.1f} MB")


if __name__ == "__main__":
    main()
