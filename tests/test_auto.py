"""Tests hors-ligne : moteur, planificateur, objectifs."""
import json, os, sys, tempfile, threading, time, unittest
from unittest import mock

os.environ["SYLVIE_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["GEMINI_API_KEY"] = "gk"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apis, engine, planner, scheduler, tools  # noqa: E402


def reply(text=None, calls=()):
    parts = ([{"text": text}] if text else []) + [{"functionCall": {"name": n, "args": a}} for n, a in calls]
    return {"content": {"parts": parts}, "provider": "gemini-flash"}


class Rec(engine.IO):
    def __init__(self, approve=False, plan_ok=True):
        self.said, self.errors, self.asked, self.denials, self._a, self._p = [], [], [], [], approve, plan_ok

    def say(self, t): self.said.append(t)
    def error(self, m): self.errors.append(m)
    def approve(self, name, args, risk, tainted): self.asked.append((name, risk, tainted)); return self._a
    def approve_plan(self, goal, plan): return self._p
    def denied(self, name, args): self.denials.append(name)


class Calendar(unittest.TestCase):
    def test_days_and_schedule(self):
        self.assertEqual(scheduler.parse_days("lun-ven"), [0, 1, 2, 3, 4])
        self.assertEqual(scheduler.parse_days("sam,dim"), [5, 6])
        self.assertEqual(scheduler.parse_days("ven-lun"), [0, 4, 5, 6])
        with self.assertRaises(ValueError):
            scheduler.parse_days("jeudiX")
        with self.assertRaises(ValueError):
            scheduler.parse_hm("25:00")
        now = time.mktime((2026, 10, 21, 17, 30, 0, 0, 0, -1))   # mercredi
        nxt = scheduler.next_after({"type": "daily", "hm": [7, 30], "days": [0, 1, 2, 3, 4]}, now)
        self.assertEqual(time.localtime(nxt)[:5], (2026, 10, 22, 7, 30))
        nxt = scheduler.next_after({"type": "daily", "hm": [7, 30], "days": [5]}, now)   # samedi
        self.assertEqual(time.localtime(nxt)[:3], (2026, 10, 24))
        self.assertIsNone(scheduler.next_after({"type": "once", "at": now - 5}, now))
        for bad in (dict(), dict(at="2026-10-22 08:00", daily="07:00"), dict(at="2020-01-01 08:00"), dict(at="demain")):
            with self.assertRaises(ValueError):
                scheduler.build_schedule(now=now, **bad)


class Jobs(unittest.TestCase):
    def setUp(self):
        for j in scheduler.jobs():
            scheduler.remove(j["id"])

    def test_schedule_add_rules(self):
        soon = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3600))
        r = scheduler.schedule_add("rappel", "Boire de l'eau", "Pense à boire", at=soon)
        self.assertEqual(scheduler.get(r["id"])["kind"], "rappel")
        for kw in (dict(kind="tache", title="x", text="y", daily="08:00", tools_list="sms_send"),
                   dict(kind="tache", title="x", text="y", daily="08:00", tools_list="schedule_add"),
                   dict(kind="tache", title="x", text="y", daily="08:00", tools_list="nope"),
                   dict(kind="tache", title="x", text="y", every_minutes=5),
                   dict(kind="surveillance", title="x", api="nope", every_minutes=30),
                   dict(kind="surveillance", title="x", api="cisa_kev", at=soon),
                   dict(kind="rappel", title="x", daily="08:00", every_minutes=5),
                   dict(kind="bidule", title="x", daily="08:00")):
            with self.assertRaises(ValueError, msg=str(kw)):
                scheduler.schedule_add(**kw)
        b = scheduler.schedule_add("briefing", "Briefing", daily="07:30", weekdays="lun-ven")
        self.assertEqual(scheduler.get(b["id"])["extra"]["tools"], scheduler.BRIEFING_TOOLS)
        self.assertIn("météo", scheduler.get(b["id"])["text"])

    def test_reminder_runs_once_and_is_claimed_once(self):
        now = time.time()
        jid, _ = scheduler.add("rappel", "RDV", "Rendez-vous à 9h", {"type": "once", "at": now + 1}, now=now)
        sent = []
        with mock.patch.object(scheduler, "notify", side_effect=lambda t, x, urgent=False: sent.append((t, x))), \
             mock.patch.object(scheduler.gate, "log"):
            s = scheduler.Scheduler()
            self.assertEqual(s.poll(now + 0.5, wait=True), 0)
            self.assertEqual(s.poll(now + 2, wait=True), 1)
            self.assertEqual(s.poll(now + 3, wait=True), 0)   # déjà réservé : pas de double envoi
        self.assertEqual(sent, [("Rappel : RDV", "Rendez-vous à 9h")])
        self.assertFalse(scheduler.get(jid)["enabled"])

    def test_missed_recurring_is_skipped_but_rescheduled(self):
        now = time.time()
        jid, _ = scheduler.add("rappel", "Vieux", "x", {"type": "every", "minutes": 60}, now=now - 20 * 3600)
        with mock.patch.object(scheduler, "notify") as n, mock.patch.object(scheduler.gate, "log"):
            self.assertEqual(scheduler.Scheduler().poll(now, wait=True), 0)
        n.assert_not_called()
        self.assertGreater(scheduler.get(jid)["next_run"], now)

    def test_watch_notifies_only_on_change(self):
        jid, _ = scheduler.add("surveillance", "KEV", "", {"type": "every", "minutes": 30},
                               {"api": "cisa_kev", "params": {"q": "huawei"}})
        outs = iter(["A", "A", "B"])
        with mock.patch.object(apis, "call", side_effect=lambda *a, **k: {"api": "cisa_kev", "resultat": next(outs)}), \
             mock.patch.object(scheduler, "notify") as n, mock.patch.object(scheduler.gate, "log"):
            for _ in range(3):
                scheduler.run_job(scheduler.get(jid))
        self.assertEqual(n.call_count, 1)
        self.assertIn("changement", scheduler.get(jid)["last_result"])

    def test_task_uses_only_listed_tools_and_queues_the_rest(self):
        jid, _ = scheduler.add("tache", "Heure", "dis l'heure", {"type": "daily", "hm": [8, 0], "days": list(range(7))},
                               {"tools": ["get_time"]})
        seq = iter([reply(calls=[("sms_send", {"number": "+2250102030405", "text": "hack"})]),
                    reply(calls=[("get_time", {})]), reply("Il est bien l'heure.")])
        sent = []
        with mock.patch.object(engine.llm, "generate", side_effect=lambda *a, **k: next(seq)), \
             mock.patch.object(scheduler, "notify", side_effect=lambda t, x, urgent=False: sent.append((t, x))), \
             mock.patch.object(scheduler.gate, "log"), mock.patch.object(engine.gate, "log"), \
             mock.patch.dict(tools.REGISTRY["sms_send"], {"fn": mock.Mock(side_effect=AssertionError("SMS envoyé !"))}):
            out = scheduler.run_job(scheduler.get(jid))
        self.assertEqual(out, "Il est bien l'heure.")
        self.assertEqual([p["tool"] for p in scheduler.pending_list()], ["sms_send"])   # gardé pour ton accord, non exécuté
        self.assertTrue(any("attend ton accord" in t for t, _ in sent))

    def test_unattended_never_approves_after_external_content(self):
        io = scheduler.UnattendedIO("t", ["web_search", "remember"])
        self.assertTrue(io.approve("remember", {}, "confirm", False))
        self.assertFalse(io.approve("remember", {}, "confirm", True))
        self.assertFalse(io.approve("sms_send", {}, "sensitive", False))


