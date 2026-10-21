"""Mémoire persistante de Sylvie (SQLite local, jamais commité) : faits classés, sans doublons, injectés selon la question."""
import contextlib, os, re, sqlite3, time, unicodedata

DB = os.environ.get("SYLVIE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sylvie.db"))
MAX_IN_PROMPT = 60      # au-delà de 40 souvenirs, on n'injecte que les plus pertinents
ALWAYS_ALL = 40
KINDS = {"preference": "préférence", "personne": "personne", "projet": "projet", "lieu": "lieu", "travail": "travail", "autre": "autre"}


def _norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", str(s).lower()) if unicodedata.category(c) != "Mn")


def _words(s):
    return {w for w in re.findall(r"[a-z0-9]+", _norm(s)) if len(w) > 2}


def _kind(k):
    return _norm(k).strip() if _norm(k).strip() in KINDS else "autre"


def _db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, "
                "created TEXT NOT NULL)")
    with contextlib.suppress(sqlite3.OperationalError):
        con.execute("ALTER TABLE facts ADD COLUMN kind TEXT DEFAULT ''")
    return con


def add(text, kind=""):
    """Enregistre un fait. Un fait quasi identique à un souvenir existant n'est pas ajouté deux fois."""
    text = re.sub(r"\s+", " ", str(text)).strip()[:500]
    if not text:
        raise ValueError("fait vide")
    new = _words(text)
    with contextlib.closing(_db()) as con, con:
        for fid, old in con.execute("SELECT id, text FROM facts"):
            ow = _words(old)
            if old.lower() == text.lower() or (new and ow and len(new & ow) / len(new | ow) >= 0.85):
                return fid, False
        cur = con.execute("INSERT INTO facts (text, created, kind) VALUES (?, ?, ?)", (text, time.strftime("%F %T"), _kind(kind) if kind else ""))
        return cur.lastrowid, True


def facts(limit=MAX_IN_PROMPT):
    with contextlib.closing(_db()) as con, con:
        rows = con.execute("SELECT id, text, created, IFNULL(kind, '') FROM facts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": i, "text": t, "created": c, "kind": k} for i, t, c, k in reversed(rows)]


def all_facts(limit=MAX_IN_PROMPT):
    return [(f["id"], f["text"], f["created"]) for f in facts(limit)]


def search(query, limit=10):
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    if not words:
        return all_facts(limit)
    with contextlib.closing(_db()) as con, con:
        rows = con.execute("SELECT id, text, created FROM facts").fetchall()
    scored = [(sum(w in r[1].lower() for w in words), r) for r in rows]
    return [r for s, r in sorted(scored, key=lambda x: (-x[0], -x[1][0])) if s > 0][:limit]


def delete(fact_id):
    with contextlib.closing(_db()) as con, con:
        return con.execute("DELETE FROM facts WHERE id = ?", (int(fact_id),)).rowcount > 0


def count():
    with contextlib.closing(_db()) as con, con:
        return con.execute("SELECT count(*) FROM facts").fetchone()[0]


def as_prompt(question=""):
    total = count()
    if not total:
        return "Tu ne sais encore rien de particulier sur l'utilisateur."
    if total <= ALWAYS_ALL:
        chosen, head = facts(), "Ce que tu as retenu sur l'utilisateur (n° : fait) :"
    else:   # beaucoup de souvenirs : les plus proches de la question + les plus récents
        qw = _words(question)
        every = facts(10_000)
        ranked = sorted(every, key=lambda f: (-len(qw & _words(f["text"])), -f["id"]))
        picked = {f["id"]: f for f in ranked[:30] if qw & _words(f["text"])}
        picked.update({f["id"]: f for f in every[-10:]})
        chosen = [picked[i] for i in sorted(picked)]
        head = f"Parmi {total} souvenirs sur l'utilisateur, ceux qui comptent ici (n° : fait ; recall pour chercher les autres) :"
    return head + "\n" + "\n".join(f"{f['id']} : " + (f"[{KINDS.get(f['kind'], f['kind'])}] " if f["kind"] else "") + f["text"] for f in chosen)
