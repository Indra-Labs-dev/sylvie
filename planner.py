"""Agent à objectifs : un plan que tu valides, puis des sous-agents spécialisés qui l'exécutent étape par étape.

Garanties (appliquées par le code) :
  - rien ne démarre sans ton accord sur le plan, qui montre pour chaque étape les outils prévus ;
  - un sous-agent n'a accès qu'aux outils de son type d'étape, et seulement à ceux prévus dans le plan ;
  - les outils « confirm » du plan sont déjà validés, sauf si du contenu externe a été lu juste avant ;
  - SMS et appels demandent toujours de taper OUI ; budget d'appels au modèle plafonné ; arrêt possible à tout moment ;
  - tant que le plan n'utilise aucun outil privé (position, contacts, SMS, mémoire), les étapes peuvent passer par
    des fournisseurs d'IA « publics » pour ménager le quota principal ; sinon tout reste chez un fournisseur de confiance.
"""
import contextlib, json, os, sqlite3, time

import engine, gate, llm, tools

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_STEPS = 7
MAX_GOAL = 600

TYPES = {
    "recherche": ["web_search", "read_url", "api_call", "api_info"],
    "donnees": ["api_call", "api_info", "calculate", "get_time", "recall"],
    "telephone": ["battery", "network", "notify", "vibrate", "torch", "clipboard_get", "clipboard_set", "location",
                  "find_contact", "sms_list", "sms_send", "call", "open_url", "play_music"],
    "fichiers": ["files_list", "files_find", "files_read", "files_summarize", "files_grep", "csv_stats", "files_write", "files_open"],
    "agenda": ["agenda_list", "agenda_add", "agenda_update", "agenda_remove", "agenda_phone", "agenda_import", "agenda_export"],
    "vision": ["camera_look", "image_analyze"],
    "redaction": [],
    "verification": [],
}
DEFAULT_TOOLS = {"recherche": ["web_search", "api_call", "api_info"], "donnees": ["api_call", "api_info", "calculate", "get_time"],
                 "telephone": [], "fichiers": ["files_list", "files_find"], "agenda": ["agenda_list"], "vision": [],
                 "redaction": [], "verification": []}
PRIVATE_TYPES = {"telephone", "fichiers", "agenda", "vision"}
PRIVATE_TOOLS = ({"recall", "remember", "location", "find_contact", "sms_list", "sms_send", "call", "clipboard_get", "clipboard_set"}
                 | set(TYPES["fichiers"]) | set(TYPES["agenda"]) | set(TYPES["vision"]))
LABELS = {"recherche": "Recherche", "donnees": "Données", "telephone": "Téléphone", "fichiers": "Fichiers", "agenda": "Agenda",
          "vision": "Vision", "redaction": "Rédaction", "verification": "Vérification"}

SUB_NOTES = {
    "recherche": "Tu es le sous-agent RECHERCHE de Sylvie. Cherche l'information demandée avec tes outils, recoupe-la, puis rends un compte rendu factuel de 12 lignes maximum (chiffres, dates, sources).",
    "donnees": "Tu es le sous-agent DONNÉES de Sylvie. Récupère les données avec les API, fais les calculs avec calculate, puis rends un compte rendu chiffré de 12 lignes maximum.",
    "telephone": "Tu es le sous-agent TÉLÉPHONE de Sylvie. N'utilise que les outils nécessaires à l'étape (chaque action sensible sera validée par l'utilisateur) et rends un compte rendu de ce qui a été fait ou lu.",
    "fichiers": "Tu es le sous-agent FICHIERS de Sylvie. Cherche, lis ou résume les fichiers demandés (préfère files_summarize et csv_stats aux longues lectures), écris un fichier seulement si l'étape le demande, et rends un compte rendu de 12 lignes maximum avec les chemins utilisés.",
    "agenda": "Tu es le sous-agent AGENDA de Sylvie. Consulte l'agenda et n'ajoute ou ne modifie des événements que si l'étape le demande ; signale les conflits d'horaire. Rends un compte rendu court.",
    "vision": "Tu es le sous-agent VISION de Sylvie. Regarde avec camera_look ou image_analyze en posant une question précise, puis rends ce que tu as vu ou lu, fidèlement.",
    "redaction": "Tu es le sous-agent RÉDACTION de Sylvie. Sans outil, rédige le livrable demandé à partir des résultats précédents : français clair, concis, sans markdown lourd.",
    "verification": "Tu es le sous-agent VÉRIFICATION de Sylvie. Sans outil, relis les résultats par rapport à l'objectif : signale incohérences et manques, puis conclus par OK ou par les corrections à apporter.",
}


def _db_path():
    return os.environ.get("SYLVIE_DB", os.path.join(HERE, "sylvie.db"))


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(_db_path(), timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS goals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, goal TEXT, plan TEXT, "
                    "status TEXT, result TEXT, done INTEGER DEFAULT 0, calls INTEGER DEFAULT 0)")
        with con:
            yield con
    finally:
        con.close()


