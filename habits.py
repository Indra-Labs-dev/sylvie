"""Habitudes : repère les demandes que tu répètes à peu près à la même heure et propose de les programmer.

Tout reste sur le téléphone : seuls les mots significatifs (pas la phrase entière) et un court extrait pour l'affichage sont gardés,
pendant 45 jours. SYLVIE_HABITS=0 désactive ; /habitudes effacer supprime tout.
"""
import contextlib, json, os, re, sqlite3, time, unicodedata

import paths

WINDOW = 21 * 86400
KEEP = 45 * 86400
MIN_DAYS = 3          # même demande sur au moins 3 jours différents
MAX_GAP_MIN = 90      # à environ 1 h 30 près
SIMILAR = 0.7


def enabled():
    return os.environ.get("SYLVIE_HABITS", "1") != "0"


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(paths.db(), timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS asked (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, words TEXT, sample TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS tips (key TEXT PRIMARY KEY, ts REAL)")
        with con:
            yield con
    finally:
        con.close()


def words_of(q):
    s = "".join(c for c in unicodedata.normalize("NFD", q.lower()) if unicodedata.category(c) != "Mn")
    return sorted({w for w in re.findall(r"[a-z0-9]+", s) if len(w) >= 4})


def _sim(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def _minute(ts):
    t = time.localtime(ts)
    return t.tm_hour * 60 + t.tm_min


def record(q, now=None):
    if not enabled() or q.startswith("/"):
        return
    w = words_of(q)
    if len(w) < 2:
        return
    now = now or time.time()
    with _db() as con:
        con.execute("DELETE FROM asked WHERE ts < ?", (now - KEEP,))
        con.execute("INSERT INTO asked (ts, words, sample) VALUES (?,?,?)", (now, json.dumps(w), q.strip()[:70]))


def similar_asks(q, now=None):
    now = now or time.time()
    w = words_of(q)
    with _db() as con:
        rows = con.execute("SELECT ts, words, sample FROM asked WHERE ts > ?", (now - WINDOW,)).fetchall()
    return [(ts, sample) for ts, words, sample in rows if _sim(w, json.loads(words)) >= SIMILAR]


def tip(q, now=None):
    """Texte de suggestion si cette demande est une habitude (une seule fois par semaine et par habitude), sinon None."""
    if not enabled() or q.startswith("/"):
        return None
    now = now or time.time()
    hits = similar_asks(q, now)
    near = [ts for ts, _ in hits if min(abs(_minute(ts) - _minute(now)), 1440 - abs(_minute(ts) - _minute(now))) <= MAX_GAP_MIN]
    days = {time.strftime("%F", time.localtime(ts)) for ts in near}
    if len(days) < MIN_DAYS:
        return None
    key = " ".join(words_of(q))[:80]
    with _db() as con:
        last = con.execute("SELECT ts FROM tips WHERE key=?", (key,)).fetchone()
        if last and now - last[0] < 7 * 86400:
            return None
        con.execute("INSERT OR REPLACE INTO tips VALUES (?,?)", (key, now))
    t = time.localtime(now)
    return f"💡 Tu me demandes souvent ça vers {t.tm_hour}h ({len(days)} jours récents). Dis « programme-le » et je le planifie chaque jour."


def summary(now=None):
    """Demandes répétées, pour /habitudes : [(extrait, nombre)]."""
    now = now or time.time()
    with _db() as con:
        rows = con.execute("SELECT words, sample FROM asked WHERE ts > ?", (now - WINDOW,)).fetchall()
    groups = []
    for words, sample in rows:
        w = json.loads(words)
        for g in groups:
            if _sim(g["w"], w) >= SIMILAR:
                g["n"] += 1
                break
        else:
            groups.append({"w": w, "sample": sample, "n": 1})
    return sorted([(g["sample"], g["n"]) for g in groups if g["n"] >= 2], key=lambda x: -x[1])[:10]


def clear():
    with _db() as con:
        con.execute("DELETE FROM asked")
        con.execute("DELETE FROM tips")
