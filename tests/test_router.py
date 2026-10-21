"""Tests hors-ligne du routeur de fournisseurs : `python -m unittest discover -s tests`."""
import json, os, sys, tempfile, time, unittest
from unittest import mock

os.environ["SYLVIE_DB"] = os.path.join(tempfile.mkdtemp(), "r.db")
os.environ["GEMINI_API_KEY"] = "gk"
os.environ["GROQ_API_KEY"] = "qk"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

PROVS = [
    {"id": "gemini-flash", "kind": "gemini", "model": "g-flash", "env": "GEMINI_API_KEY", "roles": ["chat", "fast", "search", "audio"], "trust": "private"},
    {"id": "gemini-lite", "kind": "gemini", "model": "g-lite", "env": "GEMINI_API_KEY", "roles": ["chat", "fast", "search"], "trust": "private"},
    {"id": "groq", "kind": "openai", "base_url": "https://groq.example/v1", "model": "llama-x", "env": "GROQ_API_KEY", "roles": ["fast", "chat"], "trust": "public"},
    {"id": "nokey", "kind": "openai", "base_url": "https://n.example/v1", "model": "m", "env": "NOPE_KEY", "roles": ["fast"], "trust": "public"},
]


class R:
    def __init__(self, data, code=200, headers=None):
        self.status_code, self._d, self.headers = code, data, headers or {}
        self.ok = code < 400
        self.text = json.dumps(data)

    def json(self):
        return self._d


def gem(text="ok", calls=None):
    parts = [{"text": text}] if text else []
    parts += [{"functionCall": {"name": n, "args": a}, **({"thoughtSignature": "SIG"} if i == 0 else {})} for i, (n, a) in enumerate(calls or [])]
    return R({"candidates": [{"content": {"role": "model", "parts": parts}}]})


