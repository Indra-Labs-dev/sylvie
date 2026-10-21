"""Tests hors-ligne : fichiers, agenda, vision, dépôt de fichiers."""
import base64, io, json, os, sys, tempfile, time, unittest, zipfile
from unittest import mock

TMP = tempfile.mkdtemp()
ROOT = os.path.join(TMP, "docs")
OUT = os.path.join(TMP, "dehors")
os.makedirs(ROOT)
os.makedirs(OUT)
os.environ.update(SYLVIE_DB=os.path.join(TMP, "p.db"), GEMINI_API_KEY="gk", SYLVIE_FILES_ROOTS=ROOT, HOME=TMP)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agenda, camera, engine, files, scheduler, tools  # noqa: E402


def setUpModule():   # d'autres modules de test changent l'environnement à l'import : on le réaffirme
    os.environ.update(SYLVIE_DB=os.path.join(TMP, "p.db"), SYLVIE_FILES_ROOTS=ROOT, HOME=TMP)


def put(name, content, mode="w"):
    path = os.path.join(ROOT, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(content)
    return path


def zipped(name, members):
    path = os.path.join(ROOT, name)
    with zipfile.ZipFile(path, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return path


class Rec(engine.IO):
    def __init__(self):
        self.images = []

    def image(self, d): self.images.append(d)


class Guard(unittest.TestCase):
    def test_paths_cannot_escape(self):
        secret = os.path.join(OUT, "secret.txt")
        with open(secret, "w") as f:
            f.write("x")
        os.symlink(OUT, os.path.join(ROOT, "lien"))
        put(".cache/x.txt", "caché")
        for bad in ("../dehors/secret.txt", secret, "lien/secret.txt", "~/../etc/passwd", ".cache/x.txt", "", "a\x00b",
                    os.path.join(files.HERE, "providers.json"), "/etc/passwd"):
            with self.assertRaises(files.FileError, msg=bad):
                files.resolve(bad)
        put("ok.txt", "ok")
        self.assertEqual(files.resolve("ok.txt"), os.path.realpath(os.path.join(ROOT, "ok.txt")))

    def test_tool_levels(self):
        r = tools.REGISTRY
        self.assertEqual(r["files_find"]["risk"], "auto")
        for n in ("files_read", "files_summarize", "files_write", "camera_look", "image_analyze", "agenda_add"):
            self.assertEqual(r[n]["risk"], "confirm", n)
        self.assertEqual(r["files_write"]["risk_fn"]({"overwrite": True}), "sensitive")
        self.assertEqual(r["files_write"]["risk_fn"]({}), "confirm")
        for n in ("files_read", "camera_look", "agenda_import", "csv_stats"):
            self.assertTrue(r[n]["external"], n)
        self.assertNotIn("files_delete", r)   # aucune suppression possible


class Formats(unittest.TestCase):
    def test_text_docx_pptx_xlsx(self):
        put("n.md", "# Titre\nbonjour")
        self.assertIn("bonjour", files.files_read("n.md")["texte"])
        docx = zipped("a.docx", {"word/document.xml": "<w:document><w:p><w:r><w:t>Contrat &amp; annexes</w:t></w:r></w:p><w:p><w:r><w:t>Article 2</w:t></w:r></w:p></w:document>"})
        t = files.files_read(docx)["texte"]
        self.assertIn("Contrat & annexes", t)
        self.assertIn("Article 2", t)
        pptx = zipped("a.pptx", {"ppt/slides/slide2.xml": "<a:t>Deux</a:t>", "ppt/slides/slide1.xml": "<a:t>Un</a:t>"})
        t = files.files_read(pptx)["texte"]
        self.assertLess(t.index("Un"), t.index("Deux"))
        xlsx = zipped("a.xlsx", {
            "xl/workbook.xml": '<workbook><sheets><sheet name="Ventes" sheetId="1"/></sheets></workbook>',
            "xl/sharedStrings.xml": "<sst><si><t>Produit</t></si><si><t>Riz</t></si></sst>",
            "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>12</v></c></row>'
                                         '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2"><v>7.5</v></c></row></sheetData></worksheet>'})
        t = files.files_read(xlsx)["texte"]
        self.assertIn("Feuille Ventes", t)
        self.assertIn("Produit\t12", t)
        self.assertIn("Riz\t\t7.5", t)

    def test_unreadable_and_oversized(self):
        put("bad.docx", "pas un zip")
        with self.assertRaises(files.FileError):
            files.files_read("bad.docx")
        put("x.bin", b"\x00\x01\x02" * 10, "wb")
        with self.assertRaises(files.FileError):
            files.files_read("x.bin")
        put("p.png", b"\x89PNG", "wb")
        with self.assertRaises(files.FileError):
            files.files_read("p.png")
        put("long.txt", "abc " * 20000)
        r = files.files_read("long.txt", max_chars=1000)
        self.assertEqual(len(r["texte"]), 1000)
        self.assertIn("offset=1000", r["suite"])

    def test_csv_stats_handles_semicolons_and_decimal_commas(self):
        put("v.csv", "produit;prix;ville\nriz;12,5;Abidjan\nmil;7;Abidjan\nriz;10;Bouaké\n")
        s = files.csv_stats("v.csv")
        self.assertEqual(s["lignes"], 3)
        self.assertEqual(s["stats"]["prix"]["max"], 12.5)
        self.assertAlmostEqual(s["stats"]["prix"]["somme"], 29.5)
        self.assertEqual(s["stats"]["ville"]["valeurs_distinctes"], 2)
        with self.assertRaises(files.FileError):
            files.csv_stats("v.csv", "zzz")

    def test_find_grep_list(self):
        put("rapports/Bilan été 2026.txt", "chiffre d'affaires : 42\nautre ligne")
        r = files.files_find("bilan ete")
        self.assertEqual(len(r["résultats"]), 1)
        self.assertEqual(files.files_find("introuvable-xyz"), "aucun fichier trouvé")
        g = files.files_grep("CHIFFRE d'affaires")
        self.assertEqual(g[0]["ligne"], 1)
        self.assertTrue(files.files_list("rapports")["éléments"])
        self.assertEqual(files.files_list()["dossiers_autorisés"], [os.path.realpath(ROOT)])


class Write(unittest.TestCase):
    def test_write_rules(self):
        r = files.files_write("notes/idee.md", "# idée")
        self.assertTrue(os.path.exists(r["fichier"]))
        with self.assertRaises(files.FileError):
            files.files_write("notes/idee.md", "autre")           # existe : pas d'écrasement implicite
        files.files_write("notes/idee.md", "autre", overwrite=True)
        for bad in ("run.sh", "x.exe", "a.py", ".env"):
            with self.assertRaises(files.FileError, msg=bad):
                files.files_write(bad, "x")
        with self.assertRaises(files.FileError):
            files.files_write("gros.txt", "x" * 300000)

    def test_summarize_uses_private_provider_and_survives_outage(self):
        put("long2.txt", "mot " * 30000)   # 120 000 caractères : tronqué à 60 000, 3 parties
        calls = []

        def gen(*a, **k):
            calls.append(k)
            return {"content": {"parts": [{"text": "résumé"}]}}
        with mock.patch.object(files.llm, "generate", side_effect=gen):
            r = files.files_summarize("long2.txt", "l'essentiel")
        self.assertEqual(r["résumé"], "résumé")
        self.assertEqual(len(calls), 4)                              # 3 parties + fusion
        self.assertTrue(all(c["private"] is True for c in calls))    # un document perso ne va jamais chez un fournisseur public
        with mock.patch.object(files.llm, "generate", side_effect=files.llm.NoProvider("panne")):
            self.assertIn("note", files.files_summarize("long2.txt"))


class Agenda(unittest.TestCase):
    def setUp(self):
        for e in agenda.events_between(0, 4e9):
            agenda.remove(e["id"])
        for j in scheduler.jobs():
            scheduler.remove(j["id"])

    def when(self, hours):
        return time.strftime(agenda.FMT, time.localtime(time.time() + hours * 3600))

    def test_add_conflict_reminder_and_remove(self):
        a = agenda.agenda_add("Réunion IAM", self.when(5), self.when(6), "Plateau 2")
        self.assertEqual(a["rappel"], "30 min avant")
        self.assertEqual(len([j for j in scheduler.jobs() if j["kind"] == "rappel"]), 1)
        b = agenda.agenda_add("Point rapide", self.when(5.5), place="")
        self.assertIn("Réunion IAM", b["conflits"][0])
        lst = agenda.agenda_list(2)
        self.assertEqual(len(lst), 2)
        self.assertEqual(agenda.agenda_list(1, "2001-01-01"), "aucun événement sur 1 jour(s)")
        self.assertEqual(len([j for j in scheduler.jobs() if j["kind"] == "rappel"]), 2)
        agenda.agenda_remove(a["id"])
        self.assertEqual(len([j for j in scheduler.jobs() if j["kind"] == "rappel"]), 1)   # le rappel part avec l'événement
        for bad in (dict(title="", start=self.when(1)), dict(title="x", start="demain"), dict(title="x", start=self.when(2), end=self.when(1))):
            with self.assertRaises(ValueError):
                agenda.agenda_add(**bad)

    def test_no_reminder_in_the_past_and_update(self):
        a = agenda.agenda_add("Imminent", self.when(0.2))
        self.assertEqual(a["rappel"], "aucun")
        b = agenda.agenda_add("Dentiste", self.when(30), self.when(31))
        u = agenda.agenda_update(b["id"], start=self.when(48))
        self.assertEqual(len(agenda.events_between(time.time() + 40 * 3600, time.time() + 60 * 3600)), 1)
        self.assertEqual(u["id"] != b["id"], True)

    def test_ics_round_trip_and_import_rules(self):
        t0 = time.time() + 86400
        agenda.agenda_add("Atelier ; sécurité, SOC", time.strftime(agenda.FMT, time.localtime(t0)), place="Salle B, étage 2", remind_minutes=0)
        path = agenda.agenda_export("sortie.ics")["fichier"]
        with open(path, newline="") as f:
            text = f.read()
        self.assertIn(r"SUMMARY:Atelier \; sécurité\, SOC", text)
        with self.assertRaises(ValueError):
            agenda.agenda_export("sortie.ics")        # pas d'écrasement
        for e in agenda.events_between(0, 4e9):
            agenda.remove(e["id"])
        self.assertEqual(agenda.agenda_import("sortie.ics")["importés"], 1)
        self.assertEqual(agenda.agenda_import("sortie.ics")["importés"], 0)   # pas de doublon
        ev = agenda.events_between(0, 4e9)[0]
        self.assertEqual((ev["title"], ev["place"]), ("Atelier ; sécurité, SOC", "Salle B, étage 2"))
        ics = ("BEGIN:VEVENT\r\nDTSTART:20300101T090000Z\r\nDTEND:20300101T100000Z\r\nSUMMARY:Utc\r\nEND:VEVENT\r\n"
               "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20300102\r\nSUMMARY:Journée\r\nEND:VEVENT\r\n"
               "BEGIN:VEVENT\r\nDTSTART:20300103T090000\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:Récurrent\r\nEND:VEVENT\r\n")
        evs, skipped = agenda.parse_ics(ics)
        self.assertEqual((len(evs), skipped), (2, 1))
        self.assertEqual(evs[1]["end"] - evs[1]["start"], 86400)

    def test_phone_calendar_command_is_a_list_not_a_shell_string(self):
        a = agenda.agenda_add("Dîner; rm -rf /", self.when(3), remind_minutes=0)
        with mock.patch.object(agenda.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="Starting: Intent", stderr="")) as run:
            self.assertIn("Enregistrer", agenda.agenda_phone(a["id"]))
        cmd = run.call_args[0][0]
        self.assertIsInstance(cmd, list)
        self.assertIn("Dîner; rm -rf /", cmd)
        self.assertNotIn("shell", run.call_args[1])

    def test_briefing_and_tasks_can_read_agenda(self):
        self.assertIn("agenda_list", scheduler.BRIEFING_TOOLS)
        self.assertIn("agenda_list", scheduler.SAFE_DEFAULT)


class Vision(unittest.TestCase):
    def fake_photo(self, *cmd, **kw):
        with open(cmd[-1], "wb") as f:
            f.write(b"\xff\xd8" + b"x" * 5000)

    def test_camera_look_privacy_and_cleanup(self):
        io, seen = Rec(), {}

        def gen(contents, **k):
            seen.update(k)
            seen["parts"] = contents[0]["parts"]
            return {"content": {"parts": [{"text": "Une affiche : « Soldes »"}]}}
        with mock.patch.object(camera.tools, "termux", side_effect=self.fake_photo), mock.patch.object(camera.llm, "generate", side_effect=gen), \
             mock.patch.object(camera, "_pil", return_value=(None, None)), mock.patch.object(engine._ctx, "io", io, create=True):
            r = camera.camera_look("que dit l'affiche ?", "front")
        self.assertIn("Soldes", r["analyse"])
        self.assertEqual((seen["role"], seen["private"]), ("vision", True))     # jamais vers un fournisseur public
        self.assertIn("inlineData", seen["parts"][0])
        self.assertEqual(len(io.images), 1)                                     # l'utilisateur voit ce que Sylvie a vu
        self.assertFalse(os.path.exists(os.path.join(camera.HERE, ".capture.jpg")))

    def test_camera_failure_leaves_nothing_behind(self):
        with mock.patch.object(camera.tools, "termux", side_effect=RuntimeError("permission")):
            with self.assertRaises(RuntimeError):
                camera.camera_look()
        self.assertFalse(os.path.exists(os.path.join(camera.HERE, ".capture.jpg")))

    def test_image_analyze_is_confined(self):
        with self.assertRaises(files.FileError):
            camera.image_analyze("../dehors/x.png")
        put("t.txt", "x")
        with self.assertRaises(RuntimeError):
            camera.image_analyze("t.txt")
        put("photo.png", b"\x89PNG" + b"0" * 3000, "wb")
        with mock.patch.object(camera.llm, "generate", return_value={"content": {"parts": [{"text": "un chat"}]}}), \
             mock.patch.object(camera, "_pil", return_value=(None, None)):
            self.assertEqual(camera.image_analyze("photo.png")["analyse"], "un chat")

    def test_providers_declare_vision_for_private_gemini_only(self):
        import router
        for p in router.providers():
            self.assertEqual("vision" in p.get("roles", []), p["trust"] == "private" and p["kind"] == "gemini", p["id"])


if __name__ == "__main__":
    unittest.main()
