"""Text-message line for the decision desk, over Photon Spectrum (Stable docs).

Texts each decision that needs you to one verified phone, and turns replies into
answers written exactly the way the page writes them, so the hooks deliver them as
usual. Spectrum sends and receives only through its TypeScript SDK (gRPC), so a small
Node process, spectrum-bridge.mjs, does the talking over stdin/stdout. Everything else
is here, stdlib only. Without credentials, texting stays off.
"""
import base64, hmac, json, os, re, secrets, shutil, subprocess, sys, threading, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE_TTL, MAX_TRIES = 600, 5          # a code lives 10 minutes and allows 5 guesses
CODE_GAP, CODES_PER_HOUR = 30, 5      # rate limit on sending codes
HELP_MAX, HELP_WINDOW = 3, 600        # unasked help texts: 3 per 10 minutes, so a phone's auto-reply can't start a loop
TICK, BACKOFF = 5, 60                 # outbound pace (one text per tick), and the wait after a failed send
KEYS = ("SPECTRUM_PROJECT_ID", "SPECTRUM_PROJECT_SECRET", "SPECTRUM_LINE", "SPECTRUM_ALLOW_WEBHOOKS")
API = "https://spectrum.photon.codes/projects/"
WELCOME = ('Your decision desk is connected. Decisions that need you will come here. Reply with a number to answer, '
           'and add a note after it if you like. Text "help" any time.')
SHARED_PROJECT = ("This Photon project already sends its texts to another app (it has a webhook), so your replies would reach "
                  "that app too. Run local/setup_texting.py with a project just for the desk.")
HELP = ('Reply with a number to answer, and add a note after it if you like: "2 go ahead". When several are open, '
        'start with the tag: "<tag> 2". Text "list" to get the open ones again, or "stop" to turn these texts off.')


