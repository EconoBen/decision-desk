"""Text-message line for the decision desk, over Photon Spectrum (Stable docs).

Texts each open decision to one verified phone, and turns replies into answers
written exactly the way the page writes them, so the hooks deliver them as usual.
Spectrum sends and receives only through its TypeScript SDK (gRPC), so a small
Node process, spectrum-bridge.mjs, does the talking over stdin/stdout. Everything
else is here, stdlib only. Without a configured line, texting stays off.
"""
import hmac, json, os, re, secrets, shutil, subprocess, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE_TTL, MAX_TRIES = 600, 5          # a code lives 10 minutes and allows 5 guesses
CODE_GAP, CODES_PER_HOUR = 30, 5      # rate limit on sending codes
TICK, BACKOFF = 5, 60                 # outbound poll, and the wait after a failed send
KEYS = ("SPECTRUM_PROJECT_ID", "SPECTRUM_PROJECT_SECRET", "SPECTRUM_LINE")


def now_iso():
    """Same shape as the page's new Date().toISOString(), so hooks compare it correctly."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def norm_phone(s):
    d = re.sub(r"[^\d+]", "", str(s or ""))
    if re.fullmatch(r"1?\d{10}", d):  # ponytail: bare 10 or 11 digits are read as US numbers
        d = "+1" + d[-10:]
    return d if re.fullmatch(r"\+[1-9]\d{6,14}", d) else None


class Texting:
    def __init__(self, read, write, docs, state_path, client=None):
        self.read, self.write, self.docs, self.state_path, self.client = read, write, docs, Path(state_path), client
        self.pending, self.codes, self.retry_at = None, [], 0  # code waiting for verify; code send times
        self.lock = threading.Lock()

    # --- state -----------------------------------------------------------
    def settings(self):
        return self.read("meta", "settings") or {}

    def phone(self):
        s = self.settings()
        return s.get("phone") if s.get("phoneVerified") else None

    def _state(self):
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def _save(self, state):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.state_path)

    def open_decisions(self):
        """What the page shows as open, in the page's order."""
        ds = [(i, d) for i, d in self.docs("decisions") if (d.get("status") or "open") == "open" and d.get("choices")]
        return sorted(ds, key=lambda x: (x[1].get("order") is None, x[1].get("order") or 0))

    # --- HTTP routes -----------------------------------------------------
    def route(self, action, body):
        with self.lock:
            p = self.phone()
            if action == "status":
                return 200, {"configured": bool(self.client), "verified": bool(p), "last4": p[-4:] if p else None,
                             "codeSent": bool(self.pending and time.time() < self.pending["exp"])}
            if action == "disconnect":
                s = {k: v for k, v in self.settings().items() if k not in ("phone", "phoneVerified")}
                self.write("meta", "settings", s)
                self.pending = None
                return 200, {"ok": True}
            if not self.client:
                return 503, {"error": "Texting isn't set up on this desk yet."}
            return self._connect(body) if action == "connect" else self._verify(body)

    def _connect(self, body):
        phone = norm_phone(body.get("phone"))
        if not phone:
            return 400, {"error": "Enter the full number, with its country code."}
        now = time.time()
        self.codes = [t for t in self.codes if now - t < 3600]
        if (self.codes and now - self.codes[-1] < CODE_GAP) or len(self.codes) >= CODES_PER_HOUR:
            return 429, {"error": "Too many codes for now. Wait a minute and try again."}
        self.codes.append(now)  # counts attempts, not successes
        code = f"{secrets.randbelow(10**6):06d}"
        try:
            self.client.send(phone, f"Your decision desk code is {code}. It expires in 10 minutes.")
        except Exception as e:
            print(f"sms: code text failed: {type(e).__name__}", file=sys.stderr)
            return 502, {"error": "The text didn't go out. Try again in a minute."}
        self.pending = {"phone": phone, "code": code, "exp": now + CODE_TTL, "tries": 0}
        return 200, {"ok": True}

    def _verify(self, body):
        p = self.pending
        if not p or time.time() > p["exp"]:
            self.pending = None
            return 400, {"error": "No code is waiting. Send a new one."}
        p["tries"] += 1
        if not hmac.compare_digest(str(body.get("code") or "").strip().encode(), p["code"].encode()):
            if p["tries"] >= MAX_TRIES:
                self.pending = None
                return 429, {"error": "Too many wrong codes. Send a new one."}
            return 400, {"error": "That code doesn't match."}
        self.pending = None
        self.write("meta", "settings", {**self.settings(), "phone": p["phone"], "phoneVerified": True})
        self._save({**self._state(), "sent": {}})  # a newly connected phone gets every open decision
        return 200, {"ok": True, "last4": p["phone"][-4:]}

    # --- outbound --------------------------------------------------------
    def compose(self, id, d, several):
        lines = [f"[{id}] {d.get('title') or id}"]
        for n, c in enumerate(d.get("choices") or [], 1):
            lines.append(f"{n}. {c.get('label') or c.get('id')}" + (" (recommended)" if c.get("id") == d.get("recommended") else ""))
        lines.append(f'Reply "{id} <n>", or "{id} <n> <note>". Text "list" for all open.' if several
                     else 'Reply "<n>", or "<n> <note>".')
        return "\n".join(lines)

    def tick(self):
        """Text the first open decision not yet texted in its current state (id + changedAt)."""
        with self.lock:
            phone = self.phone()
            if not phone or not self.client or time.time() < self.retry_at:
                return None
            sent, pend = self._state().get("sent", {}), self.open_decisions()
            nxt = next(((i, d) for i, d in pend if not (i in sent and sent[i] == (d.get("changedAt") or ""))), None)
            text = nxt and self.compose(*nxt, len(pend) > 1)
        self.client.start()  # keeps the inbound stream up while a phone is connected
        if not nxt:
            return None
        try:
            self.client.send(phone, text)
        except Exception as e:
            print(f"sms: send failed: {type(e).__name__}", file=sys.stderr)
            self.retry_at = time.time() + BACKOFF
            return None
        with self.lock:
            state = self._state()
            state.setdefault("sent", {})[nxt[0]] = nxt[1].get("changedAt") or ""
            self._save(state)
        return nxt[0]  # ponytail: one text per tick paces bursts, which Photon says get lines flagged

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"sms: tick failed: {type(e).__name__}", file=sys.stderr)
            time.sleep(TICK)

    # --- inbound ---------------------------------------------------------
    def inbound(self, sender, text):
        """A text arrived. Only the verified phone is heard; anyone else is dropped without a reply."""
        with self.lock:
            phone = self.phone()
            if not phone or norm_phone(sender) != phone:
                return None
            reply = self._answer(text)
        if reply:
            try:
                self.client.send(phone, reply)
            except Exception as e:
                print(f"sms: reply failed: {type(e).__name__}", file=sys.stderr)
        return reply

    def _answer(self, text):
        t = str(text or "").strip()
        pend = self.open_decisions()
        if not pend:
            return "Nothing needs you right now."
        if t.lower() == "list":
            state = self._state()
            for id, _ in pend:
                state.get("sent", {}).pop(id, None)  # the outbound loop re-sends them, paced
            self._save(state)
            return None
        m = re.fullmatch(r"(\d+)(?:\s+(.*))?", t, re.S)
        if m and len(pend) == 1:
            id, d = pend[0]
            n, note = m.group(1), m.group(2)
        else:
            m = re.fullmatch(r"\[?([A-Za-z0-9_-]+)\]?\s+(\d+)(?:\s+(.*))?", t, re.S)
            hit = []
            if m:
                tok = m.group(1).lower()
                hit = [x for x in pend if x[0].lower() == tok] or [x for x in pend if x[0].lower().startswith(tok)]
            if len(hit) != 1:
                return self._help(pend)
            (id, d), n, note = hit[0], m.group(2), m.group(3)
        choices = d.get("choices") or []
        if not 1 <= int(n) <= len(choices):
            return self._help(pend)
        # Exactly what the page's writeAnswer() does: the response doc, then status "answered".
        self.write("responses", id, {"choice": choices[int(n) - 1].get("id"), "note": (note or "").strip(), "submittedAt": now_iso()})
        if d.get("status") != "answered":
            self.write("decisions", id, {"status": "answered"}, True)
        return f"Sent: [{id}] {choices[int(n) - 1].get('label') or choices[int(n) - 1].get('id')}"

    def _help(self, pend):
        if len(pend) == 1:
            id, d = pend[0]
            return f'Reply a number from 1 to {len(d.get("choices") or [])} for [{id}], optionally with a note: "1 sounds good".'
        return ('Several are open, so start with the tag: "<tag> <n>" or "<tag> <n> <note>". Open: '
                + ", ".join(f"{id} (1-{len(d.get('choices') or [])})" for id, d in pend) + '. Text "list" to see them again.')


