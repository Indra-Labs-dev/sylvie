"""Conversation conservée entre deux lancements (locale, dans sylvie.db, périmée après quelques heures)."""
import contextlib, json, os, sqlite3, time

import engine, paths

MAX_BYTES = 400_000


def enabled():
    return os.environ.get("SYLVIE_PERSIST", "1") != "0"


def max_age():
    try:
        return float(os.environ.get("SYLVIE_PERSIST_HOURS", "12")) * 3600
    except ValueError:
        return 12 * 3600


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(paths.db(), timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS conversations (name TEXT PRIMARY KEY, ts REAL, data TEXT)")
        with con:
            yield con
    finally:
        con.close()


def _valid(c):
    return (isinstance(c, list) and all(isinstance(m, dict) and m.get("role") in ("user", "model") and isinstance(m.get("parts"), list) for m in c))


def save(name, contents):
    if not enabled():
        return
    keep = list(engine.trim(contents))
    data = json.dumps(keep, ensure_ascii=False)
    while len(data) > MAX_BYTES and len(keep) > 2:   # on abandonne les plus anciens messages
        keep = list(engine.trim(keep[2:]))
        data = json.dumps(keep, ensure_ascii=False)
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?)", (name, time.time(), data))


def load(name, now=None):
    if not enabled():
        return []
    with _db() as con:
        r = con.execute("SELECT ts, data FROM conversations WHERE name=?", (name,)).fetchone()
    if not r or (now or time.time()) - r[0] > max_age():
        return []
    try:
        c = json.loads(r[1])
    except json.JSONDecodeError:
        return []
    return c if _valid(c) and c and c[0]["role"] == "user" else []


def clear(name):
    with _db() as con:
        con.execute("DELETE FROM conversations WHERE name=?", (name,))
