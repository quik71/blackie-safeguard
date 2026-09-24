#!/usr/bin/env python3
"""Safeguard health engine — single source of truth for box status.

Used by BOTH the `blackie` CLI and the web dashboard. Returns structured
status for every critical service. Never raises; every check degrades to a
safe "unknown" so a broken check can't crash the whole report.

Status codes: green / red / yellow / unknown
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
BACKUP_ROOT = HOME / "hermes-backups"
MIRROR_ROOT = Path("/srv/storage/backups/hermes")
RUNTIME_DIR = f"/run/user/{os.getuid()}"
ENV = dict(os.environ)
ENV["XDG_RUNTIME_DIR"] = RUNTIME_DIR

# (label, systemd --user unit, port:listen, port:bind, process-name-match)
# port checks are best-effort; a service may be fine without listening.
SERVICES = [
    ("Hermes gateway",   "hermes-gateway.service",  None, 9119, "hermes_cli.main"),
    ("Neo4j",            "neo4j.service",            None, 7687, "neo4j"),
    ("IB Gateway",       None,                       None, 4002, "org.jtrader"),
    ("Factorio",         "factorio.service",         None, None, "factorio"),
    ("Tailscale",        "tailscaled.service",       None, None, "tailscaled"),
    ("Firecrawl API",    None,                       None, 3002, "api.js"),
]


def _run(cmd: list, timeout: float = 8) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=ENV, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _systemctl(unit: str) -> dict:
    """Check a unit as user first, then system (some units are system-level).
    Returns {'active': ..., 'enabled': ...} or defaults for 'absent'."""
    active, enabled = "absent", "absent"
    # user units
    for flag in ("is-active", "is-enabled"):
        r = _run(["systemctl", "--user", flag, unit])
        if r and r.returncode == 0 and r.stdout.strip():
            if flag == "is-active":
                active = r.stdout.strip()
            else:
                enabled = r.stdout.strip()
    # system units (fall back if user reported absent)
    if active in ("absent", ""):
        r = _run(["systemctl", "is-active", unit])
        if r and r.returncode == 0 and r.stdout.strip():
            active = r.stdout.strip()
    if enabled in ("absent", ""):
        r = _run(["systemctl", "is-enabled", unit])
        if r and r.returncode == 0 and r.stdout.strip():
            enabled = r.stdout.strip()
    return {"active": active, "enabled": enabled}


def _port_open(host: str, port: int, timeout: float = 2) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proc_running(name: str) -> bool:
    r = _run(["pgrep", "-f", name])
    return bool(r and r.returncode == 0)


def check_services() -> list[dict]:
    out = []
    for label, unit, bind_host, port, proc in SERVICES:
        status = "unknown"
        detail = ""
        # 1) systemd unit (authoritative when present)
        if unit:
            u = _systemctl(unit)
            active = u["active"]
            enabled = u["enabled"]
            if active == "active":
                status = "green"
                detail = f"unit active (enabled: {enabled})"
            elif active == "failed":
                status = "red"
                detail = f"unit failed (enabled: {enabled})"
            elif active in ("inactive", "deactivating", "activating"):
                status = "yellow"
                detail = f"unit {active} (enabled: {enabled})"
            else:
                status = "unknown"
                detail = f"unit not installed (enabled: {enabled})"
        # 2) port fallback (IB gateway, firecrawl have no unit)
        elif port:
            if _port_open(bind_host or "127.0.0.1", port):
                status = "green"
                detail = f"listening on {port}"
            elif _proc_running(proc or ""):
                status = "yellow"
                detail = f"process up, not listening on {port}"
            else:
                status = "red"
                detail = f"not listening on {port}"
        # 3) process fallback (Factorio has a unit already handled; this is belt-and-suspenders)
        elif proc:
            status = "green" if _proc_running(proc) else "red"
            detail = "process match"
        out.append({"label": label, "status": status, "detail": detail})
    return out


def check_tailscale() -> dict:
    r = _run(["tailscale", "status", "--json"])
    if not r or r.returncode != 0:
        return {"label": "Tailscale", "status": "red",
                "detail": "tailscale status failed", "nodes": 0, "self": None}
    try:
        data = json.loads(r.stdout)
        self = data.get("Self", {})
        # Tailscale --json exposes peers as a dict keyed by hostname, not a list
        nodes = data.get("Peer", {}) or {}
        self_status = self.get("BackendState", "unknown")
        online = self.get("Online", False)
        status = "green" if online else "red"
        return {"label": "Tailscale", "status": status,
                "detail": f"{self_status} — {len(nodes)} peers",
                "nodes": len(nodes), "self": self.get("HostName"),
                "ip": (self.get("TailscaleIPs") or [None])[0],
                "online": bool(online)}
    except (ValueError, KeyError):
        return {"label": "Tailscale", "status": "unknown", "detail": "parse error"}


def check_backups() -> dict:
    """Return newest backup + count of last 7 days, from mirror or local."""
    root = MIRROR_ROOT if MIRROR_ROOT.is_dir() else BACKUP_ROOT
    dirs = sorted([p for p in root.iterdir() if p.is_dir()
                   and p.name[0:4].isdigit()], reverse=True)
    week = [d for d in dirs if time.time() - d.stat().st_mtime < 7 * 86400]
    newest = dirs[0] if dirs else None
    return {
        "root": str(root),
        "count_7d": len(week),
        "total": len(dirs),
        "newest": newest.name if newest else None,
        "list": [d.name for d in week[:7]],
    }


def check_disk() -> dict:
    import shutil
    try:
        s = shutil.disk_usage("/")
        pct = round(s.used / s.total * 100, 1)
        status = "green" if pct < 85 else ("yellow" if pct < 95 else "red")
        return {"label": "Root disk", "status": status,
                "detail": f"{pct}% used ({s.used // 10**9}G / {s.total // 10**9}G)"}
    except OSError:
        return {"label": "Root disk", "status": "unknown", "detail": "stat failed"}


def check_wal_frozen() -> dict:
    """Detect the state.db WAL freeze (the one thing that's bitten us)."""
    db = HERMES_HOME / "state.db"
    wal = HERMES_HOME / "state.db-wal"
    if not wal.exists():
        return {"label": "state.db WAL", "status": "green",
                "detail": "no frozen WAL"}
    try:
        age = time.time() - wal.stat().st_mtime
        # wal-watchdog clears this; if it's been frozen >30min it's a problem
        status = "green" if age < 1800 else "yellow"
        return {"label": "state.db WAL", "status": status,
                "detail": f"WAL {int(age // 60)}m old (watchdog active)" if age < 1800
                         else f"WAL {int(age // 60)}m old — watchdog may have stalled"}
    except OSError:
        return {"label": "state.db WAL", "status": "unknown", "detail": "stat failed"}


def full_report() -> dict:
    services = check_services()
    ts = check_tailscale()
    services.append(ts)
    services.append(check_disk())
    services.append(check_wal_frozen())
    backups = check_backups()
    counts = {"green": 0, "red": 0, "yellow": 0, "unknown": 0}
    for s in services:
        counts[s["status"]] += 1
    return {
        "ts": int(time.time()),
        "overall": "red" if counts["red"] else ("yellow" if counts["yellow"] else "green"),
        "counts": counts,
        "services": services,
        "backups": backups,
    }


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "menu":
        menu()
    else:
        print(json.dumps(full_report(), indent=2))


def menu():
    """Pretty one-line-per-service status, for the `blackie` CLI."""
    d = full_report()
    sym = {"green": "🟢", "red": "🔴", "yellow": "🟡", "unknown": "⚪"}
    c = d["counts"]
    print(f"BLACKIE STATUS — {d['overall'].upper()}  "
          f"({c['green']}ok {c['red']}down {c['yellow']}warn {c['unknown']}?)")
    print("=" * 50)
    for s in d["services"]:
        print(f"  {sym[s['status']]}  {s['label']:16} {s['detail']}")
    b = d["backups"]
    print("=" * 50)
    print(f"  Backups: {b['count_7d']} in last 7d · newest {b['newest'] or 'none'}")
    print("  Dashboard: http://100.94.103.75:8080")


if __name__ == "__main__":
    main()
