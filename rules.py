"""Règles permanentes : tu autorises une fois un GENRE d'action, au lieu de confirmer chaque fois.

Garde-fous appliqués par le code :
  - seules les actions « confirm » d'une liste courte sont éligibles ; SMS, appels, photo, contacts, SMS lus, presse-papiers lu,
    suppression, tâches programmées et écrasement de fichier demandent toujours ton accord ;
  - une règle ne s'applique jamais juste après la lecture de contenu externe (web, SMS, fichier téléchargé) :
    c'est le moment où une instruction piégée essaierait d'en profiter ;
  - une règle est limitée (site, dossier) quand l'outil s'y prête, peut expirer, et se crée uniquement par toi (commande ou bouton),
    jamais par le modèle ;
  - chaque usage est écrit dans le journal.
"""
import contextlib, ipaddress, os, sqlite3, time
from urllib.parse import urlparse

import files, paths

MAX_RULES = 30
# outil -> (argument à limiter, type de limite) ou None pour « tout usage de cet outil »
RULEABLE = {
    "read_url": ("url", "host"), "open_url": ("url", "host"),
    "files_read": ("path", "dir"), "files_summarize": ("path", "dir"), "files_grep": ("path", "dir"), "csv_stats": ("path", "dir"),
    "files_open": ("path", "dir"), "files_write": ("path", "dir"),
    "agenda_add": None, "agenda_update": None, "notify": None, "vibrate": None, "torch": None, "play_music": None,
    "clipboard_set": None, "location": None,
}


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(paths.db(), timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, arg TEXT, kind TEXT, "
                    "value TEXT, created REAL, expires REAL)")
        with con:
            yield con
    finally:
        con.close()


def _host(url):
    h = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower().strip(".")
    if not h or "." not in h:
        raise ValueError("site invalide (ex : wikipedia.org)")
    try:
        ipaddress.ip_address(h)
    except ValueError:
        return h
    raise ValueError("une adresse IP ne peut pas faire l'objet d'une règle")


def suggest(tool, args):
    """Valeur de règle proposée pour cet appel (ou '' si l'outil n'a pas de limite), ou None si l'outil n'est pas éligible."""
    if tool not in RULEABLE:
        return None
    spec = RULEABLE[tool]
    if not spec:
        return ""
    arg, kind = spec
    try:
        if kind == "host":
            return _host(str(args.get(arg, "")))
        real = files.resolve(args.get(arg, ""))
        return real if os.path.isdir(real) else os.path.dirname(real)
    except (ValueError, files.FileError):
        return None


def add(tool, value="", days=0, now=None):
    if tool not in RULEABLE:
        raise ValueError(f"aucune règle possible pour « {tool} » (éligibles : {', '.join(sorted(RULEABLE))})")
    spec, value = RULEABLE[tool], str(value or "").strip()
    arg = kind = None
    if spec:
        arg, kind = spec
        if not value:
            raise ValueError("il faut limiter cette règle : " + ("un site, ex wikipedia.org" if kind == "host" else "un dossier autorisé"))
        try:
            value = _host(value) if kind == "host" else files.resolve(value)
        except files.FileError as e:
            raise ValueError(str(e))
        if kind == "dir" and not os.path.isdir(value):
            raise ValueError("ce n'est pas un dossier")
    elif value:
        raise ValueError("cet outil ne prend pas de limite")
    if len(rules()) >= MAX_RULES:
        raise ValueError(f"{MAX_RULES} règles maximum")
    now = now or time.time()
    with _db() as con:
        dup = con.execute("SELECT id FROM rules WHERE tool=? AND IFNULL(value,'')=?", (tool, value)).fetchone()
        if dup:
            return dup[0]
        return con.execute("INSERT INTO rules (tool, arg, kind, value, created, expires) VALUES (?,?,?,?,?,?)",
                           (tool, arg, kind, value or None, now, now + days * 86400 if days else None)).lastrowid


def rules(now=None):
    now = now or time.time()
    with _db() as con:
        con.execute("DELETE FROM rules WHERE expires IS NOT NULL AND expires < ?", (now,))
        rows = con.execute("SELECT id, tool, arg, kind, value, created, expires FROM rules ORDER BY id").fetchall()
    return [{"id": r[0], "tool": r[1], "arg": r[2], "kind": r[3], "value": r[4] or "", "created": r[5], "expires": r[6]} for r in rows]


def remove(rule_id):
    with _db() as con:
        return con.execute("DELETE FROM rules WHERE id=?", (int(rule_id),)).rowcount > 0


def describe(r):
    scope = {"host": f"sur {r['value']}", "dir": f"dans {r['value']}"}.get(r["kind"], "")
    until = time.strftime(" jusqu'au %d/%m", time.localtime(r["expires"])) if r["expires"] else ""
    return f"{r['tool']} {scope}{until}".replace("  ", " ")


def allows(tool, args, risk, tainted, now=None):
    """La règle qui autorise cet appel sans question, ou None."""
    if risk != "confirm" or tainted or tool not in RULEABLE:
        return None
    if tool == "files_write" and args.get("overwrite"):
        return None
    for r in rules(now):
        if r["tool"] != tool:
            continue
        if not r["kind"]:
            return r
        try:
            if r["kind"] == "host":
                host = _host(str(args.get(r["arg"], "")))
                if host == r["value"] or host.endswith("." + r["value"]):
                    return r
            else:
                real = files.resolve(args.get(r["arg"], ""))
                if files._inside(real, r["value"]):
                    return r
        except (ValueError, files.FileError):
            continue
    return None
