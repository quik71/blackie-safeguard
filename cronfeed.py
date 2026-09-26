#!/usr/bin/env python3
"""Cron output feed for the safeguard dashboard.

Reads `~/.hermes/cron/jobs.json` + `~/.hermes/cron/output/<job_id>/<stamp>.md`
and hands the dashboard one row per job with its recent runs. Never raises:
a missing/corrupt jobs file degrades to an empty list, and a single unreadable
run file is skipped rather than breaking the panel.

    python3 cronfeed.py                       # jobs + output index (JSON)
    python3 cronfeed.py <job_id> <file.md>    # one run's output
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"
OUTPUT_ROOT = HERMES_HOME / "cron" / "output"

MAX_FILES = 25        # runs listed per job (newest first)
MAX_BYTES = 60000     # cap on a single rendered run


def _load_jobs() -> list[dict]:
    try:
        data = json.loads(JOBS_FILE.read_text())
    except (OSError, ValueError):
        return []
    jobs = data if isinstance(data, list) else data.get("jobs", data)
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [j for j in jobs if isinstance(j, dict)]


def _schedule(j: dict) -> str:
    s = j.get("schedule")
    if isinstance(s, dict):
        return s.get("display") or s.get("expr") or ""
    return str(s or "")


def _runs(job_id: str) -> list[dict]:
    d = OUTPUT_ROOT / job_id
    if not job_id or not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file():
            out.append({"name": p.name, "mtime": int(st.st_mtime), "size": st.st_size})
    out.sort(key=lambda r: r["name"], reverse=True)
    return out


def jobs_overview() -> dict:
    rows = []
    for j in _load_jobs():
        jid = str(j.get("id") or "")
        runs = _runs(jid)
        rows.append({
            "id": jid,
            "name": j.get("name") or jid,
            "schedule": _schedule(j),
            "enabled": bool(j.get("enabled", True)),
            "state": str(j.get("state") or ("scheduled" if j.get("enabled", True) else "paused")),
            "deliver": str(j.get("deliver") or ""),
            "script": str(j.get("script") or ""),
            "last_run_at": str(j.get("last_run_at") or ""),
            "last_status": str(j.get("last_status") or ""),
            "last_error": str(j.get("last_error") or "")[:400],
            "last_delivery_error": str(j.get("last_delivery_error") or "")[:400],
            "runs": len(runs),
            "newest": runs[0]["name"] if runs else "",
            "files": runs[:MAX_FILES],
        })
    rows.sort(key=lambda r: r["newest"], reverse=True)
    return {"ts": int(time.time()), "jobs": rows, "output_root": str(OUTPUT_ROOT)}


def read_output(job_id: str, name: str) -> dict:
    """One run's text. Both path parts are validated — no traversal, .md only."""
    if not job_id or not name:
        return {"ok": False, "error": "missing job or file"}
    bad = (lambda s: (not s) or s.startswith(".") or "/" in s or "\\" in s or ".." in s)
    if bad(job_id) or bad(name) or not name.endswith(".md"):
        return {"ok": False, "error": "invalid job or file name"}
    path = (OUTPUT_ROOT / job_id / name)
    try:
        real = path.resolve()
        if OUTPUT_ROOT.resolve() not in real.parents:
            return {"ok": False, "error": "outside the cron output root"}
        text = real.read_text(errors="replace")
        mtime = int(real.stat().st_mtime)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    truncated = len(text) > MAX_BYTES
    return {"ok": True, "job_id": job_id, "name": name, "truncated": truncated,
            "mtime": mtime, "text": text[:MAX_BYTES]}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(json.dumps(read_output(sys.argv[1], sys.argv[2]), indent=2))
    else:
        print(json.dumps(jobs_overview(), indent=2))
