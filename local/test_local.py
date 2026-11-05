"""Self-check for local mode: python3 -m unittest local/test_local.py (stdlib only, no network, no texts)."""
import http.client, json, os, re, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import inject, server, sms  # noqa: E402


class FakeClient:
    """Stands in for the Spectrum bridge. Records texts instead of sending them."""
    def __init__(self):
        self.sent, self.fail = [], False

    def start(self):
        pass

    def send(self, to, text):
        if self.fail:
            raise RuntimeError("down")
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
        self.client.sent.clear()

    def test_connect_verify(self):
        self.assertEqual(self.t.route("status", {})[1], {"configured": True, "verified": False, "last4": None, "codeSent": False})
        self.verified()
        self.assertEqual(self.doc("meta", "settings"), {"phone": self.PHONE, "phoneVerified": True})
        self.assertEqual(self.t.route("status", {})[1]["last4"], "0100")
        self.assertEqual(self.t.route("disconnect", {})[0], 200)
        self.assertEqual(self.doc("meta", "settings"), {})

    def test_wrong_code_and_limits(self):
        self.assertEqual(self.t.route("connect", {"phone": "12"})[0], 400)
        code = self.connect()
        self.assertEqual(self.t.route("connect", {"phone": self.PHONE})[0], 429)  # too soon for another code
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(sms.MAX_TRIES - 1):
            self.assertEqual(self.t.route("verify", {"code": wrong})[0], 400)
        self.assertEqual(self.t.route("verify", {"code": wrong})[0], 429)
        self.assertEqual(self.t.route("verify", {"code": code})[0], 400)  # locked out: the right code is gone too
        self.assertIsNone(self.doc("meta", "settings"))
        self.t.client = None
        self.assertEqual(self.t.route("connect", {"phone": self.PHONE})[0], 503)

    def test_outbound_dedupe(self):
        self.put("decisions", "one", decision("First?", ["a", "b"], recommended="b", order=1))
        self.put("decisions", "two", decision("Second?", ["a"], order=2))
        self.put("decisions", "gone", decision("Answered", ["a"], status="answered"))
        self.assertIsNone(self.t.tick())  # no verified phone, nothing goes out
        self.verified()
        self.assertEqual([self.t.tick(), self.t.tick(), self.t.tick()], ["one", "two", None])
        first = self.client.sent[0][1]
        self.assertEqual(first.splitlines()[:3], ["[one] First?", "1. A", "2. B (recommended)"])
        self.assertIn('"one <n>"', first)  # several open, so the tag form
        server.write_doc("decisions", "one", {"changedAt": "2026-01-05T00:00:00Z"}, True)
        self.assertEqual([self.t.tick(), self.t.tick()], ["one", None])  # new state, texted once more
        self.client.fail = True
        server.write_doc("decisions", "two", {"changedAt": "2026-01-06T00:00:00Z"}, True)
        self.assertIsNone(self.t.tick())
        self.client.fail = False
        self.assertIsNone(self.t.tick())  # backing off after a failure
        self.t.retry_at = 0
        self.assertEqual(self.t.tick(), "two")

    def test_reply_one_pending(self):
        self.put("decisions", "solo", decision("Ship?", ["yes", "no"]))
        self.verified()
        self.t.tick()
        self.assertIn('Reply "<n>"', self.client.sent[0][1])
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
            self.assertIn("Several are open", self.t.inbound(self.PHONE, text))
        self.assertEqual(list(self.db.glob("responses/*.json")), [])
        self.assertEqual(self.t.inbound(self.PHONE, "RC7-CVE 2 with an expiry"), "Sent: [rc7-cve] Allow")
        self.assertEqual(self.doc("responses", "rc7-cve")["note"], "with an expiry")
        self.assertEqual(self.t.inbound(self.PHONE, "[rc7-key] 1"), "Sent: [rc7-key] Me")
        self.assertEqual(self.doc("responses", "rc7-key")["note"], "")

    def test_list_resends(self):
        self.put("decisions", "a", decision("A?", ["x"], order=1))
        self.put("decisions", "b", decision("B?", ["x"], order=2))
        self.verified()
        self.t.tick(), self.t.tick()
        self.assertIsNone(self.t.tick())
        self.assertIsNone(self.t.inbound(self.PHONE, "List"))
        self.assertEqual([self.t.tick(), self.t.tick(), self.t.tick()], ["a", "b", None])

    def test_unknown_sender_rejected(self):
        self.put("decisions", "solo", decision("Ship?", ["yes"]))
        self.assertIsNone(self.t.inbound(self.PHONE, "1"))  # nobody verified yet
        self.verified()
        for sender in ("+14155550199", "someone@example.com", "", None):
            self.assertIsNone(self.t.inbound(sender, "1"))
        self.assertEqual(self.client.sent, [])  # strangers never get a reply
        self.assertIsNone(self.doc("responses", "solo"))
        self.assertEqual(self.doc("decisions", "solo")["status"], "open")

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
        b.proc.stdin.close()
        b.proc.wait(5)
        b.proc.stdout.close()

    def test_off_without_full_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env") as f:
            f.write("SPECTRUM_PROJECT_ID=x\nSPECTRUM_PROJECT_SECRET=y\n")
            f.flush()
            with mock.patch.dict(os.environ, {"SPECTRUM_LINE": ""}):
                self.assertIsNone(sms.bridge_from_env(f.name, None))  # no line, texting stays off
        self.assertIsNone(sms.bridge_from_env("/nonexistent/spectrum.env", None))


if __name__ == "__main__":
    unittest.main()