def oai(text="ok", calls=None):
    m = {"role": "assistant", "content": text}
    if calls:
        m["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return R({"choices": [{"message": m}]})


def err(code, msg, headers=None):
    return R({"error": {"message": msg}}, code, headers)


class Base(unittest.TestCase):
    def setUp(self):
        router.reset_pauses()
        router._sql("DELETE FROM usage")
        self.sent = []
        self.p1 = mock.patch.object(router, "providers", lambda: [dict(p) for p in PROVS])
        self.p1.start()
        self.p2 = mock.patch("time.sleep", lambda s: None)
        self.p2.start()

    def tearDown(self):
        mock.patch.stopall()

    def route(self, handler):
        def post(url, headers=None, json=None, timeout=None):
            self.sent.append((url, headers, json))
            return handler(url, headers, json)
        return mock.patch.object(router.HTTP, "post", post)

    def ask(self, **kw):
        return router.generate(kw.pop("role", "chat"), kw.pop("contents", [{"role": "user", "parts": [{"text": "salut"}]}]), **kw)


class Failover(Base):
    def test_quota_pause_puis_secours(self):
        def h(url, hd, body):
            return err(429, "You exceeded your current quota, please check your plan.") if "g-flash" in url else gem("secours")
        with self.route(h):
            c = self.ask()
            self.assertEqual((c["provider"], router.text_of(c["content"])), ("gemini-lite", "secours"))
            self.sent.clear()
            c = self.ask()  # gemini-flash est en pause : pas de nouvel appel inutile
            self.assertEqual([u for u, *_ in self.sent if "g-flash" in u], [])
        self.assertIn("quota", router.cooldown("gemini-flash")[1])
        self.assertGreater(router.cooldown("gemini-flash")[0] - time.time(), 3000)

    def test_limite_par_minute_pause_courte(self):
        with self.route(lambda u, h, b: err(429, "slow down", {"retry-after": "20"}) if "g-flash" in u else gem()):
            self.ask()
        self.assertLess(router.cooldown("gemini-flash")[0] - time.time(), 60)

    def test_surcharge_deux_essais_puis_suivant(self):
        calls = {"n": 0}

        def h(u, hd, b):
            if "g-flash" in u:
                calls["n"] += 1
                return err(503, "high demand")
            return gem("lite")
        with self.route(h):
            self.assertEqual(self.ask()["provider"], "gemini-lite")
        self.assertEqual(calls["n"], 2)

    def test_cle_refusee_pause_longue(self):
        with self.route(lambda u, h, b: err(403, "API key invalid") if "g-flash" in u else gem()):
            self.ask()
        self.assertGreater(router.cooldown("gemini-flash")[0] - time.time(), 40000)

    def test_tout_en_panne_message_clair(self):
        with self.route(lambda u, h, b: err(503, "high demand")):
            with self.assertRaises(router.NoProvider) as cm:
                self.ask()
        self.assertIn("gemini-flash → 503 surchargé", str(cm.exception))
        self.assertNotIn("nokey", str(cm.exception))

    def test_reponse_bloquee_passe_au_suivant_sans_pause(self):
        with self.route(lambda u, h, b: R({"candidates": [{"finishReason": "SAFETY"}]}) if "g-flash" in u else gem("ok")):
            self.assertEqual(self.ask()["provider"], "gemini-lite")
        self.assertIsNone(router.cooldown("gemini-flash"))

    def test_budget_du_jour(self):
        PROVS[0]["daily_calls"] = 2
        try:
            with self.route(lambda u, h, b: gem()):
                self.assertEqual([self.ask()["provider"] for _ in range(3)], ["gemini-flash", "gemini-flash", "gemini-lite"])
        finally:
            PROVS[0].pop("daily_calls")


class Confidentialite(Base):
    def test_prive_jamais_vers_fournisseur_public(self):
        with self.route(lambda u, h, b: err(503, "x") if "g-" in u else oai("groq")):
            with self.assertRaises(router.NoProvider) as cm:
                self.ask()  # private=True par défaut
        self.assertEqual([u for u, *_ in self.sent if "groq" in u], [])
        self.assertIn("groq → non fiable", str(cm.exception))

    def test_public_prefere_les_fournisseurs_publics(self):
        with self.route(lambda u, h, b: oai("résumé") if "groq" in u else gem()):
            c = self.ask(role="fast", private=False)
        self.assertEqual(c["provider"], "groq")
        self.assertEqual(self.sent[0][1]["Authorization"], "Bearer qk")
        self.assertEqual(self.sent[0][0], "https://groq.example/v1/chat/completions")

    def test_public_retombe_sur_gemini_si_groq_en_panne(self):
        with self.route(lambda u, h, b: err(429, "Rate limit reached for model per day") if "groq" in u else gem("g")):
            self.assertEqual(self.ask(role="fast", private=False)["provider"], "gemini-flash")

    def test_sans_cle_ignore_et_recherche_gemini_seulement(self):
        with self.route(lambda u, h, b: gem("web")):
            c = self.ask(role="search", private=False, tools=[{"google_search": {}}], thinking=False)
        self.assertEqual(c["provider"], "gemini-flash")
        self.assertNotIn("generationConfig", self.sent[0][2])
        self.assertEqual(self.sent[0][2]["tools"], [{"google_search": {}}])

    def test_audio_gemini_seulement(self):
        with self.route(lambda u, h, b: gem("texte")):
            c = self.ask(role="audio", contents=[{"role": "user", "parts": [{"inlineData": {"mimeType": "audio/m4a", "data": "AA"}}]}])
        self.assertEqual(c["provider"], "gemini-flash")


class Conversion(Base):
    DECL = [{"name": "battery", "description": "Batterie"},
            {"name": "calculate", "description": "Calcule", "parameters": {"type": "OBJECT", "properties": {"expression": {"type": "STRING"}}, "required": ["expression"]}}]
    HIST = [{"role": "user", "parts": [{"text": "batterie ?"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "battery", "args": {}}, "thoughtSignature": "SIG"}], "_provider": "groq"},
            {"role": "user", "parts": [{"functionResponse": {"name": "battery", "response": {"result": {"percentage": 79}}}}]}]

    def test_vers_openai(self):
        with self.route(lambda u, h, b: oai("79 %")):
            c = self.ask(role="fast", private=False, contents=self.HIST, system="Sois bref", tools=[{"functionDeclarations": self.DECL}])
        body = self.sent[0][2]
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(body["messages"][2]["tool_calls"][0]["function"], {"name": "battery", "arguments": "{}"})
        self.assertEqual(body["messages"][3]["tool_call_id"], body["messages"][2]["tool_calls"][0]["id"])
        self.assertEqual(body["tools"][1]["function"]["parameters"]["properties"]["expression"]["type"], "string")
        self.assertEqual(body["tools"][0]["function"]["parameters"], {"type": "object", "properties": {}})
        self.assertNotIn("_provider", json.dumps(body))

    def test_depuis_openai_appel_outil(self):
        with self.route(lambda u, h, b: oai(None, [("calculate", {"expression": "1+1"})])):
            c = self.ask(role="fast", private=False)
        self.assertEqual(c["content"]["parts"], [{"functionCall": {"name": "calculate", "args": {"expression": "1+1"}}}])

    def test_gemini_garde_les_signatures_de_gemini(self):
        hist = [{"role": "user", "parts": [{"text": "x"}]},
                {"role": "model", "parts": [{"functionCall": {"name": "battery", "args": {}}, "thoughtSignature": "SIG"}], "_provider": "gemini-flash"},
                {"role": "user", "parts": [{"functionResponse": {"name": "battery", "response": {"r": 1}}}]}]
        with self.route(lambda u, h, b: gem("ok")):
            self.ask(contents=hist)
        sent = self.sent[0][2]["contents"]
        self.assertEqual(sent[1]["parts"][0]["thoughtSignature"], "SIG")
        self.assertIn("functionResponse", sent[2]["parts"][0])
        self.assertNotIn("_provider", json.dumps(sent))

    def test_echange_etranger_devient_texte_pour_gemini(self):
        with self.route(lambda u, h, b: gem("ok")):
            self.ask(contents=self.HIST)
        sent = self.sent[0][2]["contents"]
        self.assertIn("[appel de l'outil battery", sent[1]["parts"][0]["text"])
        self.assertIn("[résultat de battery", sent[2]["parts"][0]["text"])
        self.assertNotIn("functionCall", json.dumps(sent))
        self.assertEqual(self.HIST[1].get("_provider"), "groq")  # l'historique d'origine n'est pas modifié

    def test_audio_jamais_envoye_a_un_fournisseur_openai(self):
        PROVS[2]["roles"] = ["audio"]  # même s'il se déclare capable, l'audio ne part pas vers lui
        try:
            with self.route(lambda u, h, b: gem("texte")):
                c = self.ask(role="audio", private=False,
                             contents=[{"role": "user", "parts": [{"inlineData": {"mimeType": "audio/m4a", "data": "AA"}}]}])
        finally:
            PROVS[2]["roles"] = ["fast", "chat"]
        self.assertEqual(c["provider"], "gemini-flash")
        self.assertEqual([u for u, *_ in self.sent if "groq" in u], [])


