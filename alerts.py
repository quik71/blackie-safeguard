#!/usr/bin/env python3
"""Alert feed for the safeguard dashboard.

Since Telegram was retired (2026-09-26) the watchdogs and guards write to their
own logs instead of pushing anywhere. This module reads those logs, keeps the
lines that actually report trouble (or a recovery), and returns them
newest-first so the dashboard can show the freshest problem at a glance.

    python3 alerts.py                        # JSON feed + per-source index
    python3 alerts.py <source> [lines]       # raw tail of one source's log

Never raises: a missing or unreadable log is skipped, and a log with nothing
alert-worthy in it contributes nothing (that is the normal, quiet case).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME_BASE") or Path.home())
HH = Path(os.environ.get("HERMES_HOME") or (HOME / ".hermes"))

# (source name, path, human label). Order is display order in the raw-log picker.
SOURCES: list[tuple[str, Path, str]] = [
    ("alerts",       HH / "logs" / "alerts.log",                          "Shared alert channel"),
    ("main-llama",   HH / "logs" / "main-llama-watchdog.log",             "main llama-server guard"),
    ("wal-guard",    HH / "logs" / "wal-generation-watchdog.log",         "state.db WAL guard"),
    ("wal-recovery", HH / "logs" / "wal-generation-recovery.log",         "state.db WAL recovery"),
    ("neo4j-guard",  HH / "logs" / "mcp-neo4j-watchdog.log",              "Neo4j graph guard"),
    ("gw-guard",     HH / "logs" / "gateway-watchdog.log",                "Hermes gateway guard"),
    ("serve-fix",    HH / "logs" / "serve-restart-verify.log",            "serve/resume self-heal"),
    ("mcp-check",    HH / "logs" / "mcp-post-restart-check.log",          "MCP post-restart check"),
    ("nightly",      HOME / "hermes-backups" / "update-nightly.log",      "Nightly update"),
    ("oneshot",      HOME / "hermes-backups" / "oneshot-update.log",      "Oneshot update"),
    ("state-reopen", HOME / "hermes-backups" / "state-db-reopen.log",     "state.db reopen"),
    ("vision",       HOME / "hermes-backups" / "gateway-restart-vision.log", "Vision gateway restart"),
    ("sf-watchdog",  HOME / "hermes-backups" / "safeguard-watchdog.log",  "Safeguard watchdog (cron)"),
]

TAIL_BYTES = 128 * 1024     # per file, on the alert scan
RAW_TAIL_LINES = 250        # per file, on the raw viewer
MAX_ALERTS = 80             # newest-first cap for the feed
MAX_PER_SOURCE = 8          # one chatty guard must not fill the whole feed

TS_RE = re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
RECOVERY_RE = re.compile(r"healthy again|recovered|cleared|back to healthy|restored", re.I)
ERROR_RE = re.compile(
    r"error|fail|fatal|exception|traceback|\bdown\b|degraded|critical|refused|"
    r"timed out|timeout|unhealthy|exited|\bnot answering\b|tripped|corrupt|lost|"
    r"split|panic|misconfigur", re.I)
WARN_RE = re.compile(
    r"warn|alert|retry|skipp|held|paus|stall|budget|exhaust|slow|missing|"
    r"mismatch|dirty|unable|cannot|no output", re.I)

# Lines that match the patterns above but mean nothing. This is the difference
# between a useful alert panel and 400 lines of apt boilerplate.
NOISE_RE = re.compile(
    r"apt does not have a stable CLI interface|Not Upgrading|"
    r"For further information visit|^\s*raise |^\s*File \"|__pycache__|"
    r"dependencies unchanged|Ignoring invalid `SSL_CERT_FILE`|Installed \d+ package|"
    r"Local changes were restored on top|Not Found: |^\s*\^+$|"
    r"version: Hermes Agent|Hermes Agent v\d+\.|upstream [0-9a-f]{7}", re.I)

LEVEL_RANK = {"error": 0, "warn": 1, "ok": 2}


def _tail_lines(path: Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    """Last chunk of a file as lines. Never raises; returns [] on any problem."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
                chunk = fh.read()
                first = chunk.find(b"\n")          # drop the half line we landed in
                chunk = chunk[first + 1:] if first != -1 else chunk
            else:
                chunk = fh.read()
        return chunk.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _classify(line: str) -> str | None:
    """error / warn / ok (recovery), or None when the line is routine noise."""
    if RECOVERY_RE.search(line):
        return "ok"
    if ERROR_RE.search(line):
        return "error"
    if WARN_RE.search(line):
        return "warn"
    return None


