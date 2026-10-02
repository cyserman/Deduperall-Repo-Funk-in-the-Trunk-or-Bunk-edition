#!/usr/bin/env python3
"""
dedupe_office.py - find duplicate Office files, review in a browser UI, quarantine with undo.

  python3 dedupe_office.py            -> opens the UI (http://127.0.0.1:8765)
  python3 dedupe_office.py --cli      -> dry-run report in terminal
  python3 dedupe_office.py --cli --move   -> quarantine the suggested dupes

Stdlib only. Nothing is ever deleted: dupes are MOVED to the quarantine folder
and logged in manifest.json there, so "Restore" puts everything back.
Local API (for an AnythingLLM agent skill): POST /api/scan, /api/move, /api/restore
with header X-Token (printed at startup / in the URL).
"""
import argparse, hashlib, json, os, re, secrets, shutil, sys, threading, time, webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".txt", ".pdf"}
SKIP = ["venv", "__pycache__", "site-packages", "node_modules", "dupe_quarantine"]
HOME = os.path.expanduser("~")
DEFAULT_Q = os.path.join(HOME, "dupe_quarantine")
PORT = 8765
TOKEN = secrets.token_urlsafe(16)
LAST_SCAN = set()  # only paths found by the latest scan may be moved

# ---------------- core ----------------
def iter_files(roots, exts, protect):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not d.startswith(".")
                     and not any(s in os.path.join(dp, d) for s in SKIP)
                     and not any(p and p.lower() in os.path.join(dp, d).lower() for p in protect)]
            for f in fn:
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in exts:
                    yield os.path.join(dp, f)

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()

BAD_NAME = re.compile(r"(copy|\(\d+\)|_final|_old|backup|~\$)", re.I)

def keeper_score(p):
    # lower = better keeper: clean name, shallow path, oldest (original) file
    return (bool(BAD_NAME.search(os.path.basename(p))), p.count(os.sep), os.path.getmtime(p))

def scan(roots, exts=EXTS, protect=(), min_size=1):
    by_size = defaultdict(list)
    for p in iter_files(roots, exts, protect):
        try:
            s = os.path.getsize(p)
            if s >= min_size:          # empty files are all "duplicates" of each other - skip
                by_size[s].append(p)
        except OSError:
            pass
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash = defaultdict(list)
        for p in paths:
            try:
                by_hash[sha256(p)].append(p)
            except OSError:
                pass
        for h, ps in by_hash.items():
            if len(ps) > 1:
                ps.sort(key=keeper_score)
                groups.append({"hash": h, "size": size,
                               "files": [{"path": p, "mtime": os.path.getmtime(p)} for p in ps]})
    groups.sort(key=lambda g: -g["size"] * (len(g["files"]) - 1))
    LAST_SCAN.clear()
    LAST_SCAN.update(f["path"] for g in groups for f in g["files"])
    return {"groups": groups,
            "dupes": sum(len(g["files"]) - 1 for g in groups),
            "reclaim_bytes": sum(g["size"] * (len(g["files"]) - 1) for g in groups)}

def _manifest_path(q): return os.path.join(q, "manifest.json")

def _load(q):
    try:
        with open(_manifest_path(q)) as f:
            return json.load(f)
    except Exception:
        return []

def quarantine(paths, qdir):
    os.makedirs(qdir, exist_ok=True)
    manifest, done, errs = _load(qdir), [], []
    for p in paths:
        if p not in LAST_SCAN or not os.path.isfile(p):
            errs.append(f"skipped (not from last scan / missing): {p}")
            continue
        base = "__".join(p.lstrip(os.sep).split(os.sep))[-200:]
        dest, i = os.path.join(qdir, base), 1
        while os.path.exists(dest):
            r, e = os.path.splitext(os.path.join(qdir, base)); dest = f"{r}__dup{i}{e}"; i += 1
        try:
            digest = sha256(p)
            shutil.move(p, dest)
            manifest.append({"orig": p, "quarantined": dest, "sha256": digest, "time": time.time()})
            done.append(p)
        except Exception as e:
            errs.append(f"{p}: {e}")
    with open(_manifest_path(qdir), "w") as f:
        json.dump(manifest, f, indent=1)
    return {"moved": len(done), "errors": errs}

def restore(qdir):
    manifest, keep, n, errs = _load(qdir), [], 0, []
    for m in manifest:
        try:
            if os.path.exists(m["orig"]):
                errs.append(f"original path occupied, left in quarantine: {m['orig']}"); keep.append(m); continue
            os.makedirs(os.path.dirname(m["orig"]), exist_ok=True)
            shutil.move(m["quarantined"], m["orig"]); n += 1
        except Exception as e:
            errs.append(f"{m['orig']}: {e}"); keep.append(m)
    with open(_manifest_path(qdir), "w") as f:
        json.dump(keep, f, indent=1)
    return {"restored": n, "errors": errs}

def mb(b): return f"{b/1048576:.1f} MB"

