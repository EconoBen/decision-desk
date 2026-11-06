"""Self-check for local mode: python3 -m unittest local/test_local.py (stdlib only, no network, no texts)."""
import http.client, io, json, os, re, stat, subprocess, sys, tempfile, threading, time, unittest, urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import inject, server, setup_texting, sms  # noqa: E402


class FakeClient:
    """Stands in for the Spectrum client. Records texts and registrations instead of making them."""
    def __init__(self):
        self.sent, self.registered, self.fail, self.stopped, self.why = [], [], None, 0, None

    def problem(self):
        return self.why

    def start(self):
        pass

    def stop(self):
        self.stopped += 1

    def register(self, phone):
        self.registered.append(phone)

    def send(self, to, text):
        if self.fail:
            raise self.fail
        self.sent.append((to, text))


def decision(title, choices, **kw):
    return {"title": title, "choices": [{"id": c, "label": c.title()} for c in choices], "status": "open", "order": 1, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "db"
        for mod, attrs in ((server, {"DATA": self.db}), (inject, {"ROOT": self.tmp, "DB": self.db, "STATE": self.tmp / "hook-state.json"})):
            for k, v in attrs.items():
                p = mock.patch.object(mod, k, v)
                p.start()
                self.addCleanup(p.stop)
        self.client = FakeClient()
        self.t = sms.Texting(server.read_doc, server.write_doc, server.list_docs, self.tmp / "sms-state.json", self.client)

    def put(self, col, id, val):
        server.write_doc(col, id, val)

    def doc(self, col, id):
        return server.read_doc(col, id)


class ServerTest(Base):
    def setUp(self):
        super().setUp()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        p = mock.patch.object(server, "TEXTING", self.t)
        p.start()
        self.addCleanup(p.stop)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, method, path, body=None, **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", **headers}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if r.headers.get("Content-Type") == "application/json" else data.decode())

    def test_path_traversal_refused(self):
        outside = self.tmp / "evil.json"
        for path in ("/db/..%2f..%2fetc", "/db/decisions/..%2fevil", "/db/../evil", "/db/decisions/../../evil", "/db/decisions/a.b"):
            self.assertEqual(self.req("GET", path)[0], 404, path)
            self.assertEqual(self.req("PUT", path, {"x": 1})[0], 404, path)
        self.assertFalse(outside.exists())
        self.assertEqual(list(self.tmp.rglob("*.json")), [])

    def test_atomic_write(self):
        self.assertEqual(self.req("PUT", "/db/decisions/a", {"v": 1})[0], 200)
        seen, orig = [], Path.replace

        def spy(src, dst):
            seen.append((src.name, json.loads(Path(dst).read_text())))
            return orig(src, dst)
        with mock.patch.object(Path, "replace", spy):
            self.assertEqual(self.req("PATCH", "/db/decisions/a", {"w": 2})[0], 200)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].endswith(".tmp"))
        self.assertEqual(seen[0][1], {"v": 1})  # the real file was untouched until the rename
        self.assertEqual(self.doc("decisions", "a"), {"v": 1, "w": 2})
        self.assertEqual(list(self.db.rglob("*.tmp")), [])
        self.assertEqual(self.req("PATCH", "/db/decisions/missing", {"w": 2})[0], 404)

    def test_only_this_page_may_call(self):
        self.assertEqual(self.req("PUT", "/db/decisions/a", {"v": 1}, **{"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.req("PUT", "/db/decisions/a", {"v": 1}, Origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/db/decisions", Host="evil.example:80")[0], 403)
        self.assertEqual(self.req("POST", "/sms/connect", {"phone": "+14155550100"}, Origin="http://evil.example")[0], 403)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.req("PUT", "/db/decisions/a", {"v": 1}, Origin=f"http://localhost:{self.port}", Host=f"localhost:{self.port}")[0], 200)

    def test_page_gets_local_flag_and_sms_routes(self):
        status, html = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("window.claude = { local: true", html)
        self.assertEqual(self.req("GET", "/sms/status")[1]["configured"], True)
        self.assertEqual(self.req("POST", "/sms/nope", {})[0], 404)


    def test_events_stream_changes(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/events", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        self.assertEqual(r.headers["Content-Type"], "text/event-stream")
        first = json.loads(r.fp.readline().decode().removeprefix("data: "))
        r.fp.readline()
        t0 = time.time()
        server.write_doc("decisions", "new", {"title": "x"})  # an in-process write, like a text reply
        nxt = json.loads(r.fp.readline().decode().removeprefix("data: "))
        self.assertLess(time.time() - t0, 1)
        self.assertNotEqual(first.get("decisions"), nxt["decisions"])
        c.close()

    def test_cache_follows_mtime_and_survives_half_writes(self):
        f = self.db / "decisions" / "a.json"
        self.put("decisions", "a", {"v": 1})
        self.assertEqual(self.doc("decisions", "a"), {"v": 1})
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("re-read")):
            self.assertEqual(self.doc("decisions", "a"), {"v": 1})  # unchanged file: served from the cache
        f.write_text('{"v": 22}')
        self.assertEqual(self.doc("decisions", "a"), {"v": 22})
        f.write_text('{"v": 3')  # an agent caught mid-write
        self.assertEqual(self.doc("decisions", "a"), {"v": 22})
        self.assertEqual(self.req("GET", "/db/decisions")[1], [{"id": "a", "data": {"v": 22}}])