class Engine(unittest.TestCase):
    def test_allowed_list_cancel_and_budget(self):
        io = Rec()
        st = {"tainted": False, "allowed": {"get_time"}}
        with mock.patch.object(engine.gate, "log"):
            self.assertIn("non autorisé", engine.run_tool("calculate", {"expression": "1+1"}, st, io)["error"])
            self.assertEqual(io.denials, ["calculate"])
            ev = threading.Event(); ev.set()
            self.assertIn("arrêt", engine.run_tool("get_time", {}, {**st, "cancel": ev}, io)["error"])
        b = engine.Budget(2)
        b.take(); b.take()
        with self.assertRaises(RuntimeError):
            b.take()

    def test_run_stops_on_cancel_and_restores_history(self):
        ev = threading.Event(); ev.set()
        contents = []
        with mock.patch.object(engine.llm, "generate", side_effect=AssertionError("ne doit pas appeler le modèle")):
            self.assertIsNone(engine.run("salut", contents, Rec(), cancel=ev))
        self.assertEqual(contents, [])

    def test_private_false_hides_memory(self):
        with mock.patch.object(engine.memory, "as_prompt", return_value="SECRET-SOUVENIR"):
            self.assertIn("SECRET-SOUVENIR", engine.system_prompt("x", use_memory=True))
            self.assertNotIn("SECRET-SOUVENIR", engine.system_prompt("x", use_memory=False))


