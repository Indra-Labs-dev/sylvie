"""Tests hors-ligne du moteur d'API : `python -m unittest discover -s tests` (aucun accès réseau)."""
import json, os, sys, tempfile, unittest
from unittest import mock

os.environ["SYLVIE_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apis  # noqa: E402


class Resp:
    def __init__(self, data, code=200, url="https://x/"):
        self.status_code, self.url = code, url
        self._b = (json.dumps(data) if not isinstance(data, str) else data).encode()

    def iter_content(self, n):
        yield self._b

    def close(self):
        pass


def fake(data, code=200, url=None):
    def _req(method, u, **kw):
        _req.calls.append((method, u, kw))
        return Resp(data(u, kw) if callable(data) else data, code, url or u)
    _req.calls = []
    return _req


class Catalog(unittest.TestCase):
    def test_catalogue_valide(self):
        self.assertGreaterEqual(len(apis.CATALOG), 50)
        for e in apis.CATALOG.values():
            self.assertTrue(e["url"].startswith("https://"), e["name"])
            self.assertTrue(e["desc"] and e["keywords"], e["name"])
            for k in e.get("form", {}).values():
                for p in __import__("re").findall(r"\{(\w+)\}", k):
                    self.assertIn(p, e["params"], f"{e['name']} : {{{p}}} sans paramètre")
            if "example" in e:
                apis.validate(e, e["example"])  # l'exemple respecte ses propres règles

    def test_pertinence(self):
        top = lambda q: [e["name"] for e in apis.relevant(q)]
        self.assertIn("meteo", top("quel temps fait-il demain ? va-t-il pleuvoir"))
        self.assertIn("change_monde", top("combien font 50 euros en fcfa"))
        self.assertIn("cve", top("détails de la faille cve"))
        self.assertIn("lieux_proches", top("j'ai faim je cherche un restaurant près d'ici"))
        self.assertEqual(apis.relevant("zzz qqq"), [])


class Appels(unittest.TestCase):
    def run_call(self, name, params, data, **kw):
        f = fake(data, **kw)
        with mock.patch("requests.request", f):
            return apis.call(name, params), f.calls

    def test_meteo_extraction(self):
        r, calls = self.run_call("meteo", {"latitude": 5.36, "longitude": -4.0},
                                 {"current": {"temperature_2m": 28.1}, "daily": {"temperature_2m_max": [31]}, "junk": 1})
        self.assertEqual(r["resultat"], {"current": {"temperature_2m": 28.1}, "daily": {"temperature_2m_max": [31]}})
        q = calls[0][2]["params"]
        self.assertEqual((q["latitude"], q["forecast_days"], q["timezone"]), (5.36, 3, "auto"))

    def test_params_valides(self):
        for bad in ({"latitude": 91, "longitude": 0}, {"latitude": "abc", "longitude": 0}, {"latitude": 1}, {"latitude": 1, "longitude": 2, "x": 3}):
            with self.assertRaises(apis.ApiError):
                apis.call("meteo", bad)
        with self.assertRaises(apis.ApiError):
            apis.call("cve", {"cveId": "CVE-2024-1; DROP"})
        with self.assertRaises(apis.ApiError):
            apis.call("inexistante", {})

    def test_ip_privee_refusee(self):
        for ip in ("192.168.1.10", "127.0.0.1", "10.0.0.5", "169.254.169.254"):
            with self.assertRaises(apis.ApiError):
                apis.call("ip_menaces", {"ip": ip})

    def test_chemin_et_encodage(self):
        r, calls = self.run_call("wikipedia", {"title": "Côte d'Ivoire/Test"}, {"title": "T", "extract": "x", "description": "d", "content_urls": {"desktop": {"page": "https://p"}}})
        self.assertEqual(calls[0][1], "https://fr.wikipedia.org/api/rest_v1/page/summary/C%C3%B4te%20d%27Ivoire%2FTest")
        self.assertEqual(r["resultat"]["page"], "https://p")

    def test_liste_racine_et_limite(self):
        data = [{"date": f"2026-0{i}-01", "localName": "A", "name": "B", "x": 1} for i in range(1, 6)]
        r, _ = self.run_call("jours_feries", {"year": 2026}, data)
        self.assertEqual(len(r["resultat"]), 5)
        self.assertEqual(set(r["resultat"][0]), {"date", "localName", "name"})

    def test_filtre_devises(self):
        r, _ = self.run_call("change_monde", {"base": "EUR", "symbols": "XOF,USD"},
                             {"base_code": "EUR", "time_last_update_utc": "t", "rates": {"XOF": 655.957, "USD": 1.1, "JPY": 160, "GBP": 0.8}})
        self.assertEqual(set(r["resultat"]["rates"]), {"XOF", "USD"})

    def test_recherche_locale_kev(self):
        vulns = [{"cveID": f"CVE-2026-{i:04d}", "vendorProject": "Huawei" if i % 3 == 0 else "Cisco", "product": "P", "vulnerabilityName": "n",
                  "dateAdded": f"2026-10-{i:02d}", "knownRansomwareCampaignUse": "Unknown", "requiredAction": "z"} for i in range(1, 20)]
        r, _ = self.run_call("cisa_kev", {"q": "huawei"}, {"vulnerabilities": vulns})
        res = r["resultat"]
        self.assertTrue(all(x["vendorProject"] == "Huawei" for x in res))
        self.assertEqual(res[0]["dateAdded"], "2026-10-18")  # plus récent d'abord
        r, _ = self.run_call("cisa_kev", {}, {"vulnerabilities": vulns})
        self.assertEqual(len(r["resultat"]), 6)

    def test_remoteok_ignore_mentions_legales(self):
        data = [{"legal": "ne pas lire"}, {"position": "SOC Analyst", "company": "A", "tags": ["security"], "url": "u"}, {"position": "Chef", "company": "B", "tags": ["food"], "url": "u"}]
        r, _ = self.run_call("emplois_remoteok", {"q": "security"}, data)
        self.assertEqual([x["position"] for x in r["resultat"]], ["SOC Analyst"])

    def test_osv_corps_et_version_optionnelle(self):
        r, calls = self.run_call("osv", {"package": "requests", "ecosystem": "PyPI"}, {"vulns": [{"id": "GHSA-1", "summary": "s", "aliases": ["CVE-1"], "modified": "m", "extra": 1}]})
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][2]["json"], {"package": {"name": "requests", "ecosystem": "PyPI"}})  # pas de "version" vide
        self.assertEqual(r["resultat"][0]["id"], "GHSA-1")

    def test_overpass_formulaire(self):
        _, calls = self.run_call("lieux_proches", {"latitude": 5.36, "longitude": -4.0, "type": "restaurant"}, {"elements": [{"lat": 5.3, "lon": -4.0, "tags": {"name": "Chez Tantie"}}]})
        self.assertIn("around:1000,5.36,-4.0", calls[0][2]["data"]["data"])
        self.assertIn("[amenity=restaurant]", calls[0][2]["data"]["data"])
        with self.assertRaises(apis.ApiError):
            apis.call("lieux_proches", {"latitude": 5, "longitude": -4, "type": "restaurant];out;//"})

    def test_lieux_via_center(self):
        r, _ = self.run_call("lieux_proches", {"latitude": 5.36, "longitude": -4.0, "type": "cafe"}, {"elements": [{"center": {"lat": 5.1, "lon": -4.1}, "tags": {"name": "Way"}}]})
        self.assertEqual((r["resultat"][0]["lat"], r["resultat"][0]["name"]), (5.1, "Way"))

    def test_html_nettoye_et_aucun_resultat(self):
        r, _ = self.run_call("wikipedia_recherche", {"q": "x"}, {"pages": [{"title": "T", "description": "d", "excerpt": "un <span class=\"searchmatch\">mot</span> &amp; co"}]})
        self.assertEqual(r["resultat"][0]["excerpt"], "un mot & co")
        r, _ = self.run_call("livres", {"q": "zzz"}, {"docs": []})
        self.assertEqual(r["resultat"], "aucun résultat")

    def test_erreurs_http(self):
        with mock.patch("requests.request", fake("introuvable", code=404)):
            with self.assertRaisesRegex(apis.ApiError, "aucun résultat"):
                apis.call("npm", {"package": "nexistepas"})
        with mock.patch("requests.request", fake("<html>Too many</html>", code=429)):
            with self.assertRaisesRegex(apis.ApiError, "HTTP 429"):
                apis.call("npm", {"package": "express"})

    def test_redirection_hors_hote_refusee(self):
        with mock.patch("requests.request", fake({"name": "x"}, url="https://evil.example/steal")):
            with self.assertRaisesRegex(apis.ApiError, "redirection"):
                apis.call("npm", {"package": "express"})
        with mock.patch("requests.request", fake({"ldhName": "example.com"}, url="https://rdap.verisign.com/com/v1/domain/example.com")):
            self.assertEqual(apis.call("rdap", {"domain": "example.com"})["resultat"]["ldhName"], "example.com")

    def test_troncature_et_texte_brut(self):
        r, _ = self.run_call("cve", {"cveId": "CVE-2024-3094"}, {"vulnerabilities": [{"cve": {"id": "CVE-2024-3094", "descriptions": [{"lang": "en", "value": "x" * 5000}]}}]})
        self.assertTrue(r["resultat"].endswith("…(tronqué)"))
        r, _ = self.run_call("ip_publique", {}, '{"ip":"41.0.0.1"}')
        self.assertEqual(r["resultat"], "41.0.0.1")

    def test_budget_du_jour(self):
        e = apis.CATALOG["npm"]
        old = e.get("calls_per_day")
        e["calls_per_day"] = 2
        try:
            with mock.patch("requests.request", fake({"name": "a", "version": "1"})):
                apis.call("npm", {"package": "a"})
                apis.call("npm", {"package": "b"})
                with self.assertRaisesRegex(apis.ApiError, "budget"):
                    apis.call("npm", {"package": "c"})
        finally:
            e["calls_per_day"] = old if old else 200

    def test_cadence_min_interval(self):
        slept = []
        e = apis.CATALOG["nominatim_recherche"]
        with mock.patch("requests.request", fake([{"display_name": "d", "lat": "1", "lon": "2", "type": "t"}])), mock.patch("time.sleep", slept.append):
            apis._last_call["nominatim_recherche"] = __import__("time").time()
            apis.call("nominatim_recherche", {"q": "a"})
        self.assertTrue(slept and 0 < slept[0] <= e["min_interval"])

    def test_ajout_local(self):
        import shutil
        p = os.path.join(apis.HERE, "apis.local.json")
        self.assertFalse(os.path.exists(p), "apis.local.json ne doit pas exister pendant les tests")
        try:
            with open(p, "w") as f:
                json.dump({"apis": [{"name": "mon_api", "desc": "test", "url": "https://exemple.org/{id}", "params": {"id": {"type": "integer"}}}], "remove": ["blague"]}, f)
            apis.load()
            self.assertIn("mon_api", apis.CATALOG)
            self.assertNotIn("blague", apis.CATALOG)
            with mock.patch("requests.request", fake({"ok": True})):
                self.assertEqual(apis.call("mon_api", {"id": 7})["resultat"], {"ok": True})
            with open(p, "w") as f:
                json.dump({"apis": [{"name": "mauvaise", "url": "http://exemple.org"}]}, f)
            with self.assertRaises(apis.ApiError):
                apis.load()
        finally:
            os.remove(p)
            apis.load()


if __name__ == "__main__":
    unittest.main()
