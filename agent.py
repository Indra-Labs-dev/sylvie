"""Sylvie — agent IA personnel sur Termux. Lancer : . ./.env && python agent.py"""
import base64, json, os, sys, threading

import agenda, apis, backup, camera, diagnostic, engine, files, gate, habits, history, llm, memory, planner, router, rules, scheduler, tools, ui, voice  # noqa: F401
from fast import fast_path

STT = os.environ.get("SYLVIE_STT", "gemini")  # gemini (enregistre + transcrit) | android
SPEAK = os.environ.get("SYLVIE_SPEAK") == "1"
REC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".voice.m4a")
MAX_HISTORY, MAX_STEPS = engine.MAX_HISTORY, engine.MAX_STEPS

HELP = [
    ("v", "parler à Sylvie (Entrée pour terminer)"),
    ("/voix", "activer/désactiver la lecture à voix haute"),
    ("/memoire", "ce que Sylvie a retenu"),
    ("/oublie N", "effacer le fait n° N"),
    ("/outils", "liste des outils et leur niveau de risque"),
    ("/journal", "dernières actions autorisées/refusées"),
    ("/modele [nom]", "voir ou changer le modèle principal"),
    ("/fournisseurs", "fournisseurs d'IA : état, budgets (voir « /fournisseurs aide »)"),
    ("/apis [mot]", "catalogue d'API gratuites (filtre facultatif)"),
    ("/apitest [nom]", "tester une API (ou toutes) avec son exemple"),
    ("/objectif <texte>", "mission en plusieurs étapes : plan à valider, puis exécution"),
    ("/taches [rm|pause|reprise|lancer N]", "rappels, tâches et surveillances programmés"),
    ("/briefing HH:MM", "briefing du matin chaque jour à cette heure"),
    ("/attente", "actions que des tâches automatiques attendent de toi"),
    ("/agenda [jours]", "événements à venir de l'agenda de Sylvie"),
    ("/veille", "veilles sécurité en cours (pour en ajouter : « surveille Huawei »)"),
    ("/candidatures", "suivi des candidatures"),
    ("/regles", "règles permanentes : autoriser d'avance un genre d'action (voir « /regles aide »)"),
    ("/habitudes", "demandes que tu répètes ; « /habitudes effacer » supprime l'historique"),
    ("/diagnostic", "vérifie permissions, outils, clés, réseau et dit quoi corriger"),
    ("/quotas", "appels du jour par fournisseur d'IA et par API"),
    ("/sauvegarde", "sauvegarde la mémoire, l'agenda et les tâches (7 dernières gardées)"),
    ("/restaurer [nom]", "restaure une sauvegarde (action sensible)"),
    ("/r", "réessayer la dernière question (après une panne)"),
    ("/reset", "nouvelle conversation (la mémoire reste)"),
    ("/aide", "cette aide"),
    ("q", "quitter"),
]


class CliIO(engine.IO):
    """Terminal : affichage coloré, confirmations par boîte de dialogue Android, voix."""

    def tool(self, name, risk, args): ui.tool(name, risk, args)
    def approve(self, name, args, risk, tainted): return gate.ask(name, args, risk, tainted=tainted)
    def say(self, text): say(text)
    def info(self, msg): ui.info(msg)
    def ok(self, msg): ui.ok(msg)
    def error(self, msg): ui.error(msg)
    def progress(self, msg): ui.out("  " + ui.dim(msg))
    def busy(self, label): return ui.Spinner(label, color=ui.cyan if label.endswith("…") else None)

    def approve_plan(self, goal, plan):
        ui.box("Plan proposé", [f"Objectif : {goal}"] + planner.plan_lines(plan), color=ui.violet)
        return gate.ask("plan", {"objectif": goal, "étapes": len(plan["steps"])}, "confirm")


system_prompt = engine.system_prompt
trim = engine.trim
CLI_CANCEL = threading.Event()


def run_tool(name, args, state):
    return engine.run_tool(name, args, state, CliIO())


def transcribe(path):
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    cand = llm.generate([{"role": "user", "parts": [
        {"inlineData": {"mimeType": "audio/m4a", "data": data}},
        {"text": "Transcris exactement ce message vocal, qui est en français. Réponds uniquement par la "
                 "transcription. S'il n'y a pas de parole, réponds par un seul point."}]}],
        system="Tu es un outil de transcription. Tu ne fais que transcrire.", role="audio", thinking=False)
    text = llm.text_of(cand["content"])
    return "" if text in ("", ".") else text