def now_iso():
    """Same shape as the page's new Date().toISOString(), so hooks compare it correctly."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def norm_phone(s):
    d = re.sub(r"[^\d+]", "", str(s or ""))
    if re.fullmatch(r"1?\d{10}", d):  # ponytail: bare 10 or 11 digits are read as US numbers
        d = "+1" + d[-10:]
    return d if re.fullmatch(r"\+[1-9]\d{6,14}", d) else None


class ApiError(Exception):
    pass


def api(creds, method, path, body=None):
    """One Spectrum API call (Stable docs: HTTP Basic with the project id and secret). Returns the `data` field."""
    pid = creds.get("SPECTRUM_PROJECT_ID") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", pid):
        raise ApiError("That project id doesn't look right.")
    auth = base64.b64encode(f"{pid}:{creds.get('SPECTRUM_PROJECT_SECRET') or ''}".encode()).decode()
    req = urllib.request.Request(API + pid + path, method=method, data=None if body is None else json.dumps(body).encode(), headers={
        "Authorization": "Basic " + auth, "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "decision-desk-local"})  # Cloudflare in front of the API turns away Python's default agent
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("data")
    except urllib.error.HTTPError as e:
        e.close()
        raise ApiError({401: "Photon didn't accept that project id and secret.", 404: "Photon has no project with that id.",
                        429: "Photon is rate limiting. Wait a few seconds."}.get(e.code, f"Photon answered {e.code}.")) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise ApiError("Couldn't reach Photon.") from None


def explain(e):
    """One sentence for the page about a send that failed."""
    m = str(e)
    if isinstance(e, ApiError):
        return m
    if "not allowed" in m.lower():
        return "Photon won't text this number yet. Add it under Users in the Photon project, then try again."
    if re.search(r"unauth|\b401\b|credential", m, re.I):
        return "Photon didn't accept the project id and secret. Run setup_texting.py again."
    if isinstance(e, TimeoutError):
        return "Photon didn't answer in time. Try again in a minute."
    return "The text didn't go out. Try again in a minute."


class Texting:
    def __init__(self, read, write, docs, state_path, client=None):
        self.read, self.write, self.docs, self.state_path, self.client = read, write, docs, Path(state_path), client
        self.pending, self.codes, self.helps, self.retry_at, self.env_mtime = None, [], [], 0, None
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

    def waiting_on_you(self):
        """What the page asks you to answer: open decisions, and "Your move" followups with choices. In page order."""
        ds = [(i, d) for i, d in self.docs("decisions")
              if (d.get("status") or "open") in ("open", "followup") and any(isinstance(c, dict) and c.get("id") for c in d.get("choices") or [])]
        return sorted(ds, key=lambda x: (x[1].get("order") is None, x[1].get("order") or 0))

    # --- HTTP routes -----------------------------------------------------
    def route(self, action, body):
        client = self.client
        problem = client.problem() if client and action in ("status", "connect") else None  # network, so outside the lock
        with self.lock:
            p, code = self.phone(), self.pending if self.pending and time.time() < self.pending["exp"] else None
            if action == "status":
                return 200, {"configured": bool(client) and not problem, "problem": problem, "verified": bool(p),
                             "last4": p[-4:] if p else None, "codeSent": bool(code), "codeTo": code["phone"][-4:] if code else None}
            if action == "disconnect":
                self._forget()
                return 200, {"ok": True}
            if not self.client:
                return 503, {"error": "Texting isn't set up on this desk yet. Run local/setup_texting.py."}
            if problem and action == "connect":
                return 409, {"error": problem}
            return self._connect(body) if action == "connect" else self._verify(body)

    def _forget(self):
        self.write("meta", "settings", {k: v for k, v in self.settings().items() if k not in ("phone", "phoneVerified")})
        self.pending = None
        if self.client:
            self.client.stop()  # no phone, no stream

    def _connect(self, body):
        phone = norm_phone(body.get("phone"))
        if not phone:
            return 400, {"error": "Enter the full number, with its country code."}
        now = time.time()
        self.codes = [t for t in self.codes if now - t < 3600]
        if self.codes and now - self.codes[-1] < CODE_GAP:
            return 429, {"error": f"A code just went out. You can ask for another in {int(CODE_GAP - (now - self.codes[-1])) + 1} seconds."}
        if len(self.codes) >= CODES_PER_HOUR:
            return 429, {"error": "That's 5 codes this hour. Try again later."}
        self.codes.append(now)  # counts attempts, not successes
        code = f"{secrets.randbelow(10**6):06d}"
        try:
            self.client.register(phone)  # shared-pool projects only text registered users
            self.client.send(phone, f"Your decision desk code is {code}. It expires in 10 minutes.")
        except Exception as e:
            print(f"sms: code text failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            return 502, {"error": explain(e)}
        self.pending = {"phone": phone, "code": code, "exp": now + CODE_TTL, "tries": 0}
        return 200, {"ok": True}

    def _verify(self, body):
        p = self.pending
        if not p or time.time() > p["exp"]:
            self.pending = None
            return 400, {"error": "That code expired. Send a new one."}
        typed = re.sub(r"\D", "", str(body.get("code") or ""))
        if len(typed) != 6:
            return 400, {"error": "The code has 6 digits."}
        p["tries"] += 1
        if not hmac.compare_digest(typed.encode(), p["code"].encode()):
            if p["tries"] >= MAX_TRIES:
                self.pending = None
                return 429, {"error": "Too many wrong codes. Send a new one."}
            return 400, {"error": f"That code doesn't match. {MAX_TRIES - p['tries']} tries left."}
        self.pending = None
        self.write("meta", "settings", {**self.settings(), "phone": p["phone"], "phoneVerified": True})
        self._save({"sent": {}, "say": [WELCOME]})  # a newly connected phone gets a hello, then every open decision
        return 200, {"ok": True, "last4": p["phone"][-4:]}

    # --- outbound --------------------------------------------------------
    def compose(self, id, d, several):
        lines = [f"[{id}] {'Your move: ' if d.get('status') == 'followup' else ''}{d.get('title') or id}"]
        if d.get("status") == "followup" and d.get("agentNote"):
            note = " ".join(str(d["agentNote"]).split())
            lines.append(note if len(note) <= 200 else note[:199] + "…")
        for n, c in enumerate(d.get("choices") or [], 1):
            lines.append(f"{n}. {c.get('label') or c.get('id')}" + (" (recommended)" if c.get("id") == d.get("recommended") else ""))
        lines.append(f'Reply "{id} <n>", or add a note after the number.' if several else 'Reply with a number, or a number and a note.')
        return "\n".join(lines)

    def tick(self):
        """Send the next queued text, or the first waiting decision not yet texted in its current state."""
        with self.lock:
            phone = self.phone()
            if not phone or not self.client or time.time() < self.retry_at:
                return None
            state, pend = self._state(), self.waiting_on_you()
            sent, say = state.get("sent", {}), state.get("say", [])
            nxt = next(((i, d) for i, d in pend if sent.get(i) != f"{d.get('status') or 'open'}|{d.get('changedAt') or ''}"), None)
            text = say[0] if say else nxt and self.compose(*nxt, len(pend) > 1)
        self.client.start()  # keeps the inbound stream up while a phone is connected
        if not text:
            return None
        try:
            self.client.send(phone, text)
        except Exception as e:
            print(f"sms: send failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            self.retry_at = time.time() + BACKOFF
            return None
        with self.lock:
            state = self._state()
            if say:
                state["say"] = state.get("say", [])[1:]
            else:
                state.setdefault("sent", {})[nxt[0]] = f"{nxt[1].get('status') or 'open'}|{nxt[1].get('changedAt') or ''}"
            self._save(state)
        return "said" if say else nxt[0]  # ponytail: one text per tick paces bursts, which Photon says get lines flagged

    def reload(self, env_path):
        """Pick up a new or changed spectrum.env without a restart; setup_texting.py relies on this."""
        try:
            m = Path(env_path).stat().st_mtime_ns
        except OSError:
            m = None
        if m == self.env_mtime:
            return
        self.env_mtime = m
        old, self.client = self.client, bridge_from_env(env_path, self.inbound)
        if old:
            old.stop()
        print(f"sms: texting {'on' if self.client else 'off'}", file=sys.stderr)

    def run(self, env_path):
        while True:
            try:
                self.reload(env_path)
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
            t = str(text or "").strip()
            stop = t.lower().strip(" .!") == "stop"
            reply = "Texts are off. Turn them back on from Text me on your desk." if stop else self._answer(t)
        if reply:
            try:
                self.client.send(phone, reply)
            except Exception as e:
                print(f"sms: reply failed: {type(e).__name__}", file=sys.stderr)
        if stop:
            with self.lock:
                self._forget()  # after the reply, so the reply still goes out
        return reply

    def _answer(self, t):
        cmd, pend = t.lower().strip(" .!"), self.waiting_on_you()
        if cmd in ("help", "?", "commands"):
            return HELP
        if not pend:
            return "Nothing needs you right now." if cmd == "list" or self._may_help() else None
        if cmd == "list":
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
                return self._unmatched(pend)
            (id, d), n, note = hit[0], m.group(2), m.group(3)
        choices = d.get("choices") or []
        if not 1 <= int(n) <= len(choices):
            return self._unmatched(pend)
        # Exactly what the page's writeAnswer() does: the response doc, then status "answered".
        self.write("responses", id, {"choice": choices[int(n) - 1].get("id"), "note": (note or "").strip(), "submittedAt": now_iso()})
        if d.get("status") != "answered":
            self.write("decisions", id, {"status": "answered"}, True)
        return f"Sent: [{id}] {choices[int(n) - 1].get('label') or choices[int(n) - 1].get('id')}"

    def _may_help(self):
        now = time.time()
        self.helps = [t for t in self.helps if now - t < HELP_WINDOW]
        if len(self.helps) >= HELP_MAX:
            return False
        self.helps.append(now)
        return True

    def _unmatched(self, pend):
        if not self._may_help():
            return None
        if len(pend) == 1:
            id, d = pend[0]
            return f'Reply with a number from 1 to {len(d.get("choices") or [])} for [{id}], and a note after it if you like. Text "help" for more.'
        return ('Several are open, so start with the tag: "<tag> 2". Open now: '
                + ", ".join(f"{id} (1-{len(d.get('choices') or [])})" for id, d in pend) + '. Text "help" for more.')


class Bridge:
    """The Spectrum client: sends and receives through spectrum-bridge.mjs over its stdin and stdout (no ports),
    and registers numbers through the Spectrum API."""

    def __init__(self, env, on_inbound, node="node"):
        self.env, self.on_inbound, self.cmd = env, on_inbound, [node, str(HERE / "spectrum-bridge.mjs")]
        self.proc, self.waits, self.n, self.lock, self.kind, self.checked = None, {}, 0, threading.Lock(), None, None

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self.proc = subprocess.Popen(self.cmd, cwd=HERE, text=True,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={**os.environ, **self.env})
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def stop(self):
        with self.lock:
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()  # the bridge stops its SDK client and exits
                proc.wait(5)
            except Exception:
                proc.kill()

    def problem(self):
        """Why this project can't be the desk's, or None. One read-only check per configuration; network errors aren't kept."""
        if self.checked is None:
            try:
                hooks = api(self.env, "GET", "/webhooks/") or []
            except ApiError as e:
                return str(e)
            self.checked = (SHARED_PROJECT if hooks and self.env.get("SPECTRUM_ALLOW_WEBHOOKS") != "1" else "",)
        return self.checked[0] or None

    def register(self, phone):
        """Free and Pro projects text from Photon's shared pool, and only to numbers registered as users. Idempotent."""
        if self.kind is None:
            self.kind = (api(self.env, "GET", "/imessage/") or {}).get("type")
        if self.kind == "shared":
            api(self.env, "POST", "/users/", {"type": "shared", "phoneNumber": phone})

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
        why = "bridge exited"
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # not ours (SDK log noise)
            if "inbound" in msg:
                threading.Thread(target=self.on_inbound, args=(msg["inbound"].get("from"), msg["inbound"].get("text")), daemon=True).start()
            elif "fatal" in msg:
                why = str(msg["fatal"])[:200]
                print(f"sms: bridge could not start: {why}", file=sys.stderr)
            elif msg.get("id") in self.waits:
                w = self.waits[msg["id"]]
                w[1] = msg
                w[0].set()
        for w in list(self.waits.values()):  # the bridge exited: fail whoever is waiting
            w[1] = {"ok": False, "error": why}
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
    """A Bridge when the credentials, Node and the SDK are all present; otherwise None (texting off).
    SPECTRUM_LINE is optional: it picks one dedicated line; shared-pool projects don't have one."""
    f = load_env(env_path)
    env = {k: f.get(k) or os.environ.get(k, "") for k in KEYS}
    node = os.environ.get("DECISION_DESK_NODE") or "node"
    if not (env["SPECTRUM_PROJECT_ID"] and env["SPECTRUM_PROJECT_SECRET"]) or not shutil.which(node) or not (HERE / "node_modules" / "spectrum-ts").exists():
        return None
    return Bridge(env, on_inbound, node)
