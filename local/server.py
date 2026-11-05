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
import json, os, re, sys, threading
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
  const j = (r) => { if (!r.ok) throw Object.assign(new Error(r.statusText), { code: r.status }); return r.json(); };
  const get = (p) => fetch("/db/" + p, { cache: "no-store" }).then(j);
  const send = (m, p, v) => fetch("/db/" + p, { method: m, headers: { "Content-Type": "application/json" }, body: JSON.stringify(v) }).then(j);
  const watch = (p, shape, cb, err) => {
    let last = null, stop = false;
    const tick = async () => {
      try { const v = await get(p), s = JSON.stringify(v); if (s !== last) { last = s; cb(shape(v)); } }
      catch (e) { if (err) err(e); }
      if (!stop) setTimeout(tick, 1500);
    };
    tick(); return () => { stop = true; };
  };
  const docs = (field) => (rows) => {
    if (field) rows = rows.slice().sort((a, b) => ((a.data[field] ?? 0) > (b.data[field] ?? 0)) - ((a.data[field] ?? 0) < (b.data[field] ?? 0)));
    return { metadata: { fromCache: false }, docs: rows.map((r) => ({ id: r.id, data: () => r.data })) };
  };
  const collection = (name, field) => ({ orderBy: (f) => collection(name, f), onSnapshot: (cb, err) => watch(name, docs(field), cb, err) });
  const doc = (path) => ({
    onSnapshot: (cb, err) => watch(path, (r) => ({ exists: r.exists, data: () => r.data }), cb, err),
    set: (v) => send("PUT", path, v),
    update: (v) => send("PATCH", path, v),
  });
  window.claude = { local: true, use: async (k) => { if (k !== "db") throw new Error("only db"); return { collection, doc }; } };
})();
</script>
"""


# The store. The page's set/update and the text-message line both write through here.
def read_doc(col, id):
    f = DATA / col / f"{id}.json"
    return json.loads(f.read_text()) if f.exists() else None


def list_docs(col):
    d = DATA / col
    return [(f.stem, json.loads(f.read_text())) for f in sorted(d.glob("*.json"))] if d.is_dir() else []


def write_doc(col, id, val, merge=False):
    """set (merge=False) or update (merge=True, doc must exist). Atomic: temp file, then rename."""
    f = DATA / col / f"{id}.json"
    if merge:
        if not f.exists():
            return False
        val = {**json.loads(f.read_text()), **val}
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
        if path == "/sms/status":
            return self._json(*TEXTING.route("status", {}))
        parts = self._parts()
        if parts is None:
            return self._json(404, {"error": "not found"})
        if len(parts) == 1:
            return self._json(200, [{"id": i, "data": d} for i, d in list_docs(parts[0])])
        d = read_doc(*parts)
        return self._json(200, {"exists": d is not None, "data": d})

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
    TEXTING.client = sms.bridge_from_env(os.environ.get("DECISION_DESK_SPECTRUM_ENV") or STATE / "spectrum.env", TEXTING.inbound)
    threading.Thread(target=TEXTING.run, daemon=True).start()
    print(f"decision desk on http://127.0.0.1:{PORT}  data={DATA}  texting={'on' if TEXTING.client else 'off'}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
