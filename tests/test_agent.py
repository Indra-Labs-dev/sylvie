"""Tests hors-ligne de l'intégration agent + outils : `python -m unittest discover -s tests`."""
import os, sys, tempfile, unittest
from unittest import mock

os.environ["SYLVIE_DB"] = os.path.join(tempfile.mkdtemp(), "a.db")
os.environ["GEMINI_API_KEY"] = "gk"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent, apis, tools, web  # noqa: E402


class Tools(unittest.TestCase):
    def test_registry_flags(self):
        r = tools.REGISTRY
        self.assertTrue(r["api_call"]["egress"] and r["read_url"]["egress"])
        self.assertEqual(r["api_call"]["risk_fn"]({"api": "meteo"}), "auto")
        self.assertTrue(r["api_call"]["taints_fn"]({"api": "wikipedia"}))
        self.assertFalse(r["api_call"]["taints_fn"]({"api": "meteo"}))

    def test_api_call_not_tainting_when_structured(self):
        state = {"tainted": False}
        with mock.patch.object(apis, "call", return_value={"api": "meteo", "resultat": "20°"}), \
             mock.patch.object(agent.gate, "log"):
            out = agent.run_tool("api_call", {"api": "meteo", "params": "{}"}, state)
        self.assertIn("result", out)
        self.assertFalse(state["tainted"])

    def test_free_text_api_taints_then_egress_needs_confirmation(self):
        state = {"tainted": False}
        with mock.patch.object(apis, "call", return_value={"api": "wikipedia", "resultat": "texte"}), \
             mock.patch.object(agent.gate, "log"), mock.patch.object(agent.gate, "ask", return_value=False) as ask:
            agent.run_tool("api_call", {"api": "wikipedia", "params": "{}"}, state)
            self.assertTrue(state["tainted"])
            ask.assert_not_called()  # le premier appel n'était pas « après contenu externe »
            out = agent.run_tool("api_call", {"api": "meteo", "params": "{}"}, state)
            ask.assert_called_once()
        self.assertEqual(out, {"error": "refusé par l'utilisateur"})

    def test_unknown_tool_and_api_errors_are_returned_not_raised(self):
        self.assertIn("error", agent.run_tool("nope", {}, {"tainted": False}))
        with mock.patch.object(agent.gate, "log"):
            out = agent.run_tool("api_call", {"api": "inexistante"}, {"tainted": False})
        self.assertIn("error", out)


class Web(unittest.TestCase):
    def test_private_addresses_refused(self):
        for u in ("http://127.0.0.1/", "http://192.168.1.1/x", "ftp://example.com", "http://user:pw@example.com/"):
            with self.assertRaises(web.WebError):
                web.fetch_text(u)

    def test_html_to_text(self):
        t, x = web.html_to_text("<html><title>Hé</title><script>bad()</script><p>Bonjour <b>toi</b></p><nav>menu</nav></html>")
        self.assertEqual(t, "Hé")
        self.assertIn("Bonjour toi", x)
        self.assertNotIn("bad", x)
        self.assertNotIn("menu", x)


class Loop(unittest.TestCase):
    def test_tool_round_trip_marks_provider(self):
        replies = [
            {"content": {"parts": [{"functionCall": {"name": "get_time", "args": {}}}]}, "provider": "gemini-flash"},
            {"content": {"parts": [{"text": "Il est tard."}]}, "provider": "gemini-lite"},
        ]
        contents, said = [], []
        with mock.patch.object(agent.llm, "generate", side_effect=replies), \
             mock.patch.object(agent, "say", side_effect=said.append), mock.patch.object(agent.gate, "log"):
            self.assertIsNone(agent.ask_model("quelle heure ?", contents))
        self.assertEqual(said, ["Il est tard."])
        self.assertEqual([m.get("_provider") for m in contents if m["role"] == "model"], ["gemini-flash", "gemini-lite"])

    def test_failure_rolls_back_history(self):
        contents = []
        with mock.patch.object(agent.llm, "generate", side_effect=RuntimeError("panne")):
            self.assertEqual(agent.ask_model("salut", contents), "salut")
        self.assertEqual(contents, [])

    def test_prompt_contains_relevant_signature(self):
        self.assertIn("meteo(latitude, longitude)", agent.system_prompt("quelle météo demain ?"))

    def test_commands_do_not_crash(self):
        for c in ("/fournisseurs", "/fournisseurs aide", "/apis cve", "/apis zzzz", "/modele", "/outils"):
            self.assertTrue(agent.command(c, []))


if __name__ == "__main__":
    unittest.main()