def hear():
    if STT == "android":
        with ui.Spinner("Je t'écoute", style="pulse", color=ui.red):
            text = voice.listen()
    else:
        def wait():
            with ui.Spinner("Enregistrement… Entrée pour terminer", style="pulse", color=ui.red):
                input()
        if not voice.record(REC_PATH, wait=wait):
            ui.error("Enregistrement impossible (permission Microphone de Termux:API ?)")
            return ""
        try:
            with ui.Spinner("Transcription"):
                text = transcribe(REC_PATH)
        except Exception as e:
            ui.error(str(e))
            text = ""
        finally:
            try:
                os.remove(REC_PATH)  # on ne garde pas l'enregistrement
            except OSError:
                pass
    if not text:
        ui.warn("Je n'ai rien compris.")
    return text


def say(text):
    if SPEAK:
        voice.speak(text)
    ui.sylvie(text)


def command(q, contents):
    """Commandes locales. Renvoie True si la saisie a été traitée."""
    global SPEAK
    cmd, _, arg = q.partition(" ")
    cmd = cmd.lower()
    if cmd in ("/aide", "/help", "?"):
        w = max(len(k) for k, _ in HELP)
        for k, d in HELP:
            ui.out(f"  {ui.cyan(k.ljust(w))}  {d}")
    elif cmd == "/voix":
        SPEAK = not SPEAK
        voice.stop()
        ui.info(f"Lecture à voix haute {'activée' if SPEAK else 'désactivée'}")
    elif cmd == "/memoire":
        facts = memory.all_facts(200)
        if not facts:
            ui.info("Mémoire vide. Dis-moi par exemple : « retiens que je bois mon café sans sucre ».")
        kinds = {f["id"]: f["kind"] for f in memory.facts(200)}
        for i, t, d in facts:
            ui.out(f"  {ui.violet(str(i).rjust(3))}  {t} {ui.dim(d[:10] + (' · ' + memory.KINDS.get(kinds.get(i), '') if kinds.get(i) else ''))}")
    elif cmd == "/oublie":
        if arg.strip().isdigit() and memory.delete(arg.strip()):
            ui.ok(f"Fait n° {arg.strip()} oublié")
            gate.log("OUBLI", "memory", {"id": arg.strip()})
        else:
            ui.warn("Usage : /oublie N  (voir /memoire)")
    elif cmd == "/outils":
        labels = {"auto": ui.green("auto     "), "confirm": ui.yellow("confirmer"), "sensitive": ui.red("taper OUI")}
        for name, t in tools.REGISTRY.items():
            ui.out(f"  {labels[t['risk']]}  {name.ljust(14)} {ui.dim(t['decl']['description'][:50])}")
    elif cmd == "/journal":
        for line in gate.last_actions(15) or ["(vide)"]:
            ui.out("  " + ui.dim(line))
    elif cmd == "/modele":
        if arg.strip():
            router.save_local(lambda d: d.setdefault("overrides", {}).setdefault("gemini-flash", {}).update(model=arg.strip()))
        p = router.get("gemini-flash")
        ui.info(f"Modèle principal : {p['model']} · secours : {router.get('gemini-lite')['model']}")
    elif cmd == "/fournisseurs":
        providers_cmd(arg.split())
    elif cmd == "/apis":
        apis_cmd(arg.strip())
    elif cmd == "/apitest":
        apitest_cmd(arg.strip())
    elif cmd == "/objectif":
        if not arg.strip():
            ui.warn("Usage : /objectif <ce que tu veux obtenir>")
        else:
            CLI_CANCEL.clear()
            try:
                io = CliIO()
                io.say(planner.run_goal(arg, io, cancel=CLI_CANCEL))
            except KeyboardInterrupt:
                CLI_CANCEL.set()
                ui.warn("Objectif interrompu")
    elif cmd == "/taches":
        jobs_cmd(arg.split())
    elif cmd == "/briefing":
        try:
            r = scheduler.schedule_add("briefing", "Briefing du matin", daily=arg.strip() or "07:30")
            ui.ok(f"Briefing programmé : {r['quand']} (prochain : {r['prochaine_execution']})")
        except ValueError as e:
            ui.warn(str(e))
    elif cmd == "/attente":
        pending_cmd()
    elif cmd == "/agenda":
        evs = agenda.agenda_list(int(arg) if arg.strip().isdigit() else 7)
        if isinstance(evs, str):
            ui.info(evs)
        for e in evs if isinstance(evs, list) else []:
            ui.out(f"  {ui.violet(str(e['id']).rjust(3))}  {e['quand']}  {e['titre']}" + (ui.dim(f" · {e['lieu']}") if e.get("lieu") else ""))
    elif cmd in ("/veille", "/candidatures"):
        import candidatures, veille
        res = veille.veille_list() if cmd == "/veille" else candidatures.candidature_list()
        if isinstance(res, str):
            ui.info(res)
        for r in [] if isinstance(res, str) else res:
            ui.out("  " + ui.violet(str(r["id"]).rjust(3)) + "  " + "  ".join(f"{v}" for k, v in r.items() if k != "id" and v))
    elif cmd == "/regles":
        rules_cmd(arg.split())
    elif cmd == "/habitudes":
        if arg.strip() == "effacer":
            habits.clear()
            ui.ok("Historique des habitudes effacé")
        else:
            rows = habits.summary()
            for sample, n in rows:
                ui.out(f"  {ui.violet(str(n).rjust(3))}×  {sample}")
            if not rows:
                ui.info("Aucune demande répétée pour l'instant.")
    elif cmd == "/diagnostic":
        with ui.Spinner("Diagnostic"):
            items = diagnostic.run()
        mark = {"ok": ui.green("✔"), "warn": ui.yellow("!"), "bad": ui.red("✖")}
        for lvl, label, hint in items:
            ui.out(f"  {mark[lvl]} {label}" + (f"\n      {ui.dim(hint)}" if hint else ""))
        ui.info(diagnostic.summary(items))
    elif cmd == "/quotas":
        for r in router.table():
            if r["ok"] or r["calls"]:
                ui.out(f"  {r['id'].ljust(14)} {r['calls']}{'/' + str(r['limit']) if r['limit'] else ''} appels aujourd'hui · {ui.dim(r['why'])}")
        used = apis.usage_today()
        ui.out("  " + (", ".join(f"{k} {v}" for k, v in used.items()) if used else ui.dim("aucune API appelée aujourd'hui")))
    elif cmd == "/sauvegarde":
        try:
            p = backup.create()
            ui.ok(f"Sauvegarde : {p}")
            ui.out(ui.dim("  " + ", ".join(b["nom"] for b in backup.list_backups()[:3])))
        except Exception as e:
            ui.error(str(e))
    elif cmd == "/restaurer":
        names = [b["nom"] for b in backup.list_backups()]
        if not arg.strip():
            ui.info("Sauvegardes : " + (", ".join(names) or "aucune") + "  ·  /restaurer <nom>")
        elif gate.ask("restaurer_sauvegarde", {"nom": arg.strip(), "effet": "remplace mémoire, agenda et tâches actuels"}, "sensitive"):
            try:
                backup.restore(arg.strip())
                contents.clear()
                history.clear("cli")
                ui.ok("Restauré. Relance Sylvie pour tout recharger.")
            except Exception as e:
                ui.error(str(e))
    elif cmd == "/reset":
        contents.clear()
        history.clear("cli")
        ui.ok("Nouvelle conversation")
    else:
        return False
    return True


