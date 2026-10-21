"""Agenda de Sylvie : événements locaux, rappels automatiques, import/export .ics, ajout à l'agenda du téléphone.

Termux ne peut pas lire l'agenda Android directement. Sylvie tient donc son propre agenda (SQLite) ;
« agenda_phone » ouvre l'agenda du téléphone avec l'événement prérempli, tu touches Enregistrer ;
« agenda_import » lit un fichier .ics exporté depuis ton agenda (Google Agenda : Paramètres › Importer/exporter).
"""
import calendar as _cal, contextlib, os, re, sqlite3, subprocess, time

import scheduler, tools

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_EVENTS = 500
FMT = "%Y-%m-%d %H:%M"


def _path():
    return os.environ.get("SYLVIE_DB", os.path.join(HERE, "sylvie.db"))


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(_path(), timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, start REAL NOT NULL, "
                    "end REAL NOT NULL, place TEXT, notes TEXT, job_id INTEGER, source TEXT, created REAL)")
        with con:
            yield con
    finally:
        con.close()


def parse(s, what="date"):
    try:
        return time.mktime(time.strptime(str(s).strip(), FMT))
    except ValueError:
        raise ValueError(f"{what} : format AAAA-MM-JJ HH:MM attendu")


def fmt(ts):
    return scheduler.fmt(ts)


def span(ev):
    same = time.localtime(ev["start"])[:3] == time.localtime(ev["end"])[:3]
    return f"{fmt(ev['start'])}–{time.strftime('%H:%M', time.localtime(ev['end']))}" if same else f"{fmt(ev['start'])} → {fmt(ev['end'])}"


def _row(r):
    return {"id": r[0], "title": r[1], "start": r[2], "end": r[3], "place": r[4] or "", "notes": r[5] or "", "job_id": r[6], "source": r[7] or ""}


COLS = "id, title, start, end, place, notes, job_id, source"


def events_between(a, b):
    with _db() as con:
        rows = con.execute(f"SELECT {COLS} FROM events WHERE end > ? AND start < ? ORDER BY start", (a, b)).fetchall()
    return [_row(r) for r in rows]


def get(eid):
    with _db() as con:
        r = con.execute(f"SELECT {COLS} FROM events WHERE id=?", (int(eid),)).fetchone()
    return _row(r) if r else None


def _remind(title, start, place, minutes, now):
    if not minutes or start - minutes * 60 <= now:
        return None
    text = f"{title} à {time.strftime('%H:%M', time.localtime(start))}" + (f" ({place})" if place else "")
    jid, _ = scheduler.add("rappel", f"Agenda : {title}"[:80], text, {"type": "once", "at": start - minutes * 60}, now=now)
    return jid


def add(title, start, end, place="", notes="", remind=30, source="", now=None):
    now = now or time.time()
    title = str(title).strip()[:120]
    if not title:
        raise ValueError("titre obligatoire")
    if end <= start:
        raise ValueError("la fin doit être après le début")
    with _db() as con:
        if con.execute("SELECT count(*) FROM events").fetchone()[0] >= MAX_EVENTS:
            raise ValueError("agenda plein (500 événements)")
    conflicts = [e for e in events_between(start, end)]
    jid = _remind(title, start, place, int(remind or 0), now)
    with _db() as con:
        eid = con.execute("INSERT INTO events (title, start, end, place, notes, job_id, source, created) VALUES (?,?,?,?,?,?,?,?)",
                          (title, start, end, str(place)[:120], str(notes)[:500], jid, source, now)).lastrowid
    return eid, conflicts, jid


def remove(eid):
    ev = get(eid)
    if not ev:
        return False
    if ev["job_id"]:
        scheduler.remove(ev["job_id"])
    with _db() as con:
        con.execute("DELETE FROM events WHERE id=?", (int(eid),))
    return True


# ------------------------------------------------------------------ outils

def agenda_list(days=7, start_date=""):
    days = max(1, min(int(days or 7), 90))
    a = parse(start_date + " 00:00") if str(start_date).strip() else time.mktime(time.localtime(time.time())[:3] + (0, 0, 0, 0, 0, -1))
    evs = events_between(a, a + days * 86400)
    return [{"id": e["id"], "quand": span(e), "titre": e["title"], **({"lieu": e["place"]} if e["place"] else {}),
             **({"notes": e["notes"][:120]} if e["notes"] else {})} for e in evs] or f"aucun événement sur {days} jour(s)"


