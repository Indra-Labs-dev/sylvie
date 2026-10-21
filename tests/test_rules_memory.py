"""Tests hors-ligne : règles permanentes, mémoire structurée, habitudes."""
import os, sys, tempfile, time, unittest
from unittest import mock

TMP = tempfile.mkdtemp()
DOCS = os.path.join(TMP, "docs")
OTHER = os.path.join(TMP, "autre")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(SYLVIE_DB=os.path.join(TMP, "rm.db"), SYLVIE_FILES_ROOTS=os.pathsep.join([DOCS, OTHER]), HOME=TMP, GEMINI_API_KEY="gk")
for d in (DOCS, OTHER, os.path.join(DOCS, "sub")):
    os.makedirs(d, exist_ok=True)
import engine, habits, memory, rules  # noqa: E402

memory.DB = os.environ["SYLVIE_DB"]


def setUpModule():
    os.environ.update(SYLVIE_DB=os.path.join(TMP, "rm.db"), SYLVIE_FILES_ROOTS=os.pathsep.join([DOCS, OTHER]))
    memory.DB = os.environ["SYLVIE_DB"]


class Rules(unittest.TestCase):
    def setUp(self):
        for r in rules.rules():
            rules.remove(r["id"])

    def test_validation(self):
        for bad in [("sms_send", ""), ("read_url", ""), ("read_url", "http://127.0.0.1"), ("read_url", "localhost"),
                    ("files_read", "/etc"), ("files_read", os.path.join(DOCS, "nope")), ("notify", "x")]:
            with self.assertRaises(ValueError, msg=bad):
                rules.add(*bad)

    def test_host_matching(self):
        rid = rules.add("read_url", "https://Wikipedia.org/x")
        ok = lambda u: rules.allows("read_url", {"url": u}, "confirm", False)
        self.assertEqual(ok("https://fr.wikipedia.org/wiki/A")["id"], rid)
        self.assertEqual(ok("https://wikipedia.org")["id"], rid)
        self.assertIsNone(ok("https://evilwikipedia.org"))
        self.assertIsNone(ok("https://wikipedia.org.evil.com"))
        self.assertIsNone(ok("pas une url"))

    def test_dir_matching_and_overwrite(self):
        rules.add("files_write", DOCS)
        w = lambda p, **k: rules.allows("files_write", {"path": p, **k}, "confirm", False)
        self.assertTrue(w(os.path.join(DOCS, "sub", "a.txt")))
        self.assertIsNone(w(os.path.join(OTHER, "a.txt")))
        self.assertIsNone(w(os.path.join(DOCS, "a.txt"), overwrite=True))
        self.assertIsNone(w(os.path.join(DOCS, "..", "autre", "a.txt")))

    def test_exclusions(self):
        rules.add("notify")
        self.assertTrue(rules.allows("notify", {}, "confirm", False))
        self.assertIsNone(rules.allows("notify", {}, "confirm", True))      # après contenu externe
        self.assertIsNone(rules.allows("notify", {}, "sensitive", False))
        self.assertIsNone(rules.allows("notify", {}, "auto", False))

    def test_expiry_dedupe_and_cap(self):
        now = time.time()
        a = rules.add("torch", days=1, now=now)
        self.assertEqual(rules.add("torch"), a)
        self.assertEqual(len(rules.rules(now)), 1)
        self.assertEqual(len(rules.rules(now + 2 * 86400)), 0)
        with mock.patch.object(rules, "MAX_RULES", 1):
            rules.add("vibrate")
            with self.assertRaises(ValueError):
                rules.add("location")

    def test_suggest(self):
        self.assertEqual(rules.suggest("read_url", {"url": "https://a.b.org/x"}), "a.b.org")
        self.assertEqual(rules.suggest("files_read", {"path": os.path.join(DOCS, "x.txt")}), os.path.realpath(DOCS))
        self.assertEqual(rules.suggest("notify", {}), "")
        self.assertIsNone(rules.suggest("sms_send", {}))


class FakeIO(engine.IO):
    def __init__(self):
        self.asked, self.infos = [], []

    def approve(self, name, args, risk, tainted):
        self.asked.append(name)
        return True

    def info(self, m):
        self.infos.append(m)


