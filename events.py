#!/usr/bin/env python3
"""Safeguard events — aggregates every safeguard log into one timeline.

This is the web-based replacement for the Telegram alerts. It reads:
  - doctor.log      (auto-heal runs: restarts, WAL recovery, tailscale, pages)
  - actions.log     (manual restarts + restore attempts from the web UI)
  - restore.log     (restore verification + restore steps)
  - safeguard-watchdog.log (system cron doctor runs)

Returns a unified, reverse-chronological list of events. Each event has:
  {ts, level, source, message}
where level is info/success/warn/error.
"""
from __future__ import annotations

import re
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
SAFE = HERMES_HOME / "safeguard"

# Regex to parse "2026-09-24 09:35:03 MESSAGE" log lines
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")


def _parse_line(line: str, source: str) -> dict | None:
    line = line.rstrip("\n")
    if not line.strip():
        return None
    m = TS_RE.match(line)
    ts = m.group(1) if m else "?"
    msg = m.group(2) if m else line
    level = "info"

    # doctor.log / actions.log patterns
    low = msg.lower()
    if any(k in low for k in ("complete", "nothing needed")):
        level = "success"
    elif any(k in low for k in ("restarted", "recovered", "cleared", "reconnected",
                                 "verif", "complete", "active")):
        level = "success"
    elif any(k in low for k in ("failed", "aborted", "stalled", "did not restart",
                                 "still ", "alert sent", "page")):
        level = "warn"
    elif any(k in low for k in ("restart", "restore", "verify", "extract", "stop")):
        level = "info"
    elif any(k in low for k in ("error", "exception")):
        level = "error"

    return {"ts": ts, "level": level, "source": source, "message": msg.strip()}


def _read_log(path: Path, source: str, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    out = []
    for line in lines:
        ev = _parse_line(line, source)
        if ev:
            out.append(ev)
    return out


def events(limit: int | None = None) -> list[dict]:
    """Return unified timeline (newest first), capped at `limit`.

    limit=None returns every event across all safeguard logs.
    """
    chunks = []
    chunks += _read_log(SAFE / "doctor.log", "doctor", None)
    chunks += _read_log(SAFE / "actions.log", "actions", None)
    chunks += _read_log(SAFE / "restore.log", "restore", None)
    chunks += _read_log(SAFE / "safeguard-watchdog.log", "watchdog", None)
    chunks += _read_log(SAFE / "telegram.log", "telegram", None)

    # newest first; stable sort by timestamp desc
    chunks.sort(key=lambda e: e["ts"], reverse=True)
    return chunks[:limit]


def counts() -> dict:
    evs = events(limit=500)
    return {"total": len(evs),
            "success": sum(1 for e in evs if e["level"] == "success"),
            "warn": sum(1 for e in evs if e["level"] == "warn"),
            "error": sum(1 for e in evs if e["level"] == "error"),
            "info": sum(1 for e in evs if e["level"] == "info")}


if __name__ == "__main__":
    import json
    print(json.dumps(counts(), indent=2))
    print("--- last 10 events ---")
    for e in events(10):
        print(f"  {e['ts']}  {e['level']:6}  {e['source']:10}  {e['message']}")