def goals(limit=10):
    with _db() as con:
        rows = con.execute("SELECT id, ts, goal, plan, status, result, done, calls FROM goals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r[0], "ts": r[1], "goal": r[2], "plan": json.loads(r[3] or "null"), "status": r[4], "result": r[5] or "",
             "done": r[6], "calls": r[7]} for r in rows]


def _new(goal):
    with _db() as con:
        return con.execute("INSERT INTO goals (ts, goal, status) VALUES (?,?,?)", (time.time(), goal, "plan")).lastrowid


def _set(gid, **kw):
    if not kw:
        return
    with _db() as con:
        con.execute("UPDATE goals SET " + ", ".join(f"{k}=?" for k in kw) + " WHERE id=?",
                    [json.dumps(v) if k == "plan" else v for k, v in kw.items()] + [gid])


# ------------------------------------------------------------------ plan

def plan_prompt():
    kinds = "\n".join(f"- {t} : outils possibles = {', '.join(v) or 'aucun'}" for t, v in TYPES.items())
    return (
        "Tu es le planificateur de Sylvie, une assistante sur le téléphone de l'utilisateur. Découpe l'objectif en étapes "
        f"({MAX_STEPS} au maximum, une seule si cela suffit). Réponds UNIQUEMENT par un objet JSON de la forme :\n"
        '{"steps":[{"title":"titre court","type":"recherche","goal":"consigne précise pour le sous-agent","tools":["web_search"]}]}\n'
        f"Types d'étape :\n{kinds}\n"
        "N'indique dans tools que les outils réellement utiles, choisis parmi ceux du type. Les étapes redaction et verification "
        "n'ont pas d'outils. Termine par une étape redaction qui produit la réponse finale. N'invente aucune action que "
        "l'utilisateur n'a pas demandée, et ne prévois sms_send ou call que si l'objectif l'exige explicitement.")