def providers_cmd(a):
    if a and a[0] in ("aide", "help"):
        for k, d in [("/fournisseurs", "état, budgets du jour, pauses"),
                     ("/fournisseurs test [id]", "tester un fournisseur (tous si omis)"),
                     ("/fournisseurs modeles <id>", "lister les modèles disponibles"),
                     ("/fournisseurs modele <id> <modèle>", "choisir le modèle d'un fournisseur"),
                     ("/fournisseurs confiance <id> oui|non", "autoriser (oui) ou non un fournisseur à voir tes données privées"),
                     ("/fournisseurs reset", "lever toutes les pauses")]:
            ui.out(f"  {ui.cyan(k.ljust(38))} {d}")
    elif not a:
        for r in router.table():
            mark = ui.green("●") if r["ok"] else ui.dim("○")
            lim = f"/{r['limit']}" if r["limit"] else ""
            conf = ui.violet("privé") if r["trust"] == "private" else ui.dim("public")
            ui.out(f"  {mark} {r['id'].ljust(14)} {conf}  {str(r['model'])[:24].ljust(24)} {ui.dim(r['roles'].ljust(22))} "
                   f"{r['calls']}{lim} appels · {ui.dim(r['why'])}")
        ui.out(ui.dim("  « public » = ne reçoit jamais SMS, contacts, position ni mémoire."))
    elif a[0] == "test":
        for p in router.providers():
            if len(a) > 1 and p["id"] != a[1]:
                continue
            if len(a) == 1 and not os.environ.get(p.get("env", "")):
                continue
            with ui.Spinner(f"test {p['id']}"):
                ok, why = router.test(p["id"])
            (ui.ok if ok else ui.warn)(f"{p['id']} : {why}")
    elif a[0] == "modeles" and len(a) > 1:
        try:
            with ui.Spinner("Modèles"):
                ids = router.list_models(a[1])
            ui.out("  " + "\n  ".join(ids[:60]) if ids else "  (aucun)")
        except Exception as e:
            ui.error(str(e))
    elif a[0] == "modele" and len(a) > 2:
        if router.get(a[1]):
            router.save_local(lambda d: d.setdefault("overrides", {}).setdefault(a[1], {}).update(model=a[2]))
            ui.ok(f"{a[1]} → {a[2]}")
        else:
            ui.warn("Fournisseur inconnu (voir /fournisseurs)")
    elif a[0] == "confiance" and len(a) > 2 and a[2] in ("oui", "non"):
        p = router.get(a[1])
        if not p:
            ui.warn("Fournisseur inconnu (voir /fournisseurs)")
        elif a[2] == "oui" and not gate.ask("autoriser_donnees_privees", {"fournisseur": a[1]}, "sensitive"):
            pass
        else:
            router.save_local(lambda d: d.setdefault("overrides", {}).setdefault(a[1], {}).update(trust="private" if a[2] == "oui" else "public"))
            ui.ok(f"{a[1]} : {'données privées autorisées' if a[2] == 'oui' else 'public uniquement'}")
    elif a[0] == "reset":
        router.reset_pauses()
        ui.ok("Pauses levées")
    else:
        ui.warn("Usage : /fournisseurs aide")


