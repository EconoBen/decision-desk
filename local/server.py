#!/usr/bin/env python3
"""Serve the decision desk page locally, without a claude.ai artifact.

The page's data store, window.claude.use("db"), is replaced by JSON files at
<DATA>/<collection>/<id>.json. An injected shim polls this server, and agents
read and write the same files directly. Binds 127.0.0.1 only. Stdlib only.

Environment (all optional):
  DECISION_DESK_PAGE   the page; defaults to ../index.html, else ~/src/decision-desk/index.html
  DECISION_DESK_STATE  state folder; defaults to ~/.local/state/decision-desk
  DECISION_DESK_DATA   data folder; defaults to <state>/db
  DECISION_DESK_PORT   defaults to 8791
  DECISION_DESK_SPECTRUM_ENV, DECISION_DESK_NODE   the optional text-message line; see sms.py
"""
import hashlib, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sms

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("DECISION_DESK_STATE") or Path.home() / ".local/state/decision-desk")
DATA = Path(os.environ.get("DECISION_DESK_DATA") or STATE / "db")
PORT = int(os.environ.get("DECISION_DESK_PORT") or 8791)
PAGE = Path(os.environ.get("DECISION_DESK_PAGE") or next(
    (p for p in (HERE.parent / "index.html", Path.home() / "src/decision-desk/index.html") if p.exists()), HERE.parent / "index.html"))
NAME = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
TEXTING = None  # sms.Texting, set in main()

SHIM = """<script>
(() => {
  // The artifact's window.claude.use("db"), served from local JSON files.
  const j = (r) => { if (!r.ok) throw Object.assign(new Error(r.statusText), { code: r.status }); return r.json(); };
  const watchers = new Set(), cols = {};
  const col = (name) => cols[name] || (cols[name] = { writes: 0, epoch: 0, ver: null });
  // A watcher fires only when its value really changes.
  const show = (w, v) => { const s = JSON.stringify(v); if (s !== w.last) { w.last = s; w.val = v; w.cb(w.shape(v)); } };
  // A read that overlaps a write is dropped; the read after the write confirms it.
  const load = (w) => {
    const c = col(w.col), epoch = c.epoch;
    fetch("/db/" + w.path, { cache: "no-store" }).then(j).then((v) => { if (!c.writes && c.epoch === epoch && watchers.has(w)) show(w, v); }, () => {});
  };
  const reload = (name) => { for (const w of watchers) if (w.col === name) load(w); };
  // The server says which collections changed as they change, so nothing polls.
  new EventSource("/events").onmessage = (e) => {
    for (const [name, ver] of Object.entries(JSON.parse(e.data))) if (col(name).ver !== ver) { col(name).ver = ver; reload(name); }
  };
  const watch = (path, shape, cb) => {
    const [name, id] = path.split("/"), w = { path, col: name, id, shape, cb, last: null, val: null };
    watchers.add(w); load(w); return () => watchers.delete(w);
  };
  // Writes show at once, as they do in the artifact's store. The server's copy then confirms them, or undoes them if the write failed.
  const local = (path, val, merge) => {
    const [name, id] = path.split("/");
    for (const w of watchers) {
      if (w.col !== name || w.val == null) continue;
      if (w.id === id) show(w, { exists: true, data: merge ? { ...(w.val.data || {}), ...val } : val });
      else if (!w.id) {
        const rows = w.val.map((r) => (r.id === id ? { id, data: merge ? { ...r.data, ...val } : val } : r));
        if (!merge && !rows.some((r) => r.id === id)) rows.push({ id, data: val });
        show(w, rows);
      }
    }
  };
  const send = async (method, path, val) => {
    const name = path.split("/")[0], c = col(name), before = [...watchers].filter((w) => w.col === name && w.val != null).map((w) => [w, w.val]);
    c.writes++; local(path, val, method === "PATCH");
    try { return await fetch("/db/" + path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(val) }).then(j); }
    catch (e) { for (const [w, v] of before) show(w, v); throw e; } // undo at once, even if the server is gone
    finally { c.writes--; c.epoch++; reload(name); }
  };
  const docs = (field) => (rows) => {
    if (field) rows = rows.slice().sort((a, b) => ((a.data[field] ?? 0) > (b.data[field] ?? 0)) - ((a.data[field] ?? 0) < (b.data[field] ?? 0)));
    return { metadata: { fromCache: false }, docs: rows.map((r) => ({ id: r.id, data: () => r.data })) };
  };
  const collection = (name, field) => ({ orderBy: (f) => collection(name, f), onSnapshot: (cb) => watch(name, docs(field), cb) });
  const doc = (path) => ({
    onSnapshot: (cb) => watch(path, (r) => ({ exists: r.exists, data: () => r.data }), cb),
    set: (v) => send("PUT", path, v),
    update: (v) => send("PATCH", path, v),
  });
  window.claude = { local: true, use: async (k) => { if (k !== "db") throw new Error("only db"); return { collection, doc }; } };
})();
</script>
"""


# The store. The page's set/update and the text-message line both write through here.
_cache = {}  # path -> ((mtime_ns, size), parsed JSON)


