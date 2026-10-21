"""Planificateur : rappels, tâches récurrentes, briefing du matin, surveillances d'API.

Une tâche est approuvée UNE fois, à sa création (la boîte de confirmation montre tout ce qu'elle pourra faire).
Ensuite elle s'exécute sans toi, avec une règle fixe :
  - seuls les outils listés à la création sont utilisables ;
  - SMS, appels et tout outil hors liste sont refusés, et la demande est mise en attente d'approbation ;
  - après lecture de contenu externe (web, SMS), plus aucune action n'est autorisée.
Les rappels et les surveillances n'utilisent aucun modèle d'IA : zéro jeton.
"""
import contextlib, hashlib, json, os, sqlite3, threading, time

import apis, engine, gate, tools

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_JOBS = 40
LATE_MAX = 12 * 3600       # au-delà, une tâche récurrente manquée est sautée
MIN_EVERY = {"rappel": 1, "tache": 30, "briefing": 60, "surveillance": 10}
FORBIDDEN = {"schedule_add", "schedule_remove", "objectif"}   # une tâche ne se reprogramme pas elle-même
SAFE_DEFAULT = ["get_time", "calculate", "recall", "api_call", "api_info", "web_search", "schedule_list"]
LISTENERS = []             # fonctions appelées avec un dict {"type", ...} : interface web, journal

DAYS = {"lun": 0, "mar": 1, "mer": 2, "jeu": 3, "ven": 4, "sam": 5, "dim": 6, "lundi": 0, "mardi": 1, "mercredi": 2,
        "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6}
JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
MOIS = ["janv", "févr", "mars", "avr", "mai", "juin", "juil", "août", "sept", "oct", "nov", "déc"]


def _path():
    return os.environ.get("SYLVIE_DB", os.path.join(HERE, "sylvie.db"))


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(_path(), timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                    "title TEXT NOT NULL, text TEXT, schedule TEXT NOT NULL, next_run REAL, last_run REAL, "
                    "last_result TEXT, enabled INTEGER NOT NULL DEFAULT 1, created REAL, extra TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, source TEXT, "
                    "tool TEXT, args TEXT, risk TEXT, status TEXT NOT NULL DEFAULT 'attente')")
        with con:
            yield con
    finally:
        con.close()


# ------------------------------------------------------------------ calendrier

def fmt(ts):
    if not ts:
        return "-"
    t = time.localtime(ts)
    return f"{JOURS[t.tm_wday]} {t.tm_mday} {MOIS[t.tm_mon - 1]} {t.tm_hour:02d}:{t.tm_min:02d}"


def parse_hm(s):
    try:
        h, m = str(s).strip().replace("h", ":").split(":")[:2]
        h, m = int(h), int(m or 0)
    except ValueError:
        raise ValueError(f"heure invalide : {s!r} (attendu HH:MM)")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"heure invalide : {s!r}")
    return h, m


def parse_days(s):
    """« lun,mer », « lun-ven », « semaine », « weekend », « tous » -> liste triée de 0..6."""
    s = (s or "tous").strip().lower()
    if s in ("tous", "tous les jours", "chaque jour", ""):
        return list(range(7))
    if s in ("semaine", "jours ouvrés", "ouvres", "ouvrés"):
        return list(range(5))
    if s in ("weekend", "week-end"):
        return [5, 6]
    out = set()
    for part in s.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in DAYS or b not in DAYS:
                raise ValueError(f"jours invalides : {s!r}")
            i, j = DAYS[a], DAYS[b]
            out.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
        elif part in DAYS:
            out.add(DAYS[part])
        else:
            raise ValueError(f"jours invalides : {s!r} (ex : lun,mer ou lun-ven)")
    return sorted(out)


def next_after(sched, now):
    """Prochaine exécution strictement après `now`, ou None si la tâche est terminée."""
    kind = sched["type"]
    if kind == "once":
        return sched["at"] if sched["at"] > now else None
    if kind == "every":
        return now + sched["minutes"] * 60
    h, m = sched["hm"]
    base = time.localtime(now)
    for off in range(0, 9):
        t = time.struct_time((base.tm_year, base.tm_mon, base.tm_mday + off, h, m, 0, 0, 0, -1))
        cand = time.mktime(t)
        if cand > now and time.localtime(cand).tm_wday in sched["days"]:
            return cand
    return None