def _stamp(line: str) -> tuple[str, float]:
    """(display timestamp, sortable epoch) from a line's leading timestamp."""
    m = TS_RE.match(line.strip())
    if not m:
        return "", 0.0
    disp = f"{m.group(1)} {m.group(2)}"
    try:
        epoch = datetime.strptime(disp, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        epoch = 0.0
    return disp, epoch


def feed() -> dict:
    alerts, index = [], []
    for name, path, label in SOURCES:
        lines = _tail_lines(path)
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            mtime = 0
        # A log that timestamps its lines is a log whose untimestamped lines are
        # continuations (traceback frames, "For further information…"). Count
        # them as one entry, not five.
        stamped = any(TS_RE.match(ln.strip()) for ln in lines)
        found = 0
        for raw in lines:
            line = raw.strip()
            if not line or NOISE_RE.search(line):
                continue
            ts, epoch = _stamp(line)
            if not ts:
                if stamped:
                    continue
                epoch = float(mtime)          # purely untimestamped log: order by file
            level = _classify(line)
            if not level:
                continue
            msg = TS_RE.sub("", line).strip() or line
            alerts.append({
                "source": name, "label": label, "level": level,
                "ts": ts, "epoch": epoch,
                "age_s": (int(time.time() - epoch) if epoch else None),
                "message": msg[:400],
            })
            found += 1
        index.append({"source": name, "label": label, "path": str(path),
                      "exists": path.exists(), "alerts": found,
                      "mtime": mtime,
                      "size": (path.stat().st_size if path.exists() else 0)})

    alerts.sort(key=lambda a: a["epoch"], reverse=True)

    # A quiet box should not be drowned by one noisy guard: newest first, but at
    # most MAX_PER_SOURCE lines per source in the feed.
    kept, per_source = [], {}
    for a in alerts:
        if per_source.get(a["source"], 0) >= MAX_PER_SOURCE:
            continue
        per_source[a["source"]] = per_source.get(a["source"], 0) + 1
        kept.append(a)
        if len(kept) >= MAX_ALERTS:
            break

    counts = {"error": 0, "warn": 0, "ok": 0}
    for a in kept:
        counts[a["level"]] += 1
    for s in index:
        s["shown"] = per_source.get(s["source"], 0)
    newest = kept[0] if kept else None
    return {
        "ts": int(time.time()),
        "alerts": kept,
        "total": len(alerts),
        "counts": counts,
        "unhealthy": counts["error"] > 0,
        "newest": ({k: newest[k] for k in ("source", "label", "level", "ts", "message")}
                   if newest else None),
        "sources": index,
    }


def tail(source: str, lines: int | str = RAW_TAIL_LINES) -> dict:
    """Raw last-lines view of one source's log."""
    entry = next((s for s in SOURCES if s[0] == source), None)
    if not entry:
        return {"ok": False, "error": "unknown source"}
    name, path, label = entry
    try:
        lines = max(1, min(int(lines), 1000))
    except (TypeError, ValueError):
        lines = RAW_TAIL_LINES
    if not path.exists():
        return {"ok": False, "error": f"no such log: {path}", "label": label}
    raw = _tail_lines(path, max_bytes=1024 * 1024)
    shown = raw[-lines:]
    try:
        mtime = int(path.stat().st_mtime)
        size = path.stat().st_size
    except OSError:
        mtime, size = 0, 0
    return {"ok": True, "source": name, "label": label, "path": str(path),
            "lines_shown": len(shown), "lines_in_tail": len(raw),
            "size": size, "mtime": mtime, "text": "\n".join(shown)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        print(json.dumps(tail(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else RAW_TAIL_LINES),
                         indent=2))
    else:
        d = feed()
        print(json.dumps(d, indent=2)[:6000])