def _load(f):
    """A file's JSON, parsed again only when its mtime or size changes. A half-written file keeps its last good copy."""
    st = f.stat()
    key, hit = (st.st_mtime_ns, st.st_size), _cache.get(f)
    if hit and hit[0] == key:
        return hit[1]
    try:
        data = json.loads(f.read_text())
    except ValueError:
        if hit:
            return hit[1]
        raise
    _cache[f] = (key, data)
    return data


def read_doc(col, id):
    try:
        return _load(DATA / col / f"{id}.json")
    except (OSError, ValueError):
        return None


def list_docs(col):
    out = []
    for f in sorted((DATA / col).glob("*.json")):
        try:
            out.append((f.stem, _load(f)))
        except (OSError, ValueError):
            pass  # removed or half written; the next change event brings it back
    return out


def versions():
    """A short fingerprint per collection from file names, mtimes and sizes: stat calls only, no reads."""
    out = {}
    for d in sorted(DATA.iterdir()) if DATA.is_dir() else []:
        if d.is_dir() and NAME.match(d.name):
            h = hashlib.blake2b(digest_size=8)
            for f in sorted(d.glob("*.json")):
                try:
                    st = f.stat()
                except OSError:
                    continue
                h.update(f"{f.name}:{st.st_mtime_ns}:{st.st_size};".encode())
            out[d.name] = h.hexdigest()
    return out


def write_doc(col, id, val, merge=False):
    """set (merge=False) or update (merge=True, doc must exist). Atomic: temp file, then rename."""
    f = DATA / col / f"{id}.json"
    if merge:
        if not f.exists():
            return False
        val = {**_load(f), **val}
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(val, indent=2))
    tmp.replace(f)
    return True


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trusted(self, write):
        # Only this page may call us: a Host check stops DNS rebinding, and requiring JSON plus a
        # same-origin Origin stops other sites from posting here (a text/plain form needs no preflight).
        hosts = {f"127.0.0.1:{self.server.server_address[1]}", f"localhost:{self.server.server_address[1]}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in hosts or (origin and origin not in {f"http://{h}" for h in hosts}):
            return False
        return not write or (self.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json"

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("too large")
        val = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(val, dict):
            raise ValueError("object required")
        return val

    def _parts(self):
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if parts[0] != "db" or not 2 <= len(parts) <= 3 or not all(NAME.match(p) for p in parts[1:]):
            return None
        return parts[1:]

    def do_GET(self):
        if not self._trusted(False):
            return self._json(403, {"error": "forbidden"})
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = PAGE.read_text().replace("<script>", SHIM + "<script>", 1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/events":
            return self._events()
        if path == "/sms/status":
            return self._json(*TEXTING.route("status", {}))
        parts = self._parts()
        if parts is None:
            return self._json(404, {"error": "not found"})
        if len(parts) == 1:
            return self._json(200, [{"id": i, "data": d} for i, d in list_docs(parts[0])])
        d = read_doc(*parts)
        return self._json(200, {"exists": d is not None, "data": d})

    def _events(self):
        """Server-sent events: the collection fingerprints whenever one changes, and every 15 s as a ping."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last, quiet = None, 0
        try:
            while True:
                v = versions()
                if v != last or quiet >= 150:
                    self.wfile.write(f"data: {json.dumps(v)}\n\n".encode())
                    self.wfile.flush()
                    last, quiet = v, 0
                time.sleep(0.1)  # ponytail: each open tab stats the store 10 times a second; one shared watcher if tabs pile up
                quiet += 1
        except OSError:
            pass  # the tab went away

    def _write(self, merge):
        if not self._trusted(True):
            return self._json(403, {"error": "forbidden"})
        parts = self._parts()
        if parts is None or len(parts) != 2:
            return self._json(404, {"error": "not found"})
        try:
            val = self._body()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if not write_doc(*parts, val, merge):
            return self._json(404, {"error": "no such doc"})
        return self._json(200, {"ok": True})

    def do_PUT(self):
        self._write(merge=False)

    def do_PATCH(self):
        self._write(merge=True)

    def do_POST(self):
        if not self._trusted(True):
            return self._json(403, {"error": "forbidden"})
        action = self.path.split("?", 1)[0].removeprefix("/sms/")
        if action not in ("connect", "verify", "disconnect"):
            return self._json(404, {"error": "not found"})
        try:
            val = self._body()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        return self._json(*TEXTING.route(action, val))

    def log_message(self, *a):
        pass


def main():
    global TEXTING
    DATA.mkdir(parents=True, exist_ok=True)
    TEXTING = sms.Texting(read_doc, write_doc, list_docs, STATE / "sms-state.json")
    env_path = os.environ.get("DECISION_DESK_SPECTRUM_ENV") or STATE / "spectrum.env"
    TEXTING.reload(env_path)
    threading.Thread(target=TEXTING.run, args=(env_path,), daemon=True).start()
    print(f"decision desk on http://127.0.0.1:{PORT}  data={DATA}  texting={'on' if TEXTING.client else 'off'}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
