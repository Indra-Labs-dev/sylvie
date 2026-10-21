"""Tests du serveur web local (vrai HTTP sur 127.0.0.1, modèle simulé)."""
import json, os, socket, sys, tempfile, threading, time, unittest
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["SYLVIE_DB"] = os.path.join(TMP, "s.db")
os.environ["GEMINI_API_KEY"] = "gk"
os.environ["SYLVIE_FILES_ROOTS"] = os.path.join(TMP, "docs")
os.makedirs(os.path.join(TMP, "docs"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests  # noqa: E402
import engine, server, tools  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def reply(text=None, calls=()):
    parts = ([{"text": text}] if text else []) + [{"functionCall": {"name": n, "args": a}} for n, a in calls]
    return {"content": {"parts": parts}, "provider": "gemini-flash"}


def wait_for(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        r = pred()
        if r:
            return r
        time.sleep(0.02)
    raise AssertionError("délai dépassé")


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(SYLVIE_DB=os.path.join(TMP, "s.db"), SYLVIE_FILES_ROOTS=os.path.join(TMP, "docs"))
        server.PORT = free_port()
        server.TOKEN_FILE = os.path.join(TMP, ".token")
        cls.tok = server.token()
        cls.httpd = server.Server(("127.0.0.1", server.PORT), server.Handler, cls.tok)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{server.PORT}"
        cls.auth = {"X-Sylvie-Token": cls.tok}

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        wait_for(lambda: not server.Web.lock.locked())
        server.Web.contents.clear()
        self.mark = server.hub.next

    def events(self, type_):
        return [e for e in server.hub.events if e["id"] >= self.mark and e["type"] == type_]

    def post(self, path, body=None, headers=None):
        return requests.post(self.base + path, json=body or {}, headers={**self.auth, **(headers or {})}, timeout=5)

    # --- accès
    def test_token_required_everywhere_but_pwa_assets(self):
        self.assertEqual(requests.get(self.base + "/", timeout=5).status_code, 401)
        self.assertEqual(requests.get(self.base + "/api/state", timeout=5).status_code, 401)
        self.assertEqual(requests.post(self.base + "/api/chat", json={"text": "x"}, timeout=5).status_code, 401)
        self.assertEqual(requests.get(self.base + "/api/state", headers={"X-Sylvie-Token": "faux"}, timeout=5).status_code, 401)
        self.assertEqual(requests.get(self.base + "/manifest.webmanifest", timeout=5).status_code, 200)
        icon = requests.get(self.base + "/icon-192.png", timeout=5)
        self.assertEqual(icon.content[:8], b"\x89PNG\r\n\x1a\n")

    def test_host_header_checked_against_dns_rebinding(self):
        r = requests.get(self.base + "/api/state", headers={**self.auth, "Host": "evil.example"}, timeout=5)
        self.assertEqual(r.status_code, 403)

    def test_open_link_sets_strict_httponly_cookie(self):
        r = requests.get(f"{self.base}/?t={self.tok}", allow_redirects=False, timeout=5)
        self.assertEqual(r.status_code, 302)
        cookie = r.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        s = requests.Session()
        s.get(f"{self.base}/?t={self.tok}", timeout=5)
        page = s.get(self.base + "/", timeout=5)
        self.assertEqual(page.status_code, 200)
        self.assertIn("SYL", page.text)
        self.assertEqual(requests.get(f"{self.base}/?t=faux", timeout=5).status_code, 401)

    def test_csrf_origin_checks(self):
        self.assertEqual(self.post("/api/stop", headers={"Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.post("/api/stop", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.post("/api/stop", headers={"Origin": self.base}).status_code, 200)

    # --- conversation
    def test_chat_round_trip_and_state(self):
        with mock.patch.object(engine.llm, "generate", return_value=reply("Bonjour Navanime")):
            self.assertEqual(self.post("/api/chat", {"text": "salut"}).status_code, 200)
            wait_for(lambda: self.events("say"))
        self.assertEqual(self.events("say")[0]["text"], "Bonjour Navanime")
        st = requests.get(self.base + "/api/state", headers=self.auth, timeout=5).json()
        for k in ("busy", "providers", "jobs", "pending", "goals", "api_names"):
            self.assertIn(k, st)
        self.assertEqual(self.post("/api/chat", {"text": "  "}).status_code, 400)

    def test_busy_returns_409(self):
        gate = threading.Event()
        with mock.patch.object(engine.llm, "generate", side_effect=lambda *a, **k: gate.wait(5) and reply("ok")):
            self.assertEqual(self.post("/api/chat", {"text": "long"}).status_code, 200)
            self.assertEqual(self.post("/api/chat", {"text": "autre"}).status_code, 409)
            gate.set()
            wait_for(lambda: self.events("say"))

    def test_confirm_action_needs_click(self):
        fn = mock.Mock(return_value="allumée")
        seq = iter([reply(calls=[("torch", {"on": True})]), reply("Fait.")])
        with mock.patch.object(engine.llm, "generate", side_effect=lambda *a, **k: next(seq)), \
             mock.patch.dict(tools.REGISTRY["torch"], {"fn": fn}):
            self.post("/api/chat", {"text": "allume la lampe"})
            ask = wait_for(lambda: self.events("approval"))[0]
            self.assertEqual((ask["name"], ask["risk"]), ("torch", "confirm"))
            fn.assert_not_called()
            self.assertEqual(self.post("/api/approve", {"aid": ask["aid"], "ok": True}).status_code, 200)
            wait_for(lambda: self.events("say"))
        fn.assert_called_once()

    def test_sensitive_action_requires_typing_OUI(self):
        for typed, expect_call in (("oui", False), ("OUI", True)):
            fn = mock.Mock(return_value="envoyé")
            seq = iter([reply(calls=[("sms_send", {"number": "+2250102030405", "text": "salut"})]), reply("ok")])
            self.mark = server.hub.next
            with mock.patch.object(engine.llm, "generate", side_effect=lambda *a, **k: next(seq)), \
                 mock.patch.dict(tools.REGISTRY["sms_send"], {"fn": fn}):
                self.post("/api/chat", {"text": "envoie"})
                ask = wait_for(lambda: self.events("approval"))[0]
                self.assertEqual(ask["risk"], "sensitive")
                self.post("/api/approve", {"aid": ask["aid"], "ok": True, "text": typed})
                wait_for(lambda: self.events("say"))
            self.assertEqual(fn.called, expect_call, typed)
            wait_for(lambda: not server.Web.lock.locked())

    def test_stop_refuses_pending_approval(self):
        fn = mock.Mock()
        seq = iter([reply(calls=[("torch", {"on": True})]), reply("ok")])
        with mock.patch.object(engine.llm, "generate", side_effect=lambda *a, **k: next(seq)), mock.patch.dict(tools.REGISTRY["torch"], {"fn": fn}):
            self.post("/api/chat", {"text": "lampe"})
            wait_for(lambda: self.events("approval"))
            self.post("/api/stop")
            wait_for(lambda: self.events("approval_done"))
            wait_for(lambda: self.events("idle"))
        fn.assert_not_called()

    # --- tâches
    def test_job_lifecycle_through_api(self):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 7200))
        r = self.post("/api/job/add", {"kind": "rappel", "title": "Test", "text": "msg", "at": when})
        self.assertEqual(r.status_code, 200, r.text)
        jid = r.json()["id"]
        self.assertEqual(self.post("/api/job/add", {"kind": "tache", "title": "x", "text": "y", "daily": "08:00", "tools_list": "sms_send"}).status_code, 400)
        self.assertEqual(self.post("/api/job", {"id": jid, "action": "off"}).status_code, 200)
        st = requests.get(self.base + "/api/state", headers=self.auth, timeout=5).json()
        self.assertFalse([j for j in st["jobs"] if j["id"] == jid][0]["enabled"])
        self.assertEqual(self.post("/api/job", {"id": jid, "action": "delete"}).status_code, 200)
        self.assertEqual(self.post("/api/job", {"id": jid, "action": "delete"}).status_code, 404)

    def test_upload_goes_to_inbox_and_is_filtered(self):
        import base64
        enc = lambda b: base64.b64encode(b).decode()
        r = self.post("/api/upload", {"name": "../../etc/Rapport final.pdf", "data": enc(b"%PDF-1.4 test")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["path"], "inbox/Rapport final.pdf")
        self.assertTrue(os.path.exists(os.path.join(TMP, "docs", "inbox", "Rapport final.pdf")))
        again = self.post("/api/upload", {"name": "Rapport final.pdf", "data": enc(b"%PDF-1.4 autre")}).json()["path"]
        self.assertNotEqual(again, "inbox/Rapport final.pdf")          # pas d'écrasement
        for bad in ({"name": "virus.exe", "data": enc(b"MZ")}, {"name": ".env", "data": enc(b"k")}, {"name": "a.txt", "data": "pas du base64 !"},
                    {"name": "a.txt", "data": ""}, {"name": "a.txt", "data": enc(b"x" * 8_100_000)}):
            self.assertEqual(self.post("/api/upload", bad).status_code, 400, bad["name"])
        self.assertEqual(requests.post(self.base + "/api/upload", json={"name": "a.txt", "data": enc(b"x")}, timeout=5).status_code, 401)

    def test_event_stream(self):
        server.hub.publish("info", level="info", text="ping-test")
        with requests.get(self.base + "/api/events?last=0", headers=self.auth, stream=True, timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            got = ""
            for line in r.iter_lines(chunk_size=1, decode_unicode=True):
                got += line or ""
                if "ping-test" in got:
                    break
            self.assertIn("ping-test", got)


    # --- voix (mains libres)
    @staticmethod
    def wav(seconds=1.0, rate=16000, channels=1):
        import base64, io, wave
        b = io.BytesIO()
        with wave.open(b, "wb") as w:
            w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(b"\x00\x01" * int(rate * seconds) * channels)
        return base64.b64encode(b.getvalue()).decode()

    def test_voice_transcribes_then_runs_a_turn(self):
        with mock.patch.object(server.agent, "transcribe_bytes", return_value="/quelle heure est-il") as tr, \
             mock.patch.object(engine.llm, "generate", return_value=reply("Il est midi")):
            r = self.post("/api/voice", {"data": self.wav()})
            self.assertEqual(r.json()["result"], "started")
            wait_for(lambda: self.events("say"))
        self.assertEqual(tr.call_args[0][1], "audio/wav")
        self.assertEqual(self.events("user")[0]["text"], "quelle heure est-il")      # le « / » initial est retiré

    def test_voice_empty_busy_and_validation(self):
        with mock.patch.object(server.agent, "transcribe_bytes", return_value=""):
            self.assertEqual(self.post("/api/voice", {"data": self.wav()}).json()["result"], "empty")
            self.assertTrue(self.events("voice")[0]["empty"])
        with mock.patch.object(server.agent, "transcribe_bytes") as tr:
            for bad in ("pas du base64 !!", "UklGRg==", self.wav(channels=2), self.wav(rate=4000), self.wav(seconds=61, rate=8000)):
                self.assertEqual(self.post("/api/voice", {"data": bad}).status_code, 400, bad[:20])
            self.assertEqual(self.post("/api/voice", {}).status_code, 400)
            server.Web.lock.acquire()
            try:
                self.assertEqual(self.post("/api/voice", {"data": self.wav()}).json()["result"], "busy")
            finally:
                server.Web.lock.release()
            tr.assert_not_called()
        self.assertEqual(requests.post(self.base + "/api/voice", json={"data": self.wav()}, timeout=5).status_code, 401)


if __name__ == "__main__":
    unittest.main()