def agenda_add(title, start, end="", place="", notes="", remind_minutes=30):
    s = parse(start, "start")
    e = parse(end, "end") if str(end).strip() else s + 3600
    eid, conflicts, jid = add(title, s, e, place, notes, remind_minutes, source="sylvie")
    out = {"id": eid, "quand": span({"start": s, "end": e}), "rappel": f"{int(remind_minutes)} min avant" if jid else "aucun"}
    if conflicts:
        out["conflits"] = [f"{c['title']} ({span(c)})" for c in conflicts]
    return out


def agenda_update(event_id, title="", start="", end="", place="", notes="", remind_minutes=-1):
    ev = get(event_id)
    if not ev:
        raise ValueError(f"pas d'événement n° {event_id}")
    s = parse(start, "start") if str(start).strip() else ev["start"]
    e = parse(end, "end") if str(end).strip() else (ev["end"] if not str(start).strip() else s + (ev["end"] - ev["start"]))
    remove(event_id)
    eid, conflicts, _ = add(title or ev["title"], s, e, place or ev["place"], notes or ev["notes"],
                            30 if int(remind_minutes) < 0 and ev["job_id"] else max(0, int(remind_minutes)), source=ev["source"])
    return {"id": eid, "quand": span({"start": s, "end": e}), **({"conflits": [c["title"] for c in conflicts]} if conflicts else {})}


def agenda_remove(event_id):
    if not remove(event_id):
        raise ValueError(f"pas d'événement n° {event_id}")
    return "supprimé (et son rappel)"


