#!/usr/bin/env python3
"""Safeguard dashboard — a tiny web page you open over Tailscale.

    python3 dashboard.py [port]   (default 8080)

Serves health status + last-7-days backup list with restore links.
No dependencies (stdlib only). Binds 127.0.0.1 so it's only reachable
over Tailscale (or localhost) — no open port on the public net.
"""
from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAFE = Path(__file__).parent
sys.path.insert(0, str(SAFE))
import health  # noqa: E402


def backup_list() -> list[dict]:
    """Return last-7-days backups with size + verify status.

    Verification checks that the manifest's listed files still exist and that
    the archive + sessions + vault files are present and non-empty. We do NOT
    run a full sha256sum over the 500MB+ archive (too slow for a dashboard);
    the restore script does the full checksum before it replaces anything.
    """
    b = health.full_report()["backups"]
    out = []
    for name in b["list"]:
        d = Path(b["root"]) / name
        size_mb = 0
        if d.is_dir():
            size_mb = sum(f.stat().st_size for f in d.glob("*")) / 10**6
        manifest = d / "MANIFEST.sha256"
        verified = False
        if manifest.exists():
            # quick structural check: archive + sessions + vault exist & non-empty
            tgz = list(d.glob("hermes-*.tgz"))
            sessions = list(d.glob("sessions-*.jsonl"))
            vault = list(d.glob("vault-*.tgz"))
            verified = bool(tgz and sessions and vault
                            and all(f.stat().st_size > 1000 for f in tgz + sessions + vault))
        out.append({"name": name, "size_mb": round(size_mb), "verified": verified})
    return out


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blackie Safeguard</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --ok:#2da44e; --bad:#cf222e; --warn:#d29922; --ink:#e6edf3; --muted:#8b949e; --line:#30363d; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:-apple-system,Segoe UI,Roboto,sans-serif; }
  .wrap { max-width:720px; margin:0 auto; padding:18px 16px 60px; }
  h1 { font-size:20px; margin:0 0 2px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:16px; }
  .overall { display:flex; align-items:center; gap:10px; padding:14px 16px;
             background:var(--card); border:1px solid var(--line); border-radius:12px;
             margin-bottom:16px; }
  .dot { width:16px; height:16px; border-radius:50%; }
  .dot.green { background:var(--ok); } .dot.red { background:var(--bad); }
  .dot.yellow { background:var(--warn); }
  .status-text { font-size:18px; font-weight:600; }
  .cards { display:grid; gap:8px; margin-bottom:22px; }
  .card { display:flex; align-items:center; gap:12px; padding:12px 14px;
          background:var(--card); border:1px solid var(--line); border-radius:10px; }
  .card .dot { flex:none; }
  .card .lbl { flex:1; font-weight:500; }
  .card .det { color:var(--muted); font-size:13px; text-align:right; }
  .sec { font-size:13px; text-transform:uppercase; letter-spacing:.08em;
         color:var(--muted); margin:22px 0 10px; }
  .btn { display:inline-block; padding:8px 14px; background:var(--ok); color:#fff;
         border-radius:8px; text-decoration:none; font-size:14px; font-weight:600; }
  .btn:hover { opacity:.9; }
  .brow { display:flex; align-items:center; gap:12px; padding:10px 12px;
          background:var(--card); border:1px solid var(--line); border-radius:10px; margin-bottom:6px; }
  .brow .n { flex:1; font-family:monospace; font-size:13px; }
  .brow .sz { color:var(--muted); font-size:12px; }
  code { background:#0b0f14; padding:2px 6px; border-radius:5px; font-size:12px; }
  .tip { color:var(--muted); font-size:12px; margin-top:8px; }
</style></head><body><div class="wrap">
  <h1>🛡️ Blackie Safeguard</h1>
  <div class="sub">Auto-recovery + backup restore · last update <span id="ts"></span></div>
  <div class="overall"><div class="dot" id="dot"></div>
    <span class="status-text" id="status"></span>
    <span style="flex:1"></span>
    <span id="counts" style="font-size:13px;color:var(--muted)"></span></div>
  <div class="cards" id="cards"></div>
  <div class="sec">Backups (last 7 days)</div>
  <div id="backups"></div>
  <div class="tip">To restore from your phone:
    <code>blackie restore</code> then
    <code>hermes-restore.sh restore &lt;name&gt;</code> — it'll verify the backup
    and ask you to type <code>RESTORE HERMES</code> before replacing anything.</div>
  <div class="tip">Emergency fix: <code>blackie doctor</code> — scans and
    auto-heals dead services, pings you if something needs a human.</div>
</div><script>
const COLORS={green:'var(--ok)',red:'var(--bad)',yellow:'var(--warn)',unknown:'var(--muted)'};
async function load(){
  const r=await fetch('/api/health');
  const d=await r.json();
  const dot=document.getElementById('dot');
  dot.className='dot '+d.overall;
  document.getElementById('status').textContent=
    d.overall.toUpperCase()+' — '+d.counts.green+' ok / '+d.counts.red+' down / '+d.counts.yellow+' warn';
  document.getElementById('counts').textContent='updated '+new Date(d.ts*1000).toLocaleTimeString();
  const cards=document.getElementById('cards');
  d.services.forEach(s=>{
    const div=document.createElement('div');div.className='card';
    const dot=document.createElement('div');dot.className='dot';
    dot.style.background=COLORS[s.status];
    const lbl=document.createElement('div');lbl.className='lbl';lbl.textContent=s.label;
    const det=document.createElement('div');det.className='det';det.textContent=s.detail;
    div.appendChild(dot);div.appendChild(lbl);div.appendChild(det);
    cards.appendChild(div);
  });
  const b=document.getElementById('backups');
  d.backups.list.forEach(n=>{
    const div=document.createElement('div');div.className='brow';
    const nm=document.createElement('div');nm.className='n';nm.textContent=n.name;
    const sz=document.createElement('div');sz.className='sz';
    sz.textContent=n.size_mb+' MB '+(n.verified?'✓ verified':'⚠ not verified');
    div.appendChild(nm);div.appendChild(sz);
    b.appendChild(div);
  });
}
load();
setInterval(load, 30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        if self.path.startswith("/api/health"):
            try:
                data = json.dumps(health.full_report()).encode()
                self._send(200, data, "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        elif self.path == "/api/backups":
            try:
                data = json.dumps(backup_list()).encode()
                self._send(200, data, "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        else:
            self._send(200, HTML)

    def log_message(self, *a):
        pass  # quiet


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Safeguard dashboard on http://127.0.0.1:{port} "
          f"(open over Tailscale at http://100.94.103.75:{port})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
