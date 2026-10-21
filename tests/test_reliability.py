"""Tests hors-ligne : historique, sauvegarde, diagnostic, scripts shell."""
import json, os, sqlite3, subprocess, sys, tempfile, time, unittest, zipfile
from unittest import mock

TMP = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backup, diagnostic, history, paths  # noqa: E402

HERE = paths.HERE


def setUpModule():
    os.environ.update(SYLVIE_DB=os.path.join(TMP, "r.db"), SYLVIE_BACKUP_DIR=os.path.join(TMP, "bk"), HOME=TMP, GEMINI_API_KEY="gk")
    os.environ.pop("SYLVIE_PERSIST", None)


def convo(n=4):
    out = []
    for i in range(n):
        out += [{"role": "user", "parts": [{"text": f"q{i}"}]}, {"role": "model", "parts": [{"text": f"r{i}"}], "_provider": "gemini-flash"}]
    return out


class History(unittest.TestCase):
    def test_round_trip_staleness_and_validation(self):
        history.save("t", convo())
        self.assertEqual(len(history.load("t")), 8)
        self.assertEqual(history.load("t", now=time.time() + 13 * 3600), [])       # périmée après 12 h
        self.assertEqual(history.load("inconnue"), [])
        with history._db() as con:
            con.execute("INSERT OR REPLACE INTO conversations VALUES ('bad', ?, ?)", (time.time(), json.dumps([{"role": "root", "parts": []}])))
            con.execute("INSERT OR REPLACE INTO conversations VALUES ('bad2', ?, ?)", (time.time(), "pas du json"))
        self.assertEqual(history.load("bad"), [])
        self.assertEqual(history.load("bad2"), [])
        history.clear("t")
        self.assertEqual(history.load("t"), [])

    def test_disabled_and_size_cap(self):
        with mock.patch.dict(os.environ, {"SYLVIE_PERSIST": "0"}):
            history.save("off", convo())
            self.assertEqual(history.load("off"), [])
        big = [{"role": "user", "parts": [{"text": "x" * 120_000}]}, {"role": "model", "parts": [{"text": "ok"}]}] * 10
        history.save("big", big)
        with history._db() as con:
            size = len(con.execute("SELECT data FROM conversations WHERE name='big'").fetchone()[0])
        self.assertLessEqual(size, history.MAX_BYTES + 130_000)
        c = history.load("big")
        self.assertTrue(c == [] or c[0]["role"] == "user")


class Backup(unittest.TestCase):
    def setUp(self):
        con = sqlite3.connect(paths.db())
        con.execute("CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, text TEXT)")
        con.execute("DELETE FROM facts")
        con.execute("INSERT INTO facts (text) VALUES ('J''aime le café sans sucre')")
        con.commit()
        con.close()

    def test_create_contains_db_and_never_secrets(self):
        p = backup.create()
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
        self.assertIn("sylvie.db", names)
        for secret in (".env", ".token", "actions.log"):
            self.assertNotIn(secret, names)
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")

    def test_rotation_and_auto(self):
        for i in range(10):
            backup.create(now=time.time() - (10 - i) * 86400 * 30)
        self.assertEqual(len(backup.list_backups()), backup.KEEP)
        self.assertIsNone(backup.auto())              # une sauvegarde de moins de 7 jours existe : rien à faire

    def test_restore_replaces_content_and_rejects_bad_names(self):
        p = backup.create()
        con = sqlite3.connect(paths.db())
        con.execute("DELETE FROM facts")
        con.execute("INSERT INTO facts (text) VALUES ('autre chose')")
        con.commit()
        con.close()
        backup.restore(os.path.basename(p))
        con = sqlite3.connect(paths.db())
        rows = [r[0] for r in con.execute("SELECT text FROM facts")]
        con.close()
        self.assertEqual(rows, ["J'aime le café sans sucre"])
        for bad in ("../../etc/passwd", "inconnue.zip", "sylvie-x.zip"):
            with self.assertRaises(ValueError):
                backup.restore(bad)
        corrupt = os.path.join(paths.backup_dir(), "sylvie-00000000-000000.zip")
        with zipfile.ZipFile(corrupt, "w") as z:
            z.writestr("sylvie.db", b"pas une base")
        with self.assertRaises(Exception):
            backup.restore("sylvie-00000000-000000.zip")


class Diagnostic(unittest.TestCase):
    def test_runs_offline_and_reports_problems_with_hints(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), mock.patch.object(diagnostic.shutil, "which", return_value=None):
            items = diagnostic.run(online=False)
        by = {label: (lvl, hint) for lvl, label, hint in items}
        self.assertEqual(by["clé GEMINI_API_KEY absente"][0], diagnostic.BAD)
        self.assertIn("pkg install termux-api", by["outils Termux:API manquants"][1])
        self.assertRegex(diagnostic.summary(items), r"\d+ ok, \d+ à surveiller, \d+ à corriger")

    def test_network_failure_is_reported_not_raised(self):
        with mock.patch("requests.get", side_effect=OSError("hors ligne")):
            items = diagnostic.run(online=True)
        self.assertTrue([i for i in items if i[0] == diagnostic.BAD and "Gemini" in i[1]])


class Scripts(unittest.TestCase):
    def test_shell_scripts_parse(self):
        for s in ("update.sh", "install.sh"):
            r = subprocess.run(["bash", "-n", os.path.join(HERE, s)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_update_script_rolls_back_on_failing_tests(self):
        with open(os.path.join(HERE, "update.sh")) as f:
            text = f.read()
        self.assertIn("git reset -q --hard \"$OLD\"", text)
        self.assertIn("unittest discover", text)
        with open(os.path.join(HERE, "install.sh")) as f:
            boot = f.read()
        self.assertIn(".termux/boot/sylvie-serve", boot)


if __name__ == "__main__":
    unittest.main()