PLAN = {"steps": [
    {"title": "Chercher", "type": "recherche", "goal": "trouve X", "tools": ["web_search", "sms_send"]},
    {"title": "Écrire", "type": "redaction", "goal": "rédige", "tools": ["web_search"]}]}


class Goals(unittest.TestCase):
    def test_normalize(self):
        p = planner.normalize(PLAN)
        self.assertEqual(p["steps"][0]["tools"], ["web_search"])     # sms_send n'est pas du type recherche
        self.assertEqual(p["steps"][1]["tools"], [])                 # la rédaction n'a jamais d'outils
        self.assertFalse(p["private"])
        q = planner.normalize({"steps": [{"title": "Lire", "type": "telephone", "goal": "g", "tools": ["sms_list"]}, {"type": "???", "goal": "x"}]})
        self.assertTrue(q["private"])
        self.assertEqual(q["steps"][1]["type"], "recherche")
        with self.assertRaises(ValueError):
            planner.normalize({"steps": []})
        with self.assertRaises(ValueError):
            planner.parse_plan("pas de json")
        self.assertEqual(len(planner.normalize({"steps": [{"goal": str(i)} for i in range(20)]})["steps"]), planner.MAX_STEPS)

    def test_goal_runs_plan_then_steps(self):
        seq = iter([reply("```json\n" + json.dumps(PLAN) + "\n```"),      # plan
                    reply(calls=[("calculate", {"expression": "2+2"})]),    # hors liste de l'étape recherche
                    reply("X vaut 4"),                                      # étape 1
                    reply("Réponse finale : X vaut 4.")])                   # étape 2
        calls = []
        io = Rec(plan_ok=True)
        with mock.patch.object(engine.llm, "generate", side_effect=lambda *a, **k: calls.append(k) or next(seq)), \
             mock.patch.object(planner.llm, "generate", side_effect=lambda *a, **k: calls.append(k) or next(seq)), \
             mock.patch.object(engine.gate, "log"), mock.patch.object(planner.gate, "log"):
            final = planner.run_goal("trouve X", io)
        self.assertEqual(final, "Réponse finale : X vaut 4.")
        self.assertEqual(io.denials, ["calculate"])
        self.assertFalse(calls[2]["private"])          # plan sans outil privé : fournisseurs publics possibles
        self.assertEqual(planner.goals(1)[0]["status"], "termine")

    def test_plan_refused_runs_nothing(self):
        seq = iter([reply(json.dumps(PLAN))])
        with mock.patch.object(planner.llm, "generate", side_effect=lambda *a, **k: next(seq)), \
             mock.patch.object(planner.gate, "log"):
            self.assertIn("refusé", planner.run_goal("trouve X", Rec(plan_ok=False)))
        self.assertEqual(planner.goals(1)[0]["status"], "refuse")

    def test_stop_and_plan_tools_prevalidated(self):
        ev = threading.Event()
        seq = iter([reply(json.dumps(PLAN)), reply("résultat étape 1")])

        def gen(*a, **k):
            r = next(seq)
            ev.set()   # l'utilisateur arrête après la première étape
            return r
        with mock.patch.object(engine.llm, "generate", side_effect=gen), mock.patch.object(planner.llm, "generate", side_effect=gen), \
             mock.patch.object(engine.gate, "log"), mock.patch.object(planner.gate, "log"):
            out = planner.run_goal("trouve X", Rec(), cancel=ev)
        self.assertIn("arrêté", out)
        io = planner.PlanIO(Rec(approve=False), ["read_url"])
        self.assertTrue(io.approve("read_url", {}, "confirm", False))
        self.assertFalse(io.approve("read_url", {}, "confirm", True))     # contenu externe lu : on redemande
        self.assertFalse(io.approve("sms_send", {}, "sensitive", False))  # jamais pré-validé

    def test_bad_plan_is_reported(self):
        with mock.patch.object(planner.llm, "generate", return_value=reply("désolé")), mock.patch.object(planner.gate, "log"):
            self.assertIn("pas pu établir", planner.run_goal("x", Rec()))


if __name__ == "__main__":
    unittest.main()