def apis_cmd(word):
    health = apis.health()
    shown = 0
    for cat, names in apis.names_by_category().items():
        names = [n for n in names if not word or word.lower() in (n + " " + apis.CATALOG[n]["desc"] + " " + " ".join(apis.CATALOG[n]["keywords"])).lower()]
        if not names:
            continue
        ui.out("  " + ui.violet(cat))
        for n in names:
            h = health.get(n)
            mark = ui.dim("·") if h is None else (ui.green("●") if h[0] else ui.red("●"))
            ui.out(f"   {mark} {n.ljust(20)} {ui.dim(apis.CATALOG[n]['desc'][:46])}")
            shown += 1
    if not shown:
        ui.info("Aucune API ne correspond.")
    else:
        ui.out(ui.dim(f"  {shown} API · ● ok  ● en échec  · pas encore testée. /apitest pour vérifier."))


def apitest_cmd(name):
    names = [name] if name else list(apis.CATALOG)
    if name and name not in apis.CATALOG:
        ui.warn(apis.unknown_msg(name))
        return
    good = bad = 0
    for n in names:
        with ui.Spinner(n, color=ui.cyan):
            ok, note = apis.test(n)
        if ok is None:
            continue
        good, bad = good + bool(ok), bad + (not ok)
        (ui.ok if ok else ui.warn)(f"{n} : {note}")
    if len(names) > 1:
        ui.info(f"{good} ok, {bad} en échec. Les échecs sont désactivés pour toi seulement si tu les retires dans apis.local.json.")


def jobs_cmd(a):
    if a and a[0] in ("rm", "pause", "reprise", "lancer") and len(a) > 1 and a[1].isdigit():
        job = scheduler.get(int(a[1]))
        if not job:
            ui.warn("Tâche introuvable (voir /taches)")
        elif a[0] == "rm":
            scheduler.remove(job["id"])
            ui.ok("Supprimée")
        elif a[0] == "lancer":
            with ui.Spinner("Exécution"):
                out = scheduler.run_job(job)
            ui.sylvie(out)
        else:
            scheduler.set_enabled(job["id"], a[0] == "reprise")
            ui.ok("Reprise" if a[0] == "reprise" else "En pause")
        return
    rows = scheduler.jobs()
    if not rows:
        ui.info("Aucune tâche. Dis-moi par exemple : « rappelle-moi demain à 8h de… » ou tape /briefing 07:30")
    for j in rows:
        state = ui.green("●") if j["enabled"] else ui.dim("○")
        ui.out(f"  {state} {str(j['id']).rjust(2)} {ui.violet(j['kind'].ljust(12))} {j['title'][:28].ljust(28)} "
               f"{ui.dim(scheduler.describe_schedule(j['schedule']))}" + (f" · {ui.dim('prochaine ' + scheduler.fmt(j['next_run']))}" if j["enabled"] else ""))
        if j["last_result"]:
            ui.out("       " + ui.dim("→ " + j["last_result"][:90].replace("\n", " ")))


