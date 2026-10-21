"""Tests hors-ligne : notifications, veille sécurité, candidatures."""
import json, os, sys, tempfile, time, unittest
from unittest import mock

TMP = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(SYLVIE_DB=os.path.join(TMP, "b3.db"), HOME=TMP, GEMINI_API_KEY="gk")
import candidatures, engine, scheduler, tools, veille  # noqa: E402


def setUpModule():
    os.environ["SYLVIE_DB"] = os.path.join(TMP, "b3.db")


def jobs_titles():
    return sorted(j["title"] for j in scheduler.jobs())


class Notifications(unittest.TestCase):
    def test_filters_and_limits(self):
        data = [{"packageName": "com.termux.api", "title": "x", "content": "y"}, {"packageName": "com.x", "group": "sylvie", "title": "z"},
                {"packageName": "com.whatsapp", "title": "Awa", "content": "a" * 400}, {"packageName": "com.google.gm", "title": "Mail", "content": "CELPAID"}]
        with mock.patch.object(tools, "termux", return_value=data):
            r = tools.notifications_read()
            self.assertEqual([n["appli"] for n in r], ["com.whatsapp", "com.google.gm"])
            self.assertEqual(len(r[0]["texte"]), 250)
            self.assertEqual(len(tools.notifications_read(app="gm")), 1)
            self.assertEqual(tools.notifications_read(app="rien"), "aucune notification")
        with mock.patch.object(tools, "termux", return_value={}):
            with self.assertRaises(RuntimeError):
                tools.notifications_read()

    def test_is_confirm_and_external(self):
        t = tools.REGISTRY["notifications_read"]
        self.assertEqual((t["risk"], t["external"]), ("confirm", True))


class Veille(unittest.TestCase):
    def test_add_list_remove(self):
        r = veille.veille_add("Huawei", "les deux")
        self.assertEqual([x["source"] for x in r], ["KEV", "CVE"])
        self.assertEqual(len(veille.veille_list()), 2)
        job = [j for j in scheduler.jobs() if j["title"] == "Veille KEV : Huawei"][0]
        self.assertEqual(job["extra"]["api"], "cisa_kev")
        self.assertEqual(job["extra"]["params"], {"q": "Huawei"})
        for x in r:
            veille.veille_remove(x["id"])
        self.assertEqual(veille.veille_list(), "aucune veille en cours")

    def test_validation_and_scope(self):
        for bad in [("", "kev"), ("a;rm -rf", "kev"), ("x" * 50, "kev"), ("Wazuh", "twitter")]:
            with self.assertRaises(ValueError, msg=bad):
                veille.veille_add(*bad)
        jid, _ = scheduler.add("rappel", "Autre", "x", {"type": "once", "at": time.time() + 999})
        with self.assertRaises(ValueError):
            veille.veille_remove(jid)      # ne supprime que les veilles


class Candidatures(unittest.TestCase):
    def setUp(self):
        for j in list(scheduler.jobs()):
            scheduler.remove(j["id"])

    def test_deadline_reminder_and_followup(self):
        now = time.time()
        dl = time.strftime("%Y-%m-%d", time.localtime(now + 10 * 86400))
        r = candidatures.candidature_add("SONEC", "Ingénieur réseau", deadline=dl, now=now)
        self.assertEqual(r["rappels"], 1)
        self.assertEqual(jobs_titles(), ["Candidature : SONEC"])
        u = candidatures.candidature_update(r["id"], status="envoyée", now=now)
        self.assertEqual(u["statut"], "envoyée")
        self.assertEqual(jobs_titles(), ["Relance : SONEC"])        # l'ancien rappel est remplacé
        candidatures.candidature_update(r["id"], status="refus", notes="pas retenu", now=now)
        self.assertEqual(jobs_titles(), [])
        self.assertIn("pas retenu", candidatures.candidature_list("refus")[0]["notes"])

    def test_past_deadline_and_remove(self):
        now = time.time()
        r = candidatures.candidature_add("CELPAID", deadline="2020-01-01", now=now)
        self.assertEqual(r["rappels"], 0)
        r2 = candidatures.candidature_add("GNOC", status="envoyee", now=now)
        self.assertEqual(r2["rappels"], 1)
        candidatures.candidature_remove(r2["id"])
        self.assertEqual(jobs_titles(), [])

    def test_validation(self):
        for call in [lambda: candidatures.candidature_add(""), lambda: candidatures.candidature_add("X", status="bof"),
                     lambda: candidatures.candidature_add("X", deadline="12/03/2026"), lambda: candidatures.candidature_update(9999),
                     lambda: candidatures.candidature_remove(9999)]:
            with self.assertRaises(ValueError):
                call()

    def test_tools_registered_and_not_in_unattended_default(self):
        for n in ("candidature_add", "veille_add", "notifications_read"):
            self.assertIn(n, tools.REGISTRY)
        self.assertNotIn("notifications_read", scheduler.SAFE_DEFAULT)


if __name__ == "__main__":
    unittest.main()