class EngineRules(unittest.TestCase):
    def setUp(self):
        for r in rules.rules():
            rules.remove(r["id"])

    def run_tool(self, name, state=None):
        io = FakeIO()
        with mock.patch.dict(engine.tools.REGISTRY, {name: {**engine.tools.REGISTRY[name], "fn": lambda **k: "ok"}}):
            res = engine.run_tool(name, {}, state or {"tainted": False}, io)
        return res, io

    def test_rule_skips_the_question(self):
        _, io = self.run_tool("notify")
        self.assertEqual(io.asked, ["notify"])
        rules.add("notify")
        res, io = self.run_tool("notify")
        self.assertEqual(io.asked, [])
        self.assertIn("result", res)
        self.assertTrue(any("règle" in i for i in io.infos))

    def test_tainted_still_asks(self):
        rules.add("notify")
        _, io = self.run_tool("notify", {"tainted": True})
        self.assertEqual(io.asked, ["notify"])


class MemoryKinds(unittest.TestCase):
    def test_kind_dedupe_and_prompt(self):
        a, new = memory.add("Navanime habite à Abidjan", "lieu")
        self.assertTrue(new)
        b, new = memory.add("navanime habite à abidjan", "")
        self.assertFalse(new)
        self.assertEqual(a, b)
        self.assertIn("[lieu]", memory.as_prompt("où j'habite"))
        self.assertEqual(memory.facts()[0]["kind"], "lieu")
        self.assertEqual(memory._kind("n'importe quoi"), "autre")

    def test_relevance_above_threshold(self):
        for i in range(50):
            memory.add(f"fait numéro{i} sur sujet{i} banal", "")
        memory.add("Candidature SONEC envoyée en septembre", "travail")
        for i in range(12):
            memory.add(f"remplissage récent{i} truc{i}", "")
        p = memory.as_prompt("où en est la candidature SONEC ?")
        self.assertIn("SONEC", p)
        self.assertIn("remplissage récent11", p)
        self.assertNotIn("fait numéro3 ", p)
        self.assertLess(len(p.splitlines()), 45)


class Habits(unittest.TestCase):
    def setUp(self):
        habits.clear()

    def at(self, day, hour=8, minute=0):
        return time.mktime((2026, 3, day, hour, minute, 0, 0, 0, -1))

    def test_threshold_and_weekly_limit(self):
        q = "Donne-moi la météo d'Abidjan ce matin"
        for d in (1, 2):
            habits.record(q, self.at(d))
        self.assertIsNone(habits.tip(q, self.at(3, 8, 20)))             # 2 jours seulement
        habits.record(q, self.at(3))
        self.assertIn("programme-le", habits.tip(q, self.at(4, 8, 30)))
        self.assertIsNone(habits.tip(q, self.at(5, 8, 30)))            # une fois par semaine
        self.assertIsNotNone(habits.tip(q, self.at(12, 8, 30)))

    def test_time_gap_and_commands(self):
        q = "Résumé des actualités sécurité"
        for d in (1, 2, 3):
            habits.record(q, self.at(d, 8))
        self.assertIsNone(habits.tip(q, self.at(4, 15)))                # autre heure
        habits.record("/aide", self.at(4))
        self.assertEqual(sum(n for _, n in habits.summary(self.at(5))), 3)

    def test_retention_disable_and_clear(self):
        habits.record("vieille demande ancienne", self.at(1))
        habits.record("autre demande récente", self.at(1) + 50 * 86400)
        self.assertEqual(len(habits.similar_asks("vieille demande ancienne", self.at(1))), 0)
        with mock.patch.dict(os.environ, {"SYLVIE_HABITS": "0"}):
            habits.record("demande désactivée ici", self.at(1))
            self.assertIsNone(habits.tip("demande désactivée ici"))
        habits.clear()
        self.assertEqual(habits.summary(self.at(1) + 50 * 86400), [])


if __name__ == "__main__":
    unittest.main()


class WebAlways(unittest.TestCase):
    def test_always_creates_scoped_rule_only_when_allowed(self):
        import threading, server
        for r in rules.rules():
            rules.remove(r["id"])
        def req(risk, tainted, hint):
            r = {"aid": "a", "name": "read_url", "args": {}, "risk": risk, "tainted": tainted, "event": threading.Event(), "ok": False, "hint": hint}
            server.Web.approvals["a"] = r
            return server.resolve_approval("a", True, "", True)
        req("confirm", True, "wikipedia.org")
        req("sensitive", False, "wikipedia.org")
        req("confirm", False, None)
        self.assertEqual(rules.rules(), [])
        req("confirm", False, "wikipedia.org")
        self.assertEqual([r["value"] for r in rules.rules()], ["wikipedia.org"])