class Bridge:
    """Runs spectrum-bridge.mjs and talks JSON lines over its stdin and stdout. Opens no ports."""

    def __init__(self, env, on_inbound, node="node"):
        self.env, self.on_inbound, self.cmd = env, on_inbound, [node, str(HERE / "spectrum-bridge.mjs")]
        self.proc, self.waits, self.n, self.lock = None, {}, 0, threading.Lock()

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self.proc = subprocess.Popen(self.cmd, cwd=HERE, text=True,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={**os.environ, **self.env})
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def send(self, to, text):
        self.start()
        with self.lock:
            self.n += 1
            n, done = self.n, threading.Event()
            self.waits[n] = [done, {}]
            self.proc.stdin.write(json.dumps({"id": n, "to": to, "text": text}) + "\n")
            self.proc.stdin.flush()
        if not done.wait(60):
            self.waits.pop(n, None)
            raise TimeoutError("bridge did not answer")
        res = self.waits.pop(n)[1]
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "send failed")

    def _read(self, proc):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # not ours (SDK log noise)
            if "inbound" in msg:
                threading.Thread(target=self.on_inbound, args=(msg["inbound"].get("from"), msg["inbound"].get("text")), daemon=True).start()
            elif msg.get("id") in self.waits:
                w = self.waits[msg["id"]]
                w[1] = msg
                w[0].set()
        for w in list(self.waits.values()):  # the bridge exited: fail whoever is waiting
            w[1] = {"ok": False, "error": "bridge exited"}
            w[0].set()


def load_env(path):
    out = {}
    try:
        p = Path(path)
        if p.stat().st_mode & 0o077:
            print(f"sms: {p.name} is readable by others; chmod 600 it", file=sys.stderr)
        for line in p.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def bridge_from_env(env_path, on_inbound):
    """A Bridge when credentials, a line, Node and the SDK are all present; otherwise None (texting off)."""
    f = load_env(env_path)
    env = {k: f.get(k) or os.environ.get(k, "") for k in KEYS}
    node = os.environ.get("DECISION_DESK_NODE") or "node"
    if not all(env.values()) or not shutil.which(node) or not (HERE / "node_modules" / "spectrum-ts").exists():
        return None
    return Bridge(env, on_inbound, node)