def agenda_phone(event_id):
    ev = get(event_id)
    if not ev:
        raise ValueError(f"pas d'événement n° {event_id}")
    cmd = ["am", "start", "-a", "android.intent.action.INSERT", "-t", "vnd.android.cursor.dir/event", "--es", "title", ev["title"],
           "--el", "beginTime", str(int(ev["start"] * 1000)), "--el", "endTime", str(int(ev["end"] * 1000))]
    if ev["place"]:
        cmd += ["--es", "eventLocation", ev["place"]]
    if ev["notes"]:
        cmd += ["--es", "description", ev["notes"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        raise RuntimeError("commande « am » introuvable : disponible seulement dans Termux")
    if r.returncode != 0 or "Error" in r.stdout + r.stderr:
        raise RuntimeError((r.stderr or r.stdout).strip()[:150] or "l'agenda du téléphone ne s'est pas ouvert")
    return "agenda du téléphone ouvert avec l'événement prérempli : touche Enregistrer"


# ---- .ics

def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _ics_value(s):
    return s.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _ics_time(v):
    """YYYYMMDD ou YYYYMMDDTHHMMSS[Z] -> (timestamp local, jour entier)."""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?", v.strip())
    if not m:
        raise ValueError(v)
    y, mo, d, h, mi, sec, z = m.groups()
    parts = (int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0))
    if z:
        return float(_cal.timegm(parts + (0, 0, 0))), False
    return time.mktime(parts + (0, 0, -1)), h is None


def parse_ics(text):
    events, skipped = [], 0
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", _unfold(text), re.S):
        props = {}
        for line in block.strip().splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                props.setdefault(k.split(";")[0].upper(), v)
        if "RRULE" in props or "DTSTART" not in props:
            skipped += 1   # événements récurrents : non gérés
            continue
        try:
            s, allday = _ics_time(props["DTSTART"])
            e = _ics_time(props["DTEND"])[0] if "DTEND" in props else (s + 86400 if allday else s + 3600)
        except ValueError:
            skipped += 1
            continue
        if allday and e <= s:
            e = s + 86400
        events.append({"title": _ics_value(props.get("SUMMARY", "(sans titre)")), "start": s, "end": max(e, s + 60),
                       "place": _ics_value(props.get("LOCATION", "")), "notes": _ics_value(props.get("DESCRIPTION", ""))})
    return events, skipped


def agenda_import(path):
    import files
    real = files.resolve(path)
    if not real.lower().endswith(".ics") or os.path.getsize(real) > 2_000_000:
        raise ValueError("fichier .ics de moins de 2 Mo attendu")
    with open(real, encoding="utf-8", errors="replace") as f:
        events, skipped = parse_ics(f.read())
    now, added = time.time(), 0
    with _db() as con:
        known = {(r[0], int(r[1])) for r in con.execute("SELECT title, start FROM events")}
    for ev in sorted(events, key=lambda e: e["start"])[:200]:
        if ev["end"] < now or (ev["title"][:120], int(ev["start"])) in known:
            continue
        try:
            add(ev["title"], ev["start"], ev["end"], ev["place"], ev["notes"], remind=0, source="import")
            added += 1
        except ValueError:
            break
    return {"importés": added, "déjà présents ou passés": len(events) - added, "récurrents ignorés": skipped}


def _ics_escape(s):
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def agenda_export(path="agenda.ics"):
    import files
    real = files.resolve(path)
    if not real.lower().endswith(".ics"):
        raise ValueError("le nom doit finir par .ics")
    if os.path.exists(real):
        raise ValueError("le fichier existe déjà : choisis un autre nom")
    evs = events_between(time.time(), time.time() + 366 * 86400)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "Z"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Sylvie//FR"]
    for e in evs:
        lines += ["BEGIN:VEVENT", f"UID:sylvie-{e['id']}-{int(e['start'])}@sylvie", f"DTSTAMP:{stamp}",
                  "DTSTART:" + time.strftime("%Y%m%dT%H%M%S", time.localtime(e["start"])),
                  "DTEND:" + time.strftime("%Y%m%dT%H%M%S", time.localtime(e["end"])), "SUMMARY:" + _ics_escape(e["title"])]
        if e["place"]:
            lines.append("LOCATION:" + _ics_escape(e["place"]))
        if e["notes"]:
            lines.append("DESCRIPTION:" + _ics_escape(e["notes"]))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return {"fichier": real, "événements": len(evs)}


S, I = tools.S, tools.I
tools.tool(agenda_list, "auto", "Liste les événements de l'agenda de Sylvie (aujourd'hui et les N prochains jours)",
           {"days": (I, "nombre de jours, 7 par défaut"), "start_date": (S, "AAAA-MM-JJ (aujourd'hui par défaut)")}, required=[])
tools.tool(agenda_add, "confirm", "Ajoute un événement à l'agenda (avec rappel automatique) et signale les conflits d'horaire",
           {"title": (S, "titre"), "start": (S, "AAAA-MM-JJ HH:MM"), "end": (S, "AAAA-MM-JJ HH:MM (1 h par défaut)"), "place": (S, "lieu"),
            "notes": (S, "notes"), "remind_minutes": (I, "rappel N minutes avant, 30 par défaut, 0 = aucun")},
           required=["title", "start"], writes=True)
tools.tool(agenda_update, "confirm", "Modifie un événement de l'agenda",
           {"event_id": (I, "numéro"), "title": (S, "nouveau titre"), "start": (S, "nouveau début AAAA-MM-JJ HH:MM"), "end": (S, "nouvelle fin"),
            "place": (S, "lieu"), "notes": (S, "notes"), "remind_minutes": (I, "rappel avant, -1 = inchangé")}, required=["event_id"], writes=True)
tools.tool(agenda_remove, "confirm", "Supprime un événement de l'agenda et son rappel", {"event_id": (I, "numéro")}, writes=True)
tools.tool(agenda_phone, "confirm", "Ouvre l'agenda Android avec l'événement prérempli (l'utilisateur touche Enregistrer)", {"event_id": (I, "numéro")})
tools.tool(agenda_import, "confirm", "Importe les événements à venir d'un fichier .ics (export d'un agenda)", {"path": (S, "chemin du .ics")},
           writes=True, external=True)
tools.tool(agenda_export, "confirm", "Exporte les événements à venir dans un fichier .ics (importable dans Google Agenda)",
           {"path": (S, "chemin, agenda.ics par défaut")}, required=[], writes=True)
