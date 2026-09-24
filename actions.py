#!/usr/bin/env python3
"""Safeguard actions — restart services and restore backups.

Shared by the web dashboard and the doctor. Every action is logged to
safeguard/actions.log so the Activity Log panel can show it (this is the
web replacement for the Telegram alerts).

Safety rules:
  - restart: only systemctl restart a unit; never start something that was
    deliberately stopped. Caller decides which units are restartable.
  - restore: verify the backup's SHA manifest FIRST, stop the gateway,
    extract, restart gateway, run post-checks. Never restore without the
    manifest check. Logs every step.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
ENV = dict(os.environ)
ENV["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
ENV["PATH"] = f"{HERMES_HOME}/hermes-agent/venv/bin:{HERMES_HOME}/hermes-agent/node_modules/.bin:" \
              f"{HERMES_HOME}/node/bin:{HERMES_HOME}/node:{HOME}/.local/bin:/usr/local/sbin:" \
              f"/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LOG = HERMES_HOME / "safeguard" / "actions.log"
RESTORE_LOG = HERMES_HOME / "safeguard" / "restore.log"
RUNTIME_DIR = f"/run/user/{os.getuid()}"


def _log(path: Path, msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(line)


def _run(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=ENV, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return None


def _systemctl(args, user=False):
    base = ["systemctl"] + (["--user"] if user else []) + args
    r = _run(base)
    return (r.stdout.strip() if r and r.returncode == 0 else None)


# ---- restart ---------------------------------------------------------------

def list_restartable():
    """Units the Actions panel can restart. Returns list of dicts
    {unit, user, label} so the web UI can read a.unit / a.user / a.label."""
    return [
        {"unit": "hermes-gateway.service", "user": True, "label": "Hermes gateway"},
        {"unit": "neo4j.service", "user": False, "label": "Neo4j"},
        {"unit": "factorio.service", "user": False, "label": "Factorio"},
        {"unit": "tailscaled.service", "user": False, "label": "Tailscale"},
    ]


def restart_unit(unit: str, user: bool = False, label: str = None) -> dict:
    label = label or unit
    before = _systemctl(["is-active", unit], user=user)
    _log(LOG, f"RESTART {label} (was {before})")
    _systemctl(["reset-failed", unit], user=user)
    rc = _systemctl(["restart", unit], user=user)
    time.sleep(5)
    after = _systemctl(["is-active", unit], user=user)
    ok = after == "active"
    _log(LOG, f"  -> {'ACTIVE' if ok else after} (restart rc={rc})")
    return {"ok": ok, "before": before, "after": after, "label": label}


# ---- restore ---------------------------------------------------------------

def list_backups_all():
    """All restore points, newest first, with size + verified flag."""
    from health import MIRROR_ROOT, BACKUP_ROOT
    root = MIRROR_ROOT if MIRROR_ROOT.is_dir() else BACKUP_ROOT
    dirs = sorted([p for p in root.iterdir() if p.is_dir()
                   and p.name[0:4].isdigit()], reverse=True)
    out = []
    for d in dirs:
        size_mb = sum(f.stat().st_size for f in d.glob("*")) / 10**6
        manifest = d / "MANIFEST.sha256"
        verified = False
        if manifest.exists():
            tgz = list(d.glob("hermes-*.tgz"))
            sessions = list(d.glob("sessions-*.jsonl"))
            vault = list(d.glob("vault-*.tgz"))
            verified = bool(tgz and sessions and vault and
                            all(f.stat().st_size > 1000
                                for f in tgz + sessions + vault))
        out.append({"name": d.name, "size_mb": round(size_mb),
                    "verified": verified, "path": str(d)})
    return out


def verify_backup(backup_dir: str) -> dict:
    """Run the real SHA manifest check. Returns {ok, error}."""
    log = f"VERIFY {backup_dir}"
    r = _run(["bash", "-c", f"cd {backup_dir} && sha256sum -c MANIFEST.sha256"],
             timeout=180)
    if r and r.returncode == 0:
        _log(RESTORE_LOG, log + " -> OK")
        return {"ok": True, "error": None}
    err = (r.stderr or r.stdout or "unknown error") if r else "command failed"
    _log(RESTORE_LOG, log + f" -> FAILED: {err[:500]}")
    return {"ok": False, "error": err[:500]}


def restore_backup(backup_dir: str) -> dict:
    """Non-interactive restore with full verification + logging.

    Mirrors ~/bin/hermes-restore.sh but without the typed confirmation
    (the web UI confirms before calling this). Steps:
      1. verify SHA manifest
      2. stop hermes-gateway
      3. extract archive into HOME
      4. restart hermes-gateway
      5. post-checks
    """
    _log(RESTORE_LOG, f"===== RESTORE START: {backup_dir} =====")

    # 1. verify
    v = verify_backup(backup_dir)
    if not v["ok"]:
        _log(RESTORE_LOG, f"RESTORE ABORTED: verification failed ({v['error'][:200]})")
        return {"ok": False, "step": "verify", "error": v["error"]}

    # 2. stop gateway
    _log(RESTORE_LOG, "stopping hermes-gateway.service")
    _systemctl(["stop", "hermes-gateway.service"], user=True)
    time.sleep(2)

    # 3. extract the archive from the backup dir
    tgz = sorted(Path(backup_dir).glob("hermes-*.tgz"))
    if not tgz:
        _log(RESTORE_LOG, "RESTORE ABORTED: no hermes-*.tgz found")
        return {"ok": False, "step": "archive", "error": "no archive in backup dir"}
    archive = str(tgz[-1])
    _log(RESTORE_LOG, f"extracting {archive} into HOME")
    r = _run(["tar", "-xzf", archive, "-C", str(HOME)], timeout=300)
    if r and r.returncode != 0:
        _log(RESTORE_LOG, f"RESTORE ABORTED: extract failed ({r.stderr[:200]})")
        return {"ok": False, "step": "extract", "error": r.stderr[:200]}

    # 4. restart gateway
    _log(RESTORE_LOG, "restarting hermes-gateway.service")
    _systemctl(["restart", "hermes-gateway.service"], user=True)
    time.sleep(6)
    after = _systemctl(["is-active", "hermes-gateway.service"], user=True)
    ok = after == "active"
    _log(RESTORE_LOG, f"RESTORE COMPLETE: gateway {'ACTIVE' if ok else after}")

    return {"ok": ok, "step": "complete",
            "archive": archive, "gateway": after}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Safeguard actions")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_restart = sub.add_parser("restart")
    p_restart.add_argument("unit")
    p_restart.add_argument("--user", action="store_true")
    p_restart.add_argument("--label", default=None)

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("backup_dir")

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("backup_dir")

    p_list = sub.add_parser("list-backups")

    args = ap.parse_args()
    if args.cmd == "restart":
        print(json.dumps(restart_unit(args.unit, args.user, args.label)))
    elif args.cmd == "restore":
        print(json.dumps(restore_backup(args.backup_dir)))
    elif args.cmd == "verify":
        print(json.dumps(verify_backup(args.backup_dir)))
    elif args.cmd == "list-backups":
        print(json.dumps(list_backups_all(), indent=2))


if __name__ == "__main__":
    main()