class Admin(Base):
    def test_table_et_test(self):
        t = {r["id"]: r for r in router.table()}
        self.assertTrue(t["gemini-flash"]["ok"])
        self.assertEqual(t["nokey"]["why"], "clé absente")
        with self.route(lambda u, h, b: gem("OK")):
            ok, note = router.test("gemini-flash")
        self.assertTrue(ok)
        self.assertFalse(router.test("nokey")[0])

    def test_modeles_openai(self):
        with mock.patch.object(router.HTTP, "get", lambda url, **kw: R({"data": [{"id": "b"}, {"id": "a"}]})):
            self.assertEqual(router.list_models("groq"), ["a", "b"])

    def test_primary(self):
        self.assertEqual(router.primary()["id"], "gemini-flash")
        router.pause("gemini-flash", 600, "x")
        self.assertEqual(router.primary()["id"], "gemini-lite")


class Fichiers(unittest.TestCase):
    def test_providers_json_valide(self):
        with open(os.path.join(router.HERE, "providers.json"), encoding="utf-8") as f:
            data = json.load(f)["providers"]
        ids = [p["id"] for p in data]
        self.assertEqual(len(ids), len(set(ids)))
        for p in data:
            self.assertIn(p["kind"], ("gemini", "openai"))
            self.assertIn(p["trust"], ("private", "public"))
            if p["kind"] == "openai":
                self.assertTrue(p["base_url"].startswith("https://"))
                self.assertEqual(p["model"], "")  # à choisir par l'utilisateur
                self.assertEqual(p["trust"], "public")


if __name__ == "__main__":
    unittest.main()