def build_schedule(at="", daily="", every_minutes=0, weekdays="", now=None):
    now = now or time.time()
    given = [bool(at), bool(daily), bool(every_minutes)]
    if sum(given) != 1:
        raise ValueError("donne exactement une de ces options : at (une fois), daily (chaque jour), every_minutes")
    if at:
        try:
            ts = time.mktime(time.strptime(str(at).strip(), "%Y-%m-%d %H:%M"))
        except ValueError:
            raise ValueError("at : format AAAA-MM-JJ HH:MM attendu")
        if ts <= now:
            raise ValueError("at : cette date est déjà passée")
        if ts > now + 366 * 86400:
            raise ValueError("at : trop loin (1 an maximum)")
        return {"type": "once", "at": ts}
    if daily:
        return {"type": "daily", "hm": list(parse_hm(daily)), "days": parse_days(weekdays)}
    n = int(every_minutes)
    if n < 1 or n > 7 * 24 * 60:
        raise ValueError("every_minutes : entre 1 minute et 7 jours")
    return {"type": "every", "minutes": n}


def describe_schedule(s):
    if s["type"] == "once":
        return "une fois, " + fmt(s["at"])
    if s["type"] == "every":
        n = s["minutes"]
        return f"toutes les {n // 60} h" if n % 60 == 0 and n >= 60 else f"toutes les {n} min"
    days = {tuple(range(7)): "tous les jours", tuple(range(5)): "en semaine", (5, 6): "le week-end"}.get(tuple(s["days"])) \
        or ", ".join(JOURS[d] for d in s["days"])
    return f"{days} à {s['hm'][0]:02d}:{s['hm'][1]:02d}"


# ------------------------------------------------------------------ base de données

def _row(r):
    return {"id": r[0], "kind": r[1], "title": r[2], "text": r[3] or "", "schedule": json.loads(r[4]), "next_run": r[5],
            "last_run": r[6], "last_result": r[7] or "", "enabled": bool(r[8]), "extra": json.loads(r[10] or "{}")}


COLS = "id, kind, title, text, schedule, next_run, last_run, last_result, enabled, created, extra"


def jobs(include_done=True):
    with _db() as con:
        rows = con.execute(f"SELECT {COLS} FROM jobs ORDER BY enabled DESC, next_run IS NULL, next_run, id").fetchall()
    out = [_row(r) for r in rows]
    return out if include_done else [j for j in out if j["enabled"]]