class HookTest(Base):
    def setUp(self):
        super().setUp()
        self.put("decisions", "mine", decision("Mine?", ["a", "b"], agent={"kind": "session", "id": "s1"}, agentAt="2026-01-01T00:00:00Z"))
        self.put("decisions", "anyone", decision("Anyone?", ["a"]))
        self.put("responses", "mine", {"choice": "a", "note": "", "submittedAt": "2026-01-02T00:00:00.000Z"})
        self.put("responses", "anyone", {"choice": "a", "note": "", "submittedAt": "2026-01-02T00:00:00.000Z"})

    def test_pending_and_route(self):
        self.assertEqual({i[0] for i in inject.pending("s1")}, {"mine", "anyone"})
        self.assertEqual({i[0] for i in inject.pending("s2")}, {"anyone"})  # owned by s1, never shown to s2
        server.write_doc("decisions", "mine", {"agentAt": "2026-01-03T00:00:00Z"}, True)  # handled
        self.assertEqual({i[0] for i in inject.pending("s1")}, {"anyone"})

    def test_numeric_timestamps(self):
        # Some agents write epoch milliseconds. That must neither crash the hook nor hide other answers.
        self.put("decisions", "ms", decision("Ms?", ["a"], agent={"kind": "session", "id": "s1"}, agentAt=1767225600000))  # 2026-01-01
        self.put("responses", "ms", {"choice": "a", "note": "", "submittedAt": "2026-01-02T00:00:00.000Z"})
        self.put("decisions", "bad", "not an object")
        self.assertEqual({i[0] for i in inject.pending("s1")}, {"mine", "anyone", "ms"})
        server.write_doc("decisions", "ms", {"agentAt": 1767398400000}, True)  # 2026-01-03, after the answer
        self.assertEqual({i[0] for i in inject.pending("s1")}, {"mine", "anyone"})
        self.assertEqual(inject.when(1767225600), inject.when("2026-01-01T00:00:00Z"))

    def test_remind(self):
        state = {}
        self.assertEqual(len(inject.collect("s1", state, 120)[0]), 2)
        self.assertEqual(inject.collect("s1", state, 120)[0], [])  # just reminded
        state["seen"] = {k: time.time() - 121 for k in state["seen"]}
        self.assertEqual(len(inject.collect("s1", state, 120)[0]), 2)  # 120 s later, again

    def run_hook(self, payload):
        env = {**os.environ, "DECISION_DESK_STATE": str(self.tmp)}
        return subprocess.run([sys.executable, str(HERE / "inject.py"), "hook"], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=10).stdout

    def test_session_filtering(self):
        self.assertEqual(self.run_hook({"session_id": "s1", "agent_id": "sub", "hook_event_name": "PostToolUse"}), "")
        self.assertEqual(self.run_hook({"hook_event_name": "PostToolUse"}), "")
        ctx = json.loads(self.run_hook({"session_id": "s2", "hook_event_name": "PostToolUse"}))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[anyone]", ctx)
        self.assertNotIn("[mine]", ctx)  # owned by s1
        out = json.loads(self.run_hook({"session_id": "s1", "hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("[mine]", out["hookSpecificOutput"]["additionalContext"])

    def test_phone_never_reported(self):
        self.put("meta", "settings", {"name": "Ben"})
        state = {}
        inject.collect("s1", state, 120)
        self.put("meta", "settings", {"name": "Ben", "phone": "+14155550100", "phoneVerified": True})
        self.assertIsNone(inject.collect("s1", state, 120)[1])
        self.put("meta", "settings", {"name": "Benjamin", "phone": "+14155550100", "phoneVerified": True})
        self.assertEqual(inject.collect("s1", state, 120)[1], {"name": "Benjamin"})


class TextingTest(Base):
    PHONE = "+14155550100"

    def connect(self):
        self.assertEqual(self.t.route("connect", {"phone": "(415) 555-0100"})[0], 200)
        to, text = self.client.sent[-1]
        self.assertEqual(to, self.PHONE)
        return re.search(r"\b(\d{6})\b", text).group(1)

    def verified(self):
        code = self.connect()
        self.assertEqual(self.t.route("verify", {"code": code}), (200, {"ok": True, "last4": "0100"}))
        self.assertEqual(self.t.tick(), "said")  # the hello goes first
        self.client.sent.clear()

    def test_connect_verify_disconnect(self):
        self.assertEqual(self.t.route("status", {})[1], {"configured": True, "problem": None, "verified": False, "last4": None, "codeSent": False, "codeTo": None})
        code = self.connect()
        self.assertEqual(self.client.registered, [self.PHONE])  # shared-pool projects need the number registered first
        self.assertEqual(self.t.route("status", {})[1]["codeTo"], "0100")
        self.assertEqual(self.t.route("verify", {"code": f" {code[:3]} {code[3:]} "})[0], 200)  # spaces are fine
        self.assertEqual(self.doc("meta", "settings"), {"phone": self.PHONE, "phoneVerified": True})
        self.assertEqual(self.t.route("status", {})[1]["last4"], "0100")
        self.assertEqual(self.t.tick(), "said")
        self.assertIn("connected", self.client.sent[-1][1])
        self.assertEqual(self.t.route("disconnect", {})[0], 200)
        self.assertEqual(self.doc("meta", "settings"), {})
        self.assertEqual(self.client.stopped, 1)
        self.assertIsNone(self.t.tick())

    def test_wrong_code_and_limits(self):
        self.assertEqual(self.t.route("connect", {"phone": "12"})[0], 400)
        code = self.connect()
        st, body = self.t.route("connect", {"phone": self.PHONE})
        self.assertEqual(st, 429)
        self.assertIn("seconds", body["error"])
        self.assertEqual(self.t.route("verify", {"code": "12"}), (400, {"error": "The code has 6 digits."}))  # doesn't use a try
        wrong = "000000" if code != "000000" else "111111"
        for left in range(sms.MAX_TRIES - 1, 0, -1):
            self.assertEqual(self.t.route("verify", {"code": wrong}), (400, {"error": f"That code doesn't match. {left} tries left."}))
        self.assertEqual(self.t.route("verify", {"code": wrong})[0], 429)
        self.assertEqual(self.t.route("verify", {"code": code})[0], 400)  # locked out: the right code is gone too
        self.assertIsNone(self.doc("meta", "settings"))
        self.t.codes = [time.time() - 3500] * 5
        self.assertIn("5 codes this hour", self.t.route("connect", {"phone": self.PHONE})[1]["error"])
        self.t.client = None
        self.assertEqual(self.t.route("connect", {"phone": self.PHONE})[0], 503)

    def test_project_with_a_webhook_is_refused(self):
        self.client.why = sms.SHARED_PROJECT
        st = self.t.route("status", {})[1]
        self.assertEqual((st["configured"], st["problem"]), (False, sms.SHARED_PROJECT))
        self.assertEqual(self.t.route("connect", {"phone": self.PHONE}), (409, {"error": sms.SHARED_PROJECT}))
        self.assertEqual((self.client.registered, self.client.sent), ([], []))  # nothing touched the project

    def test_clear_errors(self):
        for err, says in ((RuntimeError("Target not allowed for this project"), "Add it under Users"),
                          (RuntimeError("401 Unauthorized"), "project id and secret"),
                          (TimeoutError("bridge did not answer"), "in time"),
                          (sms.ApiError("Couldn't reach Photon."), "Couldn't reach Photon."),
                          (RuntimeError("something else"), "didn't go out")):
            self.t.codes, self.client.fail = [], err
            st, body = self.t.route("connect", {"phone": self.PHONE})
            self.assertEqual(st, 502)
            self.assertIn(says, body["error"])

    def test_outbound_dedupe(self):
        self.put("decisions", "one", decision("First?", ["a", "b"], recommended="b", order=1))
        self.put("decisions", "two", decision("Second?", ["a"], order=2))
        self.put("decisions", "gone", decision("Answered", ["a"], status="answered"))
        self.put("decisions", "nochoice", {"title": "Nothing to pick", "status": "open", "order": 3})
        self.assertIsNone(self.t.tick())  # no verified phone, nothing goes out
        self.verified()
        self.assertEqual([self.t.tick(), self.t.tick(), self.t.tick()], ["one", "two", None])
        first = self.client.sent[0][1]
        self.assertEqual(first.splitlines()[:3], ["[one] First?", "1. A", "2. B (recommended)"])
        self.assertIn('"one <n>"', first)  # several open, so the tag form
        server.write_doc("decisions", "one", {"changedAt": "2026-01-05T00:00:00Z"}, True)
        self.assertEqual([self.t.tick(), self.t.tick()], ["one", None])  # new state, texted once more
        self.client.fail = RuntimeError("down")
        server.write_doc("decisions", "two", {"changedAt": "2026-01-06T00:00:00Z"}, True)
        self.assertIsNone(self.t.tick())
        self.client.fail = None
        self.assertIsNone(self.t.tick())  # backing off after a failure
        self.t.retry_at = 0
        self.assertEqual(self.t.tick(), "two")

    def test_followup_is_texted_and_answered(self):
        self.put("decisions", "move", decision("Pick again?", ["x", "y"], status="followup", agentNote="The first plan fell through. Two new options."))
        self.verified()
        self.assertEqual(self.t.tick(), "move")
        self.assertEqual(self.client.sent[0][1].splitlines()[:2], ["[move] Your move: Pick again?", "The first plan fell through. Two new options."])
        self.assertEqual(self.t.inbound(self.PHONE, "2"), "Sent: [move] Y")
        self.assertEqual(self.doc("decisions", "move")["status"], "answered")

    def test_reply_one_pending(self):
        self.put("decisions", "solo", decision("Ship?", ["yes", "no"]))
        self.verified()
        self.t.tick()
        self.assertIn("Reply with a number", self.client.sent[0][1])
        self.assertEqual(self.t.inbound("+1 415 555 0100", "2 not\nyet"), "Sent: [solo] No")
        r = self.doc("responses", "solo")
        self.assertEqual((r["choice"], r["note"]), ("no", "not\nyet"))
        self.assertEqual(self.doc("decisions", "solo")["status"], "answered")
        self.assertEqual(self.t.inbound(self.PHONE, "1"), "Nothing needs you right now.")

    def test_reply_several_pending(self):
        self.put("decisions", "rc7-key", decision("Key?", ["me", "ben"]))
        self.put("decisions", "rc7-cve", decision("CVE?", ["fix", "allow", "wait"]))
        self.verified()
        for text in ("1", "rc7 1", "rc7-key 3", "hello"):  # ambiguous, ambiguous prefix, out of range, junk
            self.t.helps = []
            self.assertIn("Several are open", self.t.inbound(self.PHONE, text))
        self.assertEqual(list(self.db.glob("responses/*.json")), [])
        self.assertEqual(self.t.inbound(self.PHONE, "RC7-CVE 2 with an expiry"), "Sent: [rc7-cve] Allow")
        self.assertEqual(self.doc("responses", "rc7-cve")["note"], "with an expiry")
        self.assertEqual(self.t.inbound(self.PHONE, "[rc7-key] 1"), "Sent: [rc7-key] Me")
        self.assertEqual(self.doc("responses", "rc7-key")["note"], "")

    def test_help_list_stop(self):
        self.put("decisions", "a", decision("A?", ["x"], order=1))
        self.put("decisions", "b", decision("B?", ["x"], order=2))
        self.verified()
        self.assertEqual(self.t.inbound(self.PHONE, "Help"), sms.HELP)
        self.assertEqual(self.t.inbound(self.PHONE, "?"), sms.HELP)
        self.t.tick(), self.t.tick()
        self.assertIsNone(self.t.tick())
        self.assertIsNone(self.t.inbound(self.PHONE, "List"))
        self.assertEqual([self.t.tick(), self.t.tick(), self.t.tick()], ["a", "b", None])
        self.client.sent.clear()
        self.assertIn("Texts are off", self.t.inbound(self.PHONE, "STOP"))
        self.assertEqual(self.client.sent[-1][0], self.PHONE)  # the reply went out before the phone was forgotten
        self.assertEqual(self.doc("meta", "settings"), {})
        self.assertEqual(self.client.stopped, 1)
        self.assertIsNone(self.t.inbound(self.PHONE, "1"))  # no longer heard

    def test_help_is_rate_limited(self):
        self.put("decisions", "a", decision("A?", ["x"]))
        self.verified()
        replies = [self.t.inbound(self.PHONE, "I'm driving with Focus on") for _ in range(5)]  # an auto-reply loop
        self.assertEqual(sum(r is not None for r in replies), sms.HELP_MAX)
        self.assertEqual(len(self.client.sent), sms.HELP_MAX)
        self.assertEqual(self.t.inbound(self.PHONE, "help"), sms.HELP)  # asked for, always answered

    def test_unknown_sender_rejected(self):
        self.put("decisions", "solo", decision("Ship?", ["yes"]))
        self.assertIsNone(self.t.inbound(self.PHONE, "1"))  # nobody verified yet
        self.verified()
        for sender in ("+14155550199", "someone@example.com", "", None):
            self.assertIsNone(self.t.inbound(sender, "1"))
            self.assertIsNone(self.t.inbound(sender, "stop"))
        self.assertEqual(self.client.sent, [])  # strangers never get a reply
        self.assertIsNone(self.doc("responses", "solo"))
        self.assertEqual(self.doc("decisions", "solo")["status"], "open")
        self.assertTrue(self.doc("meta", "settings")["phoneVerified"])

    def test_writes_match_the_page(self):
        page = (HERE.parent / "index.html").read_text()
        body = page[page.index("async function writeAnswer"):page.index("function toast")]
        self.assertIn('db.doc("responses/" + id).set({ choice: p.choice || null, note: (p.note || "").trim(), submittedAt: new Date().toISOString() })', body)
        self.assertIn('if (d.status !== "answered") await db.doc("decisions/" + id).update({ status: "answered" })', body)
        # The page's writeAnswer through the shim (PUT, then PATCH) next to a texted answer.
        base = decision("Q?", ["a", "b"], agent={"kind": "session", "id": "s1"}, changedAt="2026-01-01T00:00:00Z")
        self.put("decisions", "by-page", base)
        self.put("decisions", "by-text", base)
        server.write_doc("responses", "by-page", {"choice": "b", "note": "ok", "submittedAt": "2026-11-05T23:22:56.569Z"})
        server.write_doc("decisions", "by-page", {"status": "answered"}, True)
        self.verified()
        self.t.inbound(self.PHONE, "2 ok")
        page_r, text_r = self.doc("responses", "by-page"), self.doc("responses", "by-text")
        self.assertEqual(set(text_r), set(page_r))
        self.assertEqual((text_r["choice"], text_r["note"]), (page_r["choice"], page_r["note"]))
        self.assertRegex(text_r["submittedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")  # toISOString() shape
        self.assertEqual(self.doc("decisions", "by-text"), {**base, "status": "answered"})
        self.assertIn("by-text", [i[0] for i in inject.pending("s1")])  # the hooks pick it up unchanged

    def test_reload_picks_up_spectrum_env(self):
        env, made = self.tmp / "spectrum.env", []
        fake = lambda path, cb: made.append(FakeClient()) or made[-1] if sms.load_env(path).get("SPECTRUM_PROJECT_ID") else None
        with mock.patch.object(sms, "bridge_from_env", fake):
            t = sms.Texting(server.read_doc, server.write_doc, server.list_docs, self.tmp / "s.json")
            t.reload(env)
            self.assertIsNone(t.client)
            env.write_text("SPECTRUM_PROJECT_ID=p\nSPECTRUM_PROJECT_SECRET=s\n")
            t.reload(env)
            self.assertIs(t.client, made[0])
            t.reload(env)
            self.assertEqual(len(made), 1)  # unchanged file, same client
            time.sleep(0.01)
            env.write_text("SPECTRUM_PROJECT_ID=p2\nSPECTRUM_PROJECT_SECRET=s\n")
            t.reload(env)
            self.assertEqual((len(made), made[0].stopped), (2, 1))  # new values: new client, old one stopped


class ApiTest(unittest.TestCase):
    def test_headers_and_errors(self):
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def urlopen(req, timeout):
            seen.append(req)
            return Resp(b'{"succeed": true, "data": {"type": "shared"}}')
        creds = {"SPECTRUM_PROJECT_ID": "proj-1", "SPECTRUM_PROJECT_SECRET": "s3cret"}
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(sms.api(creds, "GET", "/imessage/"), {"type": "shared"})
        h = dict(seen[0].header_items())
        self.assertEqual(seen[0].full_url, "https://spectrum.photon.codes/projects/proj-1/imessage/")
        self.assertEqual(h["User-agent"], "decision-desk-local")  # Python's default agent is refused upstream
        self.assertTrue(h["Authorization"].startswith("Basic "))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 401, "no", {}, None)):
            with self.assertRaisesRegex(sms.ApiError, "didn't accept") as cm:
                sms.api(creds, "GET", "/")
            self.assertNotIn("s3cret", str(cm.exception))
        with self.assertRaises(sms.ApiError):
            sms.api({"SPECTRUM_PROJECT_ID": "../x"}, "GET", "/")


class ProblemTest(unittest.TestCase):
    def test_webhook_check(self):
        b = sms.Bridge({"SPECTRUM_PROJECT_ID": "p", "SPECTRUM_PROJECT_SECRET": "s"}, None)
        with mock.patch.object(sms, "api", side_effect=sms.ApiError("Couldn't reach Photon.")):
            self.assertEqual(b.problem(), "Couldn't reach Photon.")  # not kept: tried again next time
        with mock.patch.object(sms, "api", return_value=[{"id": "w"}]) as api:
            self.assertEqual(b.problem(), sms.SHARED_PROJECT)
            self.assertEqual(b.problem(), sms.SHARED_PROJECT)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(api.call_args.args[1:], ("GET", "/webhooks/"))  # read-only
        ok = sms.Bridge({"SPECTRUM_PROJECT_ID": "p", "SPECTRUM_PROJECT_SECRET": "s", "SPECTRUM_ALLOW_WEBHOOKS": "1"}, None)
        with mock.patch.object(sms, "api", return_value=[{"id": "w"}]):
            self.assertIsNone(ok.problem())
        clean = sms.Bridge({"SPECTRUM_PROJECT_ID": "p", "SPECTRUM_PROJECT_SECRET": "s"}, None)
        with mock.patch.object(sms, "api", return_value=[]):
            self.assertIsNone(clean.problem())


class SetupTest(unittest.TestCase):
    def run_setup(self, answers, project, kind="shared", lines=(), hooks=()):
        tmp = Path(tempfile.mkdtemp())
        calls = {"/": project, "/imessage/": {"type": kind}, "/lines/": {"lines": list(lines)}, "/webhooks/": list(hooks)}
        answers = iter(answers)
        out = io.StringIO()
        with mock.patch.object(setup_texting, "STATE", tmp), mock.patch.object(setup_texting, "ENV", tmp / "spectrum.env"), \
             mock.patch.object(sms, "api", lambda creds, m, path, body=None: calls[path]), \
             mock.patch("builtins.input", lambda p="": next(answers)), mock.patch("getpass.getpass", lambda p="": "the-secret-value"), \
             mock.patch("urllib.request.urlopen", side_effect=OSError), mock.patch.object(setup_texting.time, "sleep"), \
             mock.patch("sys.stdout", out):
            code = setup_texting.main()
        return code, tmp / "spectrum.env", out.getvalue()

    def test_writes_private_env_and_never_prints_the_secret(self):
        code, env, out = self.run_setup(["proj-1"], {"name": "Desk"})
        self.assertEqual(code, 1)  # no server running in the test, so it says to start one
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertEqual(env.read_text(), "SPECTRUM_PROJECT_ID=proj-1\nSPECTRUM_PROJECT_SECRET=the-secret-value\nSPECTRUM_LINE=\nSPECTRUM_ALLOW_WEBHOOKS=\n")
        self.assertNotIn("the-secret-value", out)
        self.assertIn("shared pool", out)

    def test_dedicated_line_checked(self):
        line = {"platform": "imessage", "phoneNumber": "+14155550123", "status": "available"}
        code, env, out = self.run_setup(["proj-1"], {"name": "Desk"}, "dedicated", [line])
        self.assertIn("SPECTRUM_LINE=+14155550123", env.read_text())
        code, env, out = self.run_setup(["proj-1", "+14155550999"], {"name": "Desk"}, "dedicated", [line, {**line, "phoneNumber": "+14155550124"}])
        self.assertEqual(code, 1)
        self.assertFalse(env.exists())
        self.assertIn("isn't one of this project's lines", out)

    def test_webhooks_need_a_yes(self):
        code, env, out = self.run_setup(["proj-1", "n"], {"name": "Prod"}, hooks=[{"id": "w"}])
        self.assertFalse(env.exists())
        self.assertIn("1 webhook", out)
        code, env, out = self.run_setup(["proj-1", "y"], {"name": "Prod"}, hooks=[{"id": "w"}])
        self.assertIn("SPECTRUM_ALLOW_WEBHOOKS=1", env.read_text())


class BridgeTest(unittest.TestCase):
    FAKE = ("import json,sys\n"
            "print('sdk log noise', flush=True)\n"
            "print(json.dumps({'inbound': {'from': '+14155550100', 'text': 'hi'}}), flush=True)\n"
            "for line in sys.stdin:\n"
            "    r = json.loads(line)\n"
            "    print(json.dumps({'id': r['id'], 'ok': r['to'] != 'bad', 'error': 'nope'}), flush=True)\n")

    def test_stdio_protocol(self):
        got = []
        b = sms.Bridge({}, lambda f, t: got.append((f, t)))
        b.cmd = [sys.executable, "-c", self.FAKE]
        b.send("+14155550100", "hello")
        with self.assertRaises(RuntimeError):
            b.send("bad", "hello")
        time.sleep(0.2)
        self.assertEqual(got, [("+14155550100", "hi")])
        proc = b.proc
        b.stop()
        self.assertIsNotNone(proc.poll())
        proc.stdout.close()

    def test_fatal_start_fails_the_send(self):
        b = sms.Bridge({}, lambda f, t: None)
        b.cmd = [sys.executable, "-c", "import json; print(json.dumps({'fatal': 'Invalid project credentials'}), flush=True)"]
        with self.assertRaisesRegex(RuntimeError, "Invalid project credentials"):
            b.send("+14155550100", "hello")
        b.proc.wait(5)
        b.proc.stdin.close()
        b.proc.stdout.close()

    def test_off_without_credentials(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env") as f:
            f.write("SPECTRUM_PROJECT_ID=x\n")
            f.flush()
            with mock.patch.dict(os.environ, {"SPECTRUM_PROJECT_SECRET": ""}):
                self.assertIsNone(sms.bridge_from_env(f.name, None))  # no secret, texting stays off
        self.assertIsNone(sms.bridge_from_env("/nonexistent/spectrum.env", None))


if __name__ == "__main__":
    unittest.main()
