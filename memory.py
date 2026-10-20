"""Mémoire persistante de Sylvie (SQLite local, jamais commité)."""
import os, re, sqlite3, time

DB = os.environ.get("SYLVIE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sylvie.db"))
MAX_IN_PROMPT = 60


def _db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, "
                "created TEXT NOT NULL)")
    return con


def add(text):
    text = re.sub(r"\s+", " ", str(text)).strip()[:500]
    if not text:
        raise ValueError("fait vide")
    with _db() as con:
        row = con.execute("SELECT id FROM facts WHERE lower(text) = lower(?)", (text,)).fetchone()
        if row:
            return row[0], False
        cur = con.execute("INSERT INTO facts (text, created) VALUES (?, ?)", (text, time.strftime("%F %T")))
        return cur.lastrowid, True


def all_facts(limit=MAX_IN_PROMPT):
    with _db() as con:
        rows = con.execute("SELECT id, text, created FROM facts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return list(reversed(rows))


def search(query, limit=10):
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    if not words:
        return all_facts(limit)
    with _db() as con:
        rows = con.execute("SELECT id, text, created FROM facts").fetchall()
    scored = [(sum(w in r[1].lower() for w in words), r) for r in rows]
    return [r for s, r in sorted(scored, key=lambda x: (-x[0], -x[1][0])) if s > 0][:limit]


def delete(fact_id):
    with _db() as con:
        return con.execute("DELETE FROM facts WHERE id = ?", (int(fact_id),)).rowcount > 0


def count():
    with _db() as con:
        return con.execute("SELECT count(*) FROM facts").fetchone()[0]


def as_prompt():
    facts = all_facts()
    if not facts:
        return "Tu ne sais encore rien de particulier sur l'utilisateur."
    return "Ce que tu as retenu sur l'utilisateur (n° : fait) :\n" + "\n".join(f"{i} : {t}" for i, t, _ in facts)