def parse_plan(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("le planificateur n'a pas renvoyé de plan lisible")
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        raise ValueError("le planificateur n'a pas renvoyé de plan lisible")


def normalize(raw):
    steps = []
    for s in (raw.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(s, dict) or not str(s.get("goal") or s.get("title") or "").strip():
            continue
        typ = str(s.get("type", "")).lower()
        typ = typ if typ in TYPES else "recherche"
        wanted = [t for t in (s.get("tools") or []) if isinstance(t, str) and t in TYPES[typ] and t in tools.REGISTRY]
        steps.append({"title": str(s.get("title") or s.get("goal"))[:70], "type": typ,
                      "goal": str(s.get("goal") or s.get("title"))[:500], "tools": wanted or list(DEFAULT_TOOLS[typ])})
    if not steps:
        raise ValueError("plan vide")
    return {"steps": steps, "private": is_private({"steps": steps})}


def is_private(plan):
    return any(s["type"] in PRIVATE_TYPES or PRIVATE_TOOLS & set(s["tools"]) for s in plan["steps"])


def plan_lines(plan):
    lines = []
    for i, s in enumerate(plan["steps"], 1):
        t = ", ".join(s["tools"]) or "aucun outil"
        lines.append(f"{i}. {LABELS[s['type']]} · {s['title']}  ({t})")
    lines.append("Données privées : fournisseur de confiance uniquement." if plan.get("private")
                 else "Aucun outil privé : des fournisseurs d'IA publics peuvent aider.")
    return lines


def make_plan(goal, budget=None):
    if budget:
        budget.take()
    cand = llm.generate([{"role": "user", "parts": [{"text": f"OBJECTIF : {goal}"}]}], system=plan_prompt(),
                        thinking=False, role="chat", private=True)
    return normalize(parse_plan(llm.text_of(cand["content"])))


# ------------------------------------------------------------------ exécution

class PlanIO(engine.IO):
    """Les outils « confirm » prévus au plan sont déjà validés (sauf après contenu externe) ; le reste passe par l'utilisateur."""

    def __init__(self, io, approved):
        self.io, self.approved = io, set(approved)

    def approve(self, name, args, risk, tainted):
        if risk == "confirm" and not tainted and name in self.approved:
            return True
        return self.io.approve(name, args, risk, tainted)

    def tool(self, *a): self.io.tool(*a)
    def denied(self, name, args): self.io.denied(name, args)
    def image(self, d): self.io.image(d)
    def info(self, m): self.io.info(m)
    def ok(self, m): self.io.ok(m)
    def error(self, m): self.io.error(m)
    def busy(self, label): return self.io.busy(label)


class Collect(PlanIO):
    def __init__(self, io, approved):
        super().__init__(io, approved)
        self.out, self.errors = [], []

    def say(self, text): self.out.append(text)
    def error(self, m):
        self.errors.append(m)
        self.io.error(m)


def _clip(text, n):
    return text if len(text) <= n else text[:n] + "…"


def run_step(goal, plan, i, results, io, gid, budget, cancel):
    step = plan["steps"][i]
    sub = Collect(io, [t for s in plan["steps"] for t in s["tools"]])
    ctx = "\n\n".join(f"[{r['title']}]\n{_clip(r['result'], 1500)}" for r in results)
    prompt = (f"OBJECTIF GLOBAL : {goal}\nÉTAPE {i + 1}/{len(plan['steps'])} : {step['title']}\nCONSIGNE : {step['goal']}"
              + (f"\n\nRÉSULTATS DES ÉTAPES PRÉCÉDENTES :\n{_clip(ctx, 6000)}" if ctx else ""))
    engine.run(prompt, [], sub, allowed=step["tools"], extra=SUB_NOTES[step["type"]], role="chat" if step["tools"] else "fast",
               private=plan["private"], budget=budget, cancel=cancel, label=f"objectif {gid}.{i + 1}", max_steps=6, thinking=False)
    if sub.out:
        return "\n".join(sub.out), True
    return ("(échec : " + "; ".join(sub.errors) + ")") if sub.errors else "(aucun résultat)", False


def synthesize(goal, plan, results, budget):
    body = "\n\n".join(f"[{r['title']}]\n{_clip(r['result'], 2000)}" for r in results)
    try:
        budget.take()
        cand = llm.generate([{"role": "user", "parts": [{"text": f"OBJECTIF : {goal}\n\nRÉSULTATS DES ÉTAPES :\n{body}"}]}],
                            system="Tu es Sylvie. Rédige la réponse finale à l'objectif à partir des résultats, en français, concise, "
                                   "sans markdown lourd. Signale ce qui a échoué ou manque.",
                            thinking=False, role="fast", private=plan["private"])
        return llm.text_of(cand["content"])
    except Exception:
        return body


def run_goal(goal, io, cancel=None, calls=40):
    """Plan -> accord -> étapes -> réponse. Renvoie le texte final (ou la raison de l'arrêt)."""
    goal = " ".join(str(goal).split())
    if not goal:
        return "objectif vide"
    if len(goal) > MAX_GOAL:
        return f"objectif trop long ({MAX_GOAL} caractères maximum)"
    gid, budget = _new(goal), engine.Budget(calls)
    gate.log("OBJECTIF", f"#{gid}", {"objectif": goal})
    try:
        with io.busy("Je prépare un plan"):
            plan = make_plan(goal, budget)
    except Exception as e:
        _set(gid, status="echec", result=str(e))
        io.error(f"plan impossible : {e}")
        return f"Je n'ai pas pu établir de plan : {e}"
    _set(gid, plan=plan)
    if not io.approve_plan(goal, plan):
        _set(gid, status="refuse")
        gate.log("PLAN-REFUSE", f"#{gid}", {})
        return "Plan refusé, je n'ai rien lancé."
    gate.log("PLAN-ACCEPTE", f"#{gid}", {"étapes": [s["title"] for s in plan["steps"]]})
    _set(gid, status="en cours")
    results, stopped = [], False
    for i, step in enumerate(plan["steps"]):
        if cancel is not None and cancel.is_set():
            stopped = True
            break
        if budget.left <= 0:
            io.error("budget d'appels au modèle épuisé")
            break
        io.progress(f"Étape {i + 1}/{len(plan['steps'])} · {step['title']}")
        text, ok = run_step(goal, plan, i, results, io, gid, budget, cancel)
        results.append({"title": step["title"], "result": text, "ok": ok})
        _set(gid, done=i + 1, calls=budget.used)
        if cancel is not None and cancel.is_set():
            stopped = True
            break
    if stopped:
        _set(gid, status="arrete", result="arrêté par l'utilisateur")
        return "Objectif arrêté. Ce qui avait été trouvé : " + _clip(" | ".join(r["result"] for r in results), 600)
    if not results:
        _set(gid, status="echec")
        return "Aucune étape n'a pu être exécutée."
    last_is_text = plan["steps"][len(results) - 1]["type"] == "redaction" and results[-1]["ok"] and len(results) == len(plan["steps"])
    final = results[-1]["result"] if last_is_text else synthesize(goal, plan, results, budget)
    _set(gid, status="termine", result=final, calls=budget.used)
    gate.log("OBJECTIF-FIN", f"#{gid}", {"appels": budget.used})
    return final


def objectif(goal):
    io = engine.current_io()
    final = run_goal(goal, io, cancel=engine.current_cancel())

    return final


tools.tool(objectif, "auto",
           "Mène à bien un objectif en plusieurs étapes (recherches, analyses, actions) : propose un plan à l'utilisateur, "
           "l'exécute avec des sous-agents après son accord, puis renvoie le résultat. À utiliser pour les demandes complexes.",
           {"goal": (tools.S, "l'objectif complet, en une ou deux phrases")}, external=True)
