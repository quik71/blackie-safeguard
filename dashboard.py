#!/usr/bin/env python3
"""Safeguard operations center — a web dashboard you open over Tailscale.

    python3 dashboard.py [port]   (default 8080)

Four panels:
  1. Status      — health of every critical service (green/red/yellow)
  2. Restore     — dropdown of ALL restore points (newest first) + restore button
  3. Actions     — restart buttons per service (gateway, neo4j, factorio, tailscale)
  4. Activity    — unified log timeline (doctor + actions + restore + watchdog)

Stdlib only. POST endpoints: /api/restart, /api/restore. The web UI confirms
before destructive actions. Binds 0.0.0.0 so it's reachable over Tailscale.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAFE = Path(__file__).parent
sys.path.insert(0, str(SAFE))
import health  # noqa: E402
import actions  # noqa: E402
import events  # noqa: E402


# ---- HTML ------------------------------------------------------------------

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blackie Safeguard</title>
<style>
  :root {
    --bg:#0d1117; --card:#161b22; --ok:#2da44e; --bad:#cf222e;
    --warn:#d29922; --ink:#e6edf3; --muted:#8b949e; --line:#30363d;
    --blue:#388bfd; --card2:#0d1117;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:-apple-system,Segoe UI,Roboto,sans-serif; }
  .wrap { max-width:820px; margin:0 auto; padding:18px 16px 60px; }
  h1 { font-size:20px; margin:0 0 2px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:16px; }
  .overall { display:flex; align-items:center; gap:10px; padding:14px 16px;
             background:var(--card); border:1px solid var(--line);
             border-radius:12px; margin-bottom:16px; }
  .dot { width:16px; height:16px; border-radius:50%; flex:none; }
  .dot.green { background:var(--ok); } .dot.red { background:var(--bad); }
  .dot.yellow { background:var(--warn); }
  .status-text { font-size:18px; font-weight:600; }
  .cards { display:grid; gap:8px; margin-bottom:8px; }
  .card { display:flex; align-items:center; gap:12px; padding:12px 14px;
          background:var(--card); border:1px solid var(--line);
          border-radius:10px; }
  .card .lbl { flex:1; font-weight:500; }
  .card .det { color:var(--muted); font-size:13px; text-align:right; }
  .panel { background:var(--card); border:1px solid var(--line);
           border-radius:12px; padding:16px; margin-bottom:16px; }
  .panel h2 { font-size:14px; text-transform:uppercase; letter-spacing:.08em;
              color:var(--muted); margin:0 0 12px; }
  .btn { display:inline-block; padding:8px 14px; background:var(--blue);
         color:#fff; border:none; border-radius:8px; font-size:14px;
         font-weight:600; cursor:pointer; }
  .btn:hover { opacity:.9; }
  .btn:disabled { opacity:.4; cursor:progress; }
  .btn.danger { background:var(--bad); }
  .btn.small { padding:5px 10px; font-size:12px; }
  select, .fselect { width:100%; padding:9px 12px; background:var(--card2);
         color:var(--ink); border:1px solid var(--line); border-radius:8px;
         font-size:14px; font-family:monospace; }
  .brow { display:flex; align-items:center; gap:12px; padding:10px 12px;
          background:var(--card2); border:1px solid var(--line);
          border-radius:10px; margin-bottom:6px; }
  .brow .n { flex:1; font-family:monospace; font-size:13px; }
  .brow .sz { color:var(--muted); font-size:12px; }
  .action-row { display:flex; align-items:center; gap:12px; padding:10px 12px;
          background:var(--card2); border:1px solid var(--line);
          border-radius:10px; margin-bottom:6px; }
  .action-row .lbl { flex:1; }
  .action-row .st { font-size:12px; color:var(--muted); }
  .actok { color:var(--ok); font-size:12px; }
  .actbad { color:var(--bad); font-size:12px; }
  code { background:#0b0f14; padding:2px 6px; border-radius:5px; font-size:12px; }
  .tip { color:var(--muted); font-size:12px; margin-top:8px; }
  .log { max-height:340px; overflow-y:auto; font-family:monospace; font-size:12px; }
  .log .ev { padding:5px 8px; border-bottom:1px solid var(--line); }
  .log .ev:last-child { border-bottom:none; }
  .log .ts { color:var(--muted); margin-right:8px; }
  .log .src { color:var(--blue); margin-right:6px; }
  .log .warn { color:var(--warn); }
  .log .error { color:var(--bad); }
  .log .success { color:var(--ok); }
  .banner { padding:10px 14px; border-radius:10px; margin-bottom:16px;
            border:1px solid var(--line); background:var(--card2); font-size:13px;
            display:none; }
  .banner.ok { border-color:var(--ok); color:var(--ok); }
  .banner.err { border-color:var(--bad); color:var(--bad); }
</style></head><body><div class="wrap">
  <h1>🛡️ Blackie Safeguard</h1>
  <div class="sub">Auto-recovery · backup restore · operations center ·
     last update <span id="ts"></span></div>

  <div class="banner" id="banner"></div>

  <div class="overall"><div class="dot" id="dot"></div>
    <span class="status-text" id="status"></span>
    <span style="flex:1"></span>
    <span id="counts" style="font-size:13px;color:var(--muted)"></span></div>

  <div class="panel"><h2>Status</h2>
    <div class="cards" id="cards"></div>
  </div>

  <div class="panel"><h2>Restore — pick a restore point</h2>
    <select id="restore-select"></select>
    <div class="tip">All restore points, newest first. Restoring will verify the
      backup's SHA manifest, restart Hermes, and replace <code>~/.hermes</code>.
      This cannot be undone — pick carefully.</div>
    <div style="margin-top:10px">
      <button class="btn danger" id="restore-btn" onclick="doRestore()">
        Restore Selected</button>
    </div>
  </div>

  <div class="panel"><h2>Actions — restart services</h2>
    <div id="actions"></div>
  </div>

  <div class="panel"><h2>Activity Log</h2>
    <div class="log" id="log"></div>
  </div>
</div><script>
const COLORS={green:'var(--ok)',red:'var(--bad)',yellow:'var(--warn)',unknown:'var(--muted)'};
function banner(msg,type){
  const b=document.getElementById('banner');
  b.className='banner '+(type||'');
  b.textContent=msg; b.style.display='block';
  setTimeout(()=>{b.style.display='none';},6000);
}
async function postJSON(url,data){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(data)});
  return r.json();
}
async function load(){
  try{
    const [hd,ed]=await Promise.all([
      fetch('/api/health').then(r=>r.json()),
      fetch('/api/events').then(r=>r.json())]);
    const dot=document.getElementById('dot');
    dot.className='dot '+hd.overall;
    document.getElementById('status').textContent=
      hd.overall.toUpperCase()+' — '+hd.counts.green+' ok / '+hd.counts.red+' down / '+hd.counts.yellow+' warn';
    document.getElementById('counts').textContent='updated '+new Date(hd.ts*1000).toLocaleTimeString();
    const cards=document.getElementById('cards');
    cards.innerHTML=''; // clear only; all values below use textContent
    hd.services.forEach(s=>{
      const div=document.createElement('div');div.className='card';
      const dot=document.createElement('div');dot.className='dot';
      dot.style.background=COLORS[s.status];
      const lbl=document.createElement('div');lbl.className='lbl';lbl.textContent=s.label;
      const det=document.createElement('div');det.className='det';det.textContent=s.detail;
      div.appendChild(dot);div.appendChild(lbl);div.appendChild(det);
      cards.appendChild(div);
    });
    // actions panel
    const ar=document.getElementById('actions');
    ar.innerHTML='';
    ed.actions.forEach(a=>{
      const div=document.createElement('div');div.className='action-row';
      const lbl=document.createElement('div');lbl.className='lbl';lbl.textContent=a.label;
      const st=document.createElement('div');st.className='st';st.textContent=a.status;
      const btn=document.createElement('button');btn.className='btn small';btn.textContent='Restart';
      btn.onclick=()=>doRestart(a);
      div.appendChild(lbl);div.appendChild(st);div.appendChild(btn);
      ar.appendChild(div);
    });
    // restore dropdown
    const sel=document.getElementById('restore-select');
    sel.innerHTML='';
    ed.backups.forEach(b=>{
      const opt=document.createElement('option');
      opt.value=b.name;
      opt.textContent=b.name+'  ('+b.size_mb+' MB '+(b.verified?'✓':'⚠')+')';
      sel.appendChild(opt);
    });
    // activity log
    const log=document.getElementById('log');
    log.innerHTML=''; // clear only; all values below use textContent
    ed.events.forEach(e=>{
      const div=document.createElement('div');div.className='ev '+e.level;
      const ts=document.createElement('span');ts.className='ts';ts.textContent=e.ts;
      const src=document.createElement('span');src.className='src';src.textContent='['+e.source+']';
      const msg=document.createElement('span');msg.textContent=e.message;
      div.appendChild(ts);div.appendChild(src);div.appendChild(msg);
      log.appendChild(div);
    });
  }catch(err){
    banner('Failed to load: '+err.message,'err');
  }
}
function doRestart(a){
  if(!confirm('Restart '+a.label+'?'))return;
  const btn=event.target;btn.disabled=true;btn.textContent='Restarting…';
  postJSON('/api/restart',{unit:a.unit,user:a.user}).then(r=>{
    btn.disabled=false;btn.textContent='Restart';
    if(r.ok){banner(a.label+' restarted (now '+r.after+')','ok');}
    else{banner(a.label+' restart failed: '+r.after,'err');}
    load();
  }).catch(e=>{banner('Error: '+e.message,'err');});
}
function doRestore(){
  const name=document.getElementById('restore-select').value;
  if(!confirm('Restore from '+name+'?\\n\\nThis replaces ~/.hermes after verifying the backup. Continue?'))return;
  const btn=document.getElementById('restore-btn');btn.disabled=true;btn.textContent='Restoring…';
  postJSON('/api/restore',{backup_dir:'/home/blackieserver/hermes-backups/'+name}).then(r=>{
    btn.disabled=false;btn.textContent='Restore Selected';
    if(r.ok){banner('Restore '+r.step+' complete. Gateway: '+r.gateway,'ok');}
    else{banner('Restore failed at '+r.step+': '+(r.error||'unknown'),'err');}
    load();
  }).catch(e=>{banner('Error: '+e.message,'err');});
}
load();
setInterval(load, 20000);
</script></body></html>"""


# ---- HTTP handler ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, data, code=200):
        try:
            self._send(code, json.dumps(data).encode(), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(),
                       "application/json")

    def do_GET(self):
        if self.path.startswith("/api/health"):
            try:
                self._json(health.full_report())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path == "/api/events":
            try:
                self._json({
                    "events": events.events(limit=100),
                    "counts": events.counts(),
                    "actions": actions.list_restartable(),
                    "backups": actions.list_backups_all(),
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._send(200, HTML)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode() if length else "{}"
            data = json.loads(raw) if raw else {}
        except (ValueError, OSError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
            return

        if self.path == "/api/restart":
            unit = data.get("unit")
            is_user = data.get("user", False)
            label = data.get("label", unit)
            if not unit:
                self._json({"error": "missing unit"}, 400)
                return
            try:
                self._json(actions.restart_unit(unit, is_user, label))
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path == "/api/restore":
            backup_dir = data.get("backup_dir")
            if not backup_dir:
                self._json({"error": "missing backup_dir"}, 400)
                return
            try:
                self._json(actions.restore_backup(backup_dir))
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass  # quiet


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Safeguard operations center on http://127.0.0.1:{port} "
          f"(open over Tailscale at http://100.94.103.75:{port})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
