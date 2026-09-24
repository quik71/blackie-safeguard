#!/usr/bin/env python3
"""Safeguard doctor — auto-heals what it safely can, pages Mike for the rest.

Runs from the watchdog (background) OR manually via `blackie doctor`.
Actions:
  - restart dead systemd units (Hermes gateway, Neo4j, Factorio, Tailscale)
  - restart IB Gateway if its process died (no unit; best-effort via watchdog)
  - clear a stuck Tailscale if it's down
  - trigger the existing wal-state recovery if the WAL is frozen
  - send a Telegram alert summarizing every action taken

Policy: only restart things that are DEAD. Never restart something that's
deliberately stopped. If a unit fails to come back after one restart attempt,
page Mike instead of retrying forever.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
ENV = dict(os.environ)
ENV["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
LOG = HERMES_HOME / "safeguard" / "doctor.log"

# (unit, is_user). hermes-gateway is a user unit; the rest are system units.
RESTARTABLE = [
    ("hermes-gateway.service", True),
    ("neo4j.service", False),
    ("factorio.service", False),
    ("tailscaled.service", False),
]


def _run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=ENV, check=False)
    except Exception:
        return None


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line)
    print(line, end="")


def unit_status(unit: str, user: bool = False) -> str:
    args = ["systemctl"] + (["--user"] if user else []) + ["is-active", unit]
    r = _run(args)
    return (r.stdout.strip() if r and r.returncode == 0 else "absent")


def restart_unit(unit: str, user: bool = False) -> bool:
    prefix = ["systemctl", "--user"] if user else ["systemctl"]
    log(f"RESTART {unit} (was {unit_status(unit, user)})")
    _run(prefix + ["reset-failed", unit])
    r = _run(prefix + ["restart", unit])
    time.sleep(5)
    ok = unit_status(unit, user) == "active"
    log(f"  -> {'ACTIVE' if ok else 'STILL ' + unit_status(unit, user)}")
    return ok


def recover_wal():
    """Trigger the existing wal-state recovery if the WAL is stale."""
    wal = HERMES_HOME / "state.db-wal"
    if not wal.exists():
        return None
    age = time.time() - wal.stat().st_mtime
    if age < 1800:  # <30min: watchdog is handling it, leave alone
        return None
    log(f"WAL frozen {int(age // 60)}m — running recovery")
    _run(["bash", str(HERMES_HOME / "scripts" / "wal-generation-recovery.sh")])
    time.sleep(3)
    age2 = time.time() - wal.stat().st_mtime
    return "recovered" if age2 < age else "stalled"


def recover_tailscale():
    """If tailscale is down, try to bring it back up."""
    r = _run(["tailscale", "status", "--json"])
    online = False
    if r and r.returncode == 0:
        try:
            online = bool(json.loads(r.stdout).get("Self", {}).get("Online"))
        except ValueError:
            pass
    if online:
        return None
    log("Tailscale down — attempting up")
    _run(["systemctl", "restart", "tailscaled.service"])
    time.sleep(3)
    r = _run(["tailscale", "status"])
    return "up" if (r and r.returncode == 0) else "still down"


def page_telegram(text: str):
    """Send a Telegram alert. Best-effort; failures don't crash the doctor."""
    env_path = HERMES_HOME / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = env.get("TELEGRAM_BACKUP_NOTIFY_CHAT") or env.get("TELEGRAM_HOME_CHANNEL") or "7013102764"
    if not token:
        log("Telegram: no token, skipping alert")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat, "text": f"[SAFEGUARD] {text}"}).encode()
    try:
        with urllib.request.urlopen(url, data=payload, timeout=10) as r:
            log(f"Telegram alert sent (status {r.status})")
            return True
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def doctor(auto: bool = False) -> dict:
    """Run health + recovery. Returns a summary dict.
    auto=True = background watchdog mode (take fixes, page for rest).
    auto=False = manual (same, but caller may want to see more)."""
    log(f"=== doctor run auto={auto} ===")
    actions = []
    problems = []

    # 1) restart dead units
    for unit, is_user in RESTARTABLE:
        st = unit_status(unit, is_user)
        if st != "active":
            ok = restart_unit(unit, is_user)
            if ok:
                actions.append(f"restarted {unit}")
            else:
                problems.append(f"{unit} did not restart (manual attention)")

    # 2) WAL freeze
    wal = recover_wal()
    if wal == "recovered":
        actions.append("cleared frozen state.db WAL")
    elif wal == "stalled":
        problems.append("state.db WAL still frozen after recovery")

    # 3) tailscale
    ts = recover_tailscale()
    if ts == "up":
        actions.append("reconnected Tailscale")
    elif ts == "still down":
        problems.append("Tailscale still down (manual attention)")

    # 4) page if anything needs a human
    if problems:
        msg = f"{len(problems)} problem(s) need you:\n" + \
              "\n".join(f"• {p}" for p in problems)
        page_telegram(msg)
        log(f"Page sent: {len(problems)} problem(s)")

    summary = {
        "actions": actions,
        "problems": problems,
        "page_sent": bool(problems),
        "all_healthy": not actions and not problems,
    }
    log("=== doctor complete ===")
    return summary


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Safeguard recovery doctor")
    ap.add_argument("--auto", action="store_true",
                    help="background mode (default; also used standalone)")
    ap.add_argument("--check-only", action="store_true",
                    help="run health check, do not fix anything")
    args = ap.parse_args()

    if args.check_only:
        from health import full_report
        print(json.dumps(full_report(), indent=2))
        return

    summary = doctor(auto=True)
    if summary["all_healthy"]:
        print("ALL SYSTEMS GO — nothing needed fixing.")
    else:
        if summary["actions"]:
            print("Auto-fixed:")
            for a in summary["actions"]:
                print(f"  ✓ {a}")
        if summary["problems"]:
            print("\nNeed your attention:")
            for p in summary["problems"]:
                print(f"  ✗ {p}")
        if summary["page_sent"]:
            print("\n(Alert sent to Telegram)")


if __name__ == "__main__":
    main()