def get(job_id):
    with _db() as con:
        r = con.execute(f"SELECT {COLS} FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    return _row(r) if r else None


def add(kind, title, text, schedule, extra=None, now=None):
    now = now or time.time()
    if len([j for j in jobs() if j["enabled"]]) >= MAX_JOBS:
        raise ValueError(f"trop de tâches actives ({MAX_JOBS} maximum)")
    nxt = next_after(schedule, now)
    with _db() as con:
        cur = con.execute("INSERT INTO jobs (kind, title, text, schedule, next_run, enabled, created, extra) VALUES (?,?,?,?,?,?,?,?)",
                          (kind, title[:80], text[:600], json.dumps(schedule), nxt, 1 if nxt else 0, now, json.dumps(extra or {})))
        return cur.lastrowid, nxt


def remove(job_id):
    with _db() as con:
        return con.execute("DELETE FROM jobs WHERE id=?", (int(job_id),)).rowcount > 0


def set_enabled(job_id, on):
    j = get(job_id)
    if not j:
        return False
    nxt = next_after(j["schedule"], time.time()) if on else j["next_run"]
    with _db() as con:
        con.execute("UPDATE jobs SET enabled=?, next_run=? WHERE id=?", (1 if on and nxt else 0, nxt, int(job_id)))
    return True


def _save_result(job_id, result, extra=None):
    with _db() as con:
        if extra is None:
            con.execute("UPDATE jobs SET last_result=? WHERE id=?", (result[:600], job_id))
        else:
            con.execute("UPDATE jobs SET last_result=?, extra=? WHERE id=?", (result[:600], json.dumps(extra), job_id))


def claim(job, now):
    """Réserve l'exécution (sûr si deux processus tournent) et programme la suivante. Renvoie True si c'est à nous."""
    nxt = next_after(job["schedule"], now)
    with _db() as con:
        return con.execute("UPDATE jobs SET next_run=?, last_run=?, enabled=? WHERE id=? AND next_run=? AND enabled=1",
                           (nxt, now, 1 if nxt else 0, job["id"], job["next_run"])).rowcount == 1


def due(now):
    with _db() as con:
        rows = con.execute(f"SELECT {COLS} FROM jobs WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now,)).fetchall()
    return [_row(r) for r in rows]


# ------------------------------------------------------------------ approbations en attente

def pending_add(source, tool, args, risk):
    with _db() as con:
        r = con.execute("SELECT id FROM pending WHERE status='attente' AND tool=? AND args=?",
                        (tool, json.dumps(args, sort_keys=True, ensure_ascii=False))).fetchone()
        if r:
            return r[0], False
        cur = con.execute("INSERT INTO pending (ts, source, tool, args, risk) VALUES (?,?,?,?,?)",
                          (time.time(), source, tool, json.dumps(args, sort_keys=True, ensure_ascii=False), risk))
        return cur.lastrowid, True


def pending_list(status="attente"):
    with _db() as con:
        rows = con.execute("SELECT id, ts, source, tool, args, risk, status FROM pending WHERE status=? ORDER BY id", (status,)).fetchall()
    return [{"id": r[0], "ts": r[1], "source": r[2], "tool": r[3], "args": json.loads(r[4]), "risk": r[5], "status": r[6]} for r in rows]


def pending_get(pid):
    with _db() as con:
        r = con.execute("SELECT id, ts, source, tool, args, risk, status FROM pending WHERE id=?", (int(pid),)).fetchone()
    return {"id": r[0], "ts": r[1], "source": r[2], "tool": r[3], "args": json.loads(r[4]), "risk": r[5], "status": r[6]} if r else None


def pending_close(pid, status):
    with _db() as con:
        con.execute("UPDATE pending SET status=? WHERE id=?", (status, int(pid)))


# ------------------------------------------------------------------ notifications

def emit(event):
    for cb in list(LISTENERS):
        try:
            cb(event)
        except Exception:
            pass


def notify(title, text, urgent=False):
    args = ["termux-notification", "-t", title[:80], "-c", text[:900], "--group", "sylvie"]
    if urgent:
        args += ["--priority", "high", "--vibrate", "400,200,400"]
    try:
        tools.termux(*args, timeout=10)
    except Exception:
        pass  # pas de Termux:API (tests, PC) : l'événement ci-dessous suffit
    emit({"type": "notify", "title": title, "text": text})


# ------------------------------------------------------------------ exécution

class UnattendedIO(engine.IO):
    """IO d'une tâche sans surveillance : applique la règle fixe décrite en tête de fichier."""

    def __init__(self, label, pre_approved):
        self.label, self.pre = label, set(pre_approved)
        self.out, self.errors = [], []

    def approve(self, name, args, risk, tainted):
        if tainted:
            return False   # contenu externe lu : aucune action sans toi
        if risk == "sensitive" or name not in self.pre:
            _, new = pending_add(self.label, name, args, risk)
            if new:
                notify("Sylvie attend ton accord", f"{self.label} voudrait lancer « {name} ». Ouvre Sylvie, rubrique Attente.", urgent=True)
            return False
        return True

    def denied(self, name, args):
        if name in FORBIDDEN:
            return
        _, new = pending_add(self.label, name, args, tools.REGISTRY[name]["risk"])
        if new:
            notify("Sylvie attend ton accord", f"{self.label} voudrait lancer « {name} » (non prévu). Ouvre Sylvie, rubrique Attente.", urgent=True)

    def say(self, text): self.out.append(text)
    def error(self, msg): self.errors.append(msg)


def home():
    lat, lon = (os.environ.get("SYLVIE_HOME_COORDS") or "5.36,-4.0").split(",")
    return os.environ.get("SYLVIE_HOME_CITY", "Abidjan"), lat.strip(), lon.strip()


def briefing_text():
    city, lat, lon = home()
    return (f"Prépare mon briefing du matin en 8 lignes maximum : la date, la météo d'aujourd'hui à {city} "
            f"(api_call meteo, latitude {lat}, longitude {lon}), mes rappels et tâches à venir (schedule_list), "
            "puis les 3 failles activement exploitées les plus récentes (api_call cisa_kev sans q). Ton chaleureux, sans markdown.")


BRIEFING_TOOLS = ["api_call", "api_info", "schedule_list", "get_time", "recall"]


def run_task(job):
    ex = job["extra"]
    allowed = ex.get("tools") or SAFE_DEFAULT
    io = UnattendedIO(f"{job['title']} (tâche {job['id']})", allowed)
    note = ("Tâche automatique programmée par l'utilisateur, exécutée sans lui. Fais-la sans poser de question, "
            "avec seulement les outils autorisés, et réponds par un compte rendu de 8 lignes maximum.")
    engine.run(job["text"], [], io, allowed=allowed, extra=note, label=f"tâche {job['id']}",
               budget=engine.Budget(10), max_steps=6, thinking=False)
    if io.out:
        return "\n".join(io.out), True
    return ("échec : " + "; ".join(io.errors)) if io.errors else "(rien à signaler)", False


def run_watch(job):
    ex = dict(job["extra"])
    try:
        res = apis.call(ex["api"], ex.get("params") or {})
    except apis.ApiError as e:
        ex["fails"] = ex.get("fails", 0) + 1
        _save_result(job["id"], f"erreur : {e}", ex)
        if ex["fails"] == 3:
            notify(f"Surveillance en panne : {job['title']}", str(e))
        return
    digest = hashlib.sha1(json.dumps(res["resultat"], sort_keys=True, default=str).encode()).hexdigest()
    old, ex["last_hash"], ex["fails"] = ex.get("last_hash"), digest, 0
    shown = res["resultat"] if isinstance(res["resultat"], str) else json.dumps(res["resultat"], ensure_ascii=False)
    if old is None:
        _save_result(job["id"], "état initial enregistré", ex)
    elif old != digest:
        _save_result(job["id"], "changement détecté : " + shown, ex)
        notify(f"Changement : {job['title']}", shown[:700], urgent=True)
    else:
        _save_result(job["id"], "inchangé", ex)


_llm_lock = threading.Lock()


def run_job(job, late=False):
    """Exécute une tâche maintenant (bloquant). Renvoie le résultat sous forme de texte."""
    gate.log("TACHE", f"{job['kind']}:{job['id']}", {"titre": job["title"], "en_retard": late})
    kind = job["kind"]
    try:
        if kind == "rappel":
            text = ("(en retard) " if late else "") + (job["text"] or job["title"])
            _save_result(job["id"], text)
            notify(f"Rappel : {job['title']}", text, urgent=True)
            return text
        if kind == "surveillance":
            run_watch(job)
            return get(job["id"])["last_result"] if get(job["id"]) else ""
        with _llm_lock:   # une tâche IA à la fois : ménage les quotas
            text, ok = run_task(job)
        _save_result(job["id"], text)
        notify(job["title"] if ok else f"Tâche en échec : {job['title']}", text, urgent=not ok)
        return text
    except Exception as e:  # une tâche ne doit jamais faire tomber le planificateur
        _save_result(job["id"], f"erreur : {e}")
        notify(f"Tâche en échec : {job['title']}", str(e))
        return f"erreur : {e}"


class Scheduler(threading.Thread):
    def __init__(self, tick=20):
        super().__init__(daemon=True, name="sylvie-scheduler")
        self.tick, self._stop_ev = tick, threading.Event()

    def poll(self, now=None, wait=False):
        """Lance les tâches échues. `wait=True` attend leur fin (tests)."""
        now = now or time.time()
        threads = []
        for job in due(now):
            late = now - job["next_run"]
            if not claim(job, now):
                continue
            if late > LATE_MAX and job["schedule"]["type"] != "once":
                continue  # téléphone éteint trop longtemps : on saute, la suivante est déjà programmée
            t = threading.Thread(target=run_job, args=(job, late > 120), daemon=True)
            t.start()
            threads.append(t)
        if wait:
            for t in threads:
                t.join()
        return len(threads)

    def run(self):
        while not self._stop_ev.wait(self.tick):
            try:
                self.poll()
            except Exception:
                pass

    def stop(self):
        self._stop_ev.set()


_instance = None


def start(tick=20):
    global _instance
    if _instance is None or not _instance.is_alive():
        _instance = Scheduler(tick)
        _instance.start()
    return _instance


# ------------------------------------------------------------------ outils du modèle

def schedule_add(kind, title, text="", at="", daily="", every_minutes=0, weekdays="", tools_list="", api="", params="{}"):
    kind = {"rappel": "rappel", "tache": "tache", "tâche": "tache", "surveillance": "surveillance", "briefing": "briefing"}.get(str(kind).lower())
    if not kind:
        raise ValueError("kind : rappel, tache, surveillance ou briefing")
    title = str(title).strip()
    if not title:
        raise ValueError("title obligatoire")
    every_minutes = int(every_minutes or 0)
    if every_minutes and every_minutes < MIN_EVERY[kind]:
        raise ValueError(f"{kind} : au moins toutes les {MIN_EVERY[kind]} minutes")
    sched = build_schedule(at, daily, every_minutes, weekdays)
    extra = {}
    if kind == "surveillance":
        e = apis.CATALOG.get(api)
        if not e:
            raise ValueError(apis.unknown_msg(api))
        p = json.loads(params) if isinstance(params, str) and params.strip() else (params or {})
        apis.validate(e, p)
        extra = {"api": api, "params": p}
        if sched["type"] == "every" and sched["minutes"] < 10:
            raise ValueError("surveillance : au moins toutes les 10 minutes")
        if sched["type"] == "once":
            raise ValueError("une surveillance doit se répéter (every_minutes ou daily)")
    elif kind in ("tache", "briefing"):
        if kind == "briefing":
            text, names = briefing_text(), BRIEFING_TOOLS
        else:
            names = [n.strip() for n in str(tools_list).split(",") if n.strip()] or SAFE_DEFAULT
            if not text.strip():
                raise ValueError("text : décris ce que la tâche doit faire")
        for n in names:
            t = tools.REGISTRY.get(n)
            if not t:
                raise ValueError(f"outil inconnu : {n}")
            if t["risk"] == "sensitive" or n in FORBIDDEN:
                raise ValueError(f"l'outil {n} ne peut pas être utilisé dans une tâche automatique")
        extra = {"tools": names}
    elif not text.strip():
        text = title
    job_id, nxt = add(kind, title, text, sched, extra)
    return {"id": job_id, "quand": describe_schedule(sched), "prochaine_execution": fmt(nxt)}


def schedule_list():
    out = [{"id": j["id"], "type": j["kind"], "titre": j["title"], "quand": describe_schedule(j["schedule"]),
            "prochaine": fmt(j["next_run"]) if j["enabled"] else "terminée", "dernier_resultat": j["last_result"][:120]}
           for j in jobs()]
    return out or "aucune tâche programmée"


def schedule_remove(job_id):
    if not remove(job_id):
        raise ValueError(f"pas de tâche n° {job_id}")
    return "supprimée"


tools.tool(schedule_add, "confirm",
           "Programme un rappel, une tâche IA récurrente, une surveillance d'API (alerte si le résultat change) ou le briefing du matin. "
           "Donne EXACTEMENT UNE option de moment : at (AAAA-MM-JJ HH:MM, une fois), daily (HH:MM, avec weekdays facultatif : lun,mer ou lun-ven) ou every_minutes. "
           "rappel : text = message. tache : text = consigne, tools_list = outils permis séparés par des virgules (les seuls utilisables sans l'utilisateur). "
           "surveillance : api + params (JSON). briefing : seulement daily.",
           {"kind": (tools.S, "rappel | tache | surveillance | briefing"), "title": (tools.S, "nom court"),
            "text": (tools.S, "message ou consigne"), "at": (tools.S, "AAAA-MM-JJ HH:MM"), "daily": (tools.S, "HH:MM"),
            "every_minutes": (tools.I, "intervalle en minutes"), "weekdays": (tools.S, "ex lun-ven (avec daily)"),
            "tools_list": (tools.S, "outils permis pour une tache"), "api": (tools.S, "nom de l'API (surveillance)"),
            "params": (tools.S, "paramètres JSON de l'API")},
           required=["kind", "title"], writes=True)
tools.tool(schedule_list, "auto", "Liste les rappels, tâches et surveillances programmés")
tools.tool(schedule_remove, "confirm", "Supprime une tâche programmée", {"job_id": (tools.I, "numéro de la tâche")})