def rules_cmd(a):
    if a and a[0] == "aide":
        for k, d in [("/regles", "liste des règles"), ("/regles autoriser <outil> [site|dossier] [jours]", "ex : /regles autoriser read_url wikipedia.org 30"),
                     ("/regles rm N", "supprimer une règle")]:
            ui.out(f"  {ui.cyan(k.ljust(52))} {d}")
        ui.out(ui.dim("  Éligibles : " + ", ".join(sorted(rules.RULEABLE)) + ". Jamais après lecture de contenu externe."))
    elif a and a[0] == "autoriser" and len(a) > 1:
        tool = a[1]
        days = int(a[-1]) if len(a) > 2 and a[-1].isdigit() else 0
        value = " ".join(a[2:-1] if days else a[2:])
        try:
            rid = rules.add(tool, value, days)
            ui.ok(f"Règle n° {rid} : {rules.describe([r for r in rules.rules() if r['id'] == rid][0])}")
        except ValueError as e:
            ui.warn(str(e))
    elif a and a[0] == "rm" and len(a) > 1 and a[1].isdigit():
        ui.ok("Supprimée") if rules.remove(a[1]) else ui.warn("Règle introuvable")
    else:
        rs = rules.rules()
        for r in rs:
            ui.out(f"  {ui.violet(str(r['id']).rjust(3))}  {rules.describe(r)}")
        if not rs:
            ui.info("Aucune règle. Exemple : /regles autoriser read_url wikipedia.org")


def pending_cmd():
    items = scheduler.pending_list()
    if not items:
        ui.info("Rien en attente.")
    for p in items:
        ok = gate.ask(p["tool"], p["args"], p["risk"])
        scheduler.pending_close(p["id"], "acceptee" if ok else "refusee")
        if ok:
            try:
                res = engine.execute_approved(p["tool"], p["args"])
                ui.ok(str(res)[:200])
            except Exception as e:
                ui.error(str(e))


def ask_model(q, contents):
    CLI_CANCEL.clear()
    return engine.run(q, contents, CliIO(), cancel=CLI_CANCEL, learn=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve":   # sylvie serve : interface web + popup + tâches en arrière-plan
        sys.modules.setdefault("agent", sys.modules[__name__])
        import server
        return server.main()
    if os.environ.get("SYLVIE_SCHEDULER", "1") != "0":
        scheduler.start()   # les rappels et tâches partent aussi quand Sylvie est ouverte dans le terminal
    p = router.primary()
    ui.banner(f"assistante personnelle · {p['model'] if p else 'aucun modèle (voir /fournisseurs)'} · "
              f"{len(apis.CATALOG)} API · {memory.count()} souvenir(s) · /aide")
    with __import__("contextlib").suppress(Exception):
        if backup.auto():
            ui.out(ui.dim("  sauvegarde hebdomadaire faite"))
    contents = history.load("cli")
    if contents:
        ui.info(f"Conversation reprise ({len(contents)} messages) · /reset pour repartir de zéro")
    failed = None  # dernière question perdue sur une panne de l'API
    while True:
        try:
            q = ui.prompt().strip()
        except (EOFError, KeyboardInterrupt):
            ui.out()
            break
        voice.stop()  # une nouvelle saisie coupe la parole en cours
        if not q:
            continue
        if q.lower() in ("q", "quit", "exit", "/quit"):
            break
        if q.lower() in ("v", "/v"):
            q = hear()
            if not q:
                continue
            ui.out(f"  {ui.dim('🎙')} {ui.italic(q)}")
        if q.lower() == "/r":
            if not failed:
                ui.info("Rien à réessayer.")
                continue
            q = failed
            ui.out(f"  {ui.dim('↻')} {ui.italic(q)}")
        elif q.startswith("/") and command(q, contents):
            continue
        quick = fast_path(q)
        if quick:  # demande triviale : réponse locale, sans appel API
            say(quick)
            contents += [{"role": "user", "parts": [{"text": q}]}, {"role": "model", "parts": [{"text": quick}]}]
            history.save("cli", contents)
            continue
        try:
            failed = ask_model(q, contents)
            history.save("cli", contents)
        except KeyboardInterrupt:
            ui.warn("Interrompu")
    voice.stop()
    ui.out(ui.dim("  À bientôt 👋"))


if __name__ == "__main__":
    sys.exit(main() or 0)
