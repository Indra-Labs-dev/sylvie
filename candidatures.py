"""Suivi des candidatures : organisme, poste, statut, date limite, relance automatique.

Rappels créés avec le planificateur (sans jeton d'IA) : 2 jours avant la date limite, et une relance 7 jours après l'envoi.
"""
import contextlib, json, sqlite3, time

import paths, scheduler, tools

STATUTS = {"a_envoyer": "à envoyer", "envoyee": "envoyée", "entretien": "entretien", "offre": "offre", "refus": "refus", "abandon": "abandon"}
CLOSED = {"offre", "refus", "abandon"}
FOLLOW_DAYS = 7
MAX_ROWS = 200
FMT = "%Y-%m-%d"


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(paths.db(), timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, org TEXT NOT NULL, role TEXT, status TEXT, "
                    "deadline REAL, applied REAL, notes TEXT, jobs TEXT, created REAL)")
        with con:
            yield con
    finally:
        con.close()


def _day(s, what):
    try:
        return time.mktime(time.strptime(str(s).strip(), FMT)) + 9 * 3600   # rappels à 9 h
    except ValueError:
        raise ValueError(f"{what} : format AAAA-MM-JJ")


def _status(s):
    s = str(s or "a_envoyer").lower().strip().replace("é", "e").replace(" ", "_")
    s = {"a_envoyer": "a_envoyer", "envoye": "envoyee", "envoyee": "envoyee", "refuse": "refus"}.get(s, s)
    if s not in STATUTS:
        raise ValueError("statut : " + ", ".join(STATUTS))
    return s


def _reminders(org, role, status, deadline, applied, now):
    ids, label = [], f"{org}" + (f" ({role})" if role else "")
    if status in CLOSED:
        return ids
    if deadline and status == "a_envoyer" and deadline - 2 * 86400 > now:
        ids.append(scheduler.add("rappel", f"Candidature : {org}"[:80], f"Date limite dans 2 jours : {label}", {"type": "once", "at": deadline - 2 * 86400}, now=now)[0])
    if status == "envoyee" and applied and applied + FOLLOW_DAYS * 86400 > now:
        ids.append(scheduler.add("rappel", f"Relance : {org}"[:80], f"Pense à relancer {label} (envoyée il y a {FOLLOW_DAYS} jours)", {"type": "once", "at": applied + FOLLOW_DAYS * 86400}, now=now)[0])
    return ids


def _clear(jobs):
    for j in json.loads(jobs or "[]"):
        scheduler.remove(j)


def _row(r):
    return {"id": r[0], "org": r[1], "role": r[2] or "", "status": r[3], "deadline": r[4], "applied": r[5], "notes": r[6] or "", "jobs": r[7]}


COLS = "id, org, role, status, deadline, applied, notes, jobs"


def get(aid):
    with _db() as con:
        r = con.execute(f"SELECT {COLS} FROM applications WHERE id=?", (int(aid),)).fetchone()
    return _row(r) if r else None


def _show(a):
    d = lambda t: time.strftime(FMT, time.localtime(t)) if t else ""
    return {"id": a["id"], "organisme": a["org"], **({"poste": a["role"]} if a["role"] else {}), "statut": STATUTS[a["status"]],
            **({"date_limite": d(a["deadline"])} if a["deadline"] else {}), **({"envoyee_le": d(a["applied"])} if a["applied"] else {}),
            **({"notes": a["notes"][:150]} if a["notes"] else {})}


def candidature_add(org, role="", status="a_envoyer", deadline="", applied="", notes="", now=None):
    now = now or time.time()
    org = str(org).strip()[:80]
    if not org:
        raise ValueError("organisme obligatoire")
    st = _status(status)
    dl = _day(deadline, "date limite") if str(deadline).strip() else None
    ap = _day(applied, "date d'envoi") if str(applied).strip() else (now if st == "envoyee" else None)
    with _db() as con:
        if con.execute("SELECT count(*) FROM applications").fetchone()[0] >= MAX_ROWS:
            raise ValueError("trop de candidatures (200)")
    ids = _reminders(org, str(role)[:80], st, dl, ap, now)
    with _db() as con:
        aid = con.execute("INSERT INTO applications (org, role, status, deadline, applied, notes, jobs, created) VALUES (?,?,?,?,?,?,?,?)",
                          (org, str(role)[:80], st, dl, ap, str(notes)[:500], json.dumps(ids), now)).lastrowid
    return {"id": aid, "rappels": len(ids)}


def candidature_update(app_id, status="", deadline="", applied="", notes="", now=None):
    now = now or time.time()
    a = get(app_id)
    if not a:
        raise ValueError(f"pas de candidature n° {app_id}")
    st = _status(status) if str(status).strip() else a["status"]
    dl = _day(deadline, "date limite") if str(deadline).strip() else a["deadline"]
    ap = _day(applied, "date d'envoi") if str(applied).strip() else (a["applied"] or (now if st == "envoyee" else None))
    _clear(a["jobs"])
    ids = _reminders(a["org"], a["role"], st, dl, ap, now)
    with _db() as con:
        con.execute("UPDATE applications SET status=?, deadline=?, applied=?, notes=?, jobs=? WHERE id=?",
                    (st, dl, ap, (a["notes"] + (" | " if a["notes"] and notes else "") + str(notes))[:500], json.dumps(ids), a["id"]))
    return _show(get(a["id"]))


def candidature_remove(app_id):
    a = get(app_id)
    if not a:
        raise ValueError(f"pas de candidature n° {app_id}")
    _clear(a["jobs"])
    with _db() as con:
        con.execute("DELETE FROM applications WHERE id=?", (a["id"],))
    return "supprimée"


def candidature_list(status=""):
    with _db() as con:
        rows = [_row(r) for r in con.execute(f"SELECT {COLS} FROM applications ORDER BY id")]
    if str(status).strip():
        rows = [a for a in rows if a["status"] == _status(status)]
    return [_show(a) for a in rows] or "aucune candidature"


S = tools.S
tools.tool(candidature_list, "auto", "Liste les candidatures suivies (concours, emplois), filtrables par statut", {"status": (S, "a_envoyer | envoyee | entretien | offre | refus | abandon")}, required=[])
tools.tool(candidature_add, "confirm", "Suit une candidature ; crée un rappel 2 jours avant la date limite, et une relance 7 jours après l'envoi",
           {"org": (S, "organisme (ex : CELPAID, SONEC)"), "role": (S, "poste"), "status": (S, "a_envoyer (défaut) | envoyee | entretien | offre | refus | abandon"),
            "deadline": (S, "date limite AAAA-MM-JJ"), "applied": (S, "date d'envoi AAAA-MM-JJ"), "notes": (S, "notes")}, required=["org"], writes=True)
tools.tool(candidature_update, "confirm", "Met à jour une candidature (statut, dates, notes) et recalcule ses rappels",
           {"app_id": (tools.I, "numéro"), "status": (S, "nouveau statut"), "deadline": (S, "AAAA-MM-JJ"), "applied": (S, "AAAA-MM-JJ"), "notes": (S, "notes à ajouter")},
           required=["app_id"], writes=True)
tools.tool(candidature_remove, "confirm", "Supprime une candidature et ses rappels", {"app_id": (tools.I, "numéro")}, writes=True)