# ---------------- UI ----------------
PAGE = """<!doctype html><meta charset=utf-8><title>Dupe Cleaner</title>
<style>
body{font:14px system-ui;margin:0;background:#111;color:#ddd}header{padding:14px 20px;background:#1b1b1b;position:sticky;top:0;border-bottom:1px solid #333}
h1{font-size:16px;margin:0 0 8px}textarea,input{background:#222;color:#ddd;border:1px solid #444;border-radius:4px;padding:6px;width:100%;box-sizing:border-box;font:13px monospace}
.row{display:flex;gap:10px;margin:6px 0}.row>div{flex:1}button{background:#2d6cdf;color:#fff;border:0;border-radius:4px;padding:8px 14px;cursor:pointer;margin-right:6px}
button.g{background:#444}button.r{background:#b33}.grp{margin:10px 20px;padding:8px;background:#1a1a1a;border-radius:6px}
.f{display:flex;gap:8px;align-items:center;padding:2px 0;font:12px monospace;word-break:break-all}.keep{color:#6c6}.sum{margin:10px 20px;color:#9cf}label{font-size:12px;color:#999}
</style>
<header><h1>Dupe Cleaner <span style=color:#777>- nothing is deleted, only moved. Undo any time.</span></h1>
<div class=row><div><label>Folders to scan (one per line)</label><textarea id=roots rows=2>__HOME__</textarea></div>
<div><label>Protect - skip paths containing (one per line, e.g. evidence/legal folders)</label><textarea id=prot rows=2></textarea></div></div>
<div class=row><div><label>Quarantine folder</label><input id=q value="__Q__"></div>
<div><label>Extensions</label><input id=ext value="__EXTS__"></div></div>
<button onclick=scan()>Scan</button><button class=g onclick=sel(true)>Check all dupes</button><button class=g onclick=sel(false)>Uncheck all</button>
<button onclick=mv()>Move checked to quarantine</button><button class=r onclick=rest()>Undo: restore everything</button> <span id=st></span></header>
<div class=sum id=sum></div><div id=out></div>
<script>
const T="__TOKEN__";let G=[];
const api=(p,b)=>fetch(p,{method:'POST',headers:{'X-Token':T,'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
const lines=s=>document.getElementById(s).value.split('\\n').map(x=>x.trim()).filter(Boolean);
const mb=b=>(b/1048576).toFixed(1)+' MB';
async function scan(){st.textContent='scanning...';const r=await api('/api/scan',{roots:lines('roots'),protect:lines('prot'),exts:ext.value.split(/[ ,]+/).filter(Boolean)});
G=r.groups;st.textContent='';sum.textContent=`${G.length} groups, ${r.dupes} duplicate files, ${mb(r.reclaim_bytes)} reclaimable`;
out.innerHTML=G.map((g,i)=>`<div class=grp><b>${g.files.length} copies - ${mb(g.size)}</b>`+g.files.map((f,j)=>
`<div class=f><input type=radio style=width:auto name=k${i} ${j==0?'checked':''} onchange=pick(${i})><input type=checkbox style=width:auto data-g=${i} data-p="${f.path.replace(/"/g,'&quot;')}" ${j==0?'':'checked'}><span class="${j==0?'keep':''}">${f.path}</span></div>`).join('')+'</div>').join('');}
function pick(i){const g=document.querySelectorAll(`input[type=checkbox][data-g="${i}"]`);const r=document.querySelectorAll(`input[name=k${i}]`);r.forEach((x,j)=>g[j].checked=!x.checked);}
function sel(v){document.querySelectorAll('input[type=checkbox][data-p]').forEach(c=>{const i=c.dataset.g;const k=[...document.querySelectorAll(`input[name=k${i}]`)].findIndex(x=>x.checked);const idx=[...document.querySelectorAll(`input[type=checkbox][data-g="${i}"]`)].indexOf(c);c.checked=v&&idx!=k;});}
async function mv(){const p=[...document.querySelectorAll('input[type=checkbox][data-p]:checked')].map(c=>c.dataset.p);if(!p.length)return;
if(!confirm(`Move ${p.length} files to quarantine?`))return;const r=await api('/api/move',{paths:p,quarantine:q.value});st.textContent=`moved ${r.moved}`+(r.errors.length?`, ${r.errors.length} errors`:'');if(r.errors.length)alert(r.errors.join('\\n'));scan();}
async function rest(){if(!confirm('Put every quarantined file back where it came from?'))return;const r=await api('/api/restore',{quarantine:q.value});st.textContent=`restored ${r.restored}`;if(r.errors.length)alert(r.errors.join('\\n'));}
</script>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, "{}")
        page = (PAGE.replace("__TOKEN__", TOKEN).replace("__HOME__", HOME)
                .replace("__Q__", DEFAULT_Q).replace("__EXTS__", " ".join(sorted(EXTS))))
        self._send(200, page, "text/html; charset=utf-8")
    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, '{"error":"bad token"}')
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        q = b.get("quarantine") or DEFAULT_Q
        if self.path == "/api/scan":
            r = scan(b.get("roots") or [HOME], set(b.get("exts") or EXTS), b.get("protect") or [])
        elif self.path == "/api/move":
            r = quarantine(b.get("paths", []), q)
        elif self.path == "/api/restore":
            r = restore(q)
        else:
            return self._send(404, "{}")
        self._send(200, json.dumps(r))

def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Dupe Cleaner running at {url}   (Ctrl+C to quit)\nAPI token: {TOKEN}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

def cli(a):
    r = scan(a.root or [HOME], EXTS, a.protect or [])
    for i, g in enumerate(r["groups"], 1):
        print(f"\n=== Group {i} ({mb(g['size'])}) ===\nKEEP: {g['files'][0]['path']}")
        for f in g["files"][1:]: print("  dupe:", f["path"])
    print(f"\n{r['dupes']} dupes, {mb(r['reclaim_bytes'])} reclaimable")
    if a.move:
        print(quarantine([f["path"] for g in r["groups"] for f in g["files"][1:]], a.quarantine or DEFAULT_Q))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", action="store_true"); ap.add_argument("--move", action="store_true")
    ap.add_argument("--root", action="append"); ap.add_argument("--protect", action="append")
    ap.add_argument("--quarantine"); ap.add_argument("--port", type=int)
    a = ap.parse_args()
    if a.port: PORT = a.port
    cli(a) if a.cli else serve()
