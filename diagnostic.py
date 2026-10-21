"""/diagnostic : vérifie tout ce dont Sylvie dépend et dit quoi faire quand quelque chose manque."""
import os, shutil, socket, subprocess, sys

import paths

OK, WARN, BAD = "ok", "warn", "bad"
TERMUX_TOOLS = {
    "termux-notification": "notifications, rappels", "termux-battery-status": "batterie", "termux-location": "position",
    "termux-microphone-record": "micro", "termux-camera-photo": "caméra", "termux-tts-speak": "voix", "termux-dialog": "confirmations",
    "termux-wake-lock": "tâches écran éteint", "termux-open-url": "ouvrir des pages", "termux-contact-list": "contacts",
    "termux-sms-send": "SMS", "termux-clipboard-get": "presse-papiers", "am": "agenda du téléphone"}


def run(online=True, port=None):
    out = []
    add = lambda lvl, label, hint="": out.append((lvl, label, hint))

    v = sys.version_info
    add(OK if v >= (3, 9) else BAD, f"Python {v.major}.{v.minor}", "" if v >= (3, 9) else "Python 3.9 ou plus requis : pkg upgrade python")
    try:
        import requests  # noqa: F401
        add(OK, "requests")
    except ImportError:
        add(BAD, "requests manquant", "python -m pip install requests")
    for mod, why, cmd in (("pypdf", "lire les PDF", "pip install pypdf"), ("PIL", "réduire les photos", "pkg install python-pillow")):
        try:
            __import__(mod)
            add(OK, mod)
        except ImportError:
            add(WARN, f"{mod} absent : {why}", cmd)

    key = os.environ.get("GEMINI_API_KEY")
    add(OK if key else BAD, "clé GEMINI_API_KEY" if key else "clé GEMINI_API_KEY absente", "" if key else "nano ~/agent/.env puis relance Sylvie")
    try:
        import router
        usable = [r for r in router.table() if r["ok"]]
        add(OK if usable else BAD, f"{len(usable)} fournisseur(s) d'IA utilisable(s)" + (": " + ", ".join(r["id"] for r in usable) if usable else ""),
            "" if usable else "voir /fournisseurs : quota atteint ou clé absente")
        paused = [r for r in router.table() if "pause" in r["why"]]
        for r in paused:
            add(WARN, f"{r['id']} en pause", r["why"])
    except Exception as e:
        add(BAD, "routeur d'IA", str(e)[:100])

    missing = [f"{b} ({why})" for b, why in TERMUX_TOOLS.items() if not shutil.which(b)]
    if missing:
        add(WARN, "outils Termux:API manquants", "pkg install termux-api ; absent : " + ", ".join(missing[:6]))
    elif shutil.which("termux-battery-status"):
        try:
            r = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=8)
            add(OK if r.returncode == 0 and r.stdout.strip() else WARN, "appli Termux:API répond" if r.returncode == 0 and r.stdout.strip() else "l'appli Termux:API ne répond pas",
                "" if r.returncode == 0 and r.stdout.strip() else "installe/ouvre l'appli Termux:API (F-Droid) et donne-lui les permissions")
        except subprocess.TimeoutExpired:
            add(WARN, "Termux:API ne répond pas (8 s)", "ouvre l'appli Termux:API une fois, désactive l'optimisation de batterie")
        except Exception as e:
            add(WARN, "Termux:API", str(e)[:100])

    shared = os.path.join(os.path.expanduser("~"), "storage", "shared")
    add(OK if os.path.isdir(shared) else WARN, "stockage du téléphone accessible" if os.path.isdir(shared) else "stockage non autorisé",
        "" if os.path.isdir(shared) else "termux-setup-storage (Documents et Download seront alors lisibles)")
    try:
        import files
        add(OK, "dossiers autorisés : " + ", ".join(files.roots()) if files.roots() else "aucun dossier autorisé")
    except Exception as e:
        add(WARN, "dossiers autorisés", str(e)[:100])

    try:
        import sqlite3
        con = sqlite3.connect(paths.db(), timeout=3)
        con.execute("CREATE TABLE IF NOT EXISTS _diag (x)")
        con.execute("DROP TABLE _diag")
        con.close()
        add(OK, f"base de données ({os.path.getsize(paths.db()) // 1024} Ko)")
    except Exception as e:
        add(BAD, "base de données inaccessible", str(e)[:100])
    try:
        import scheduler
        n = len([j for j in scheduler.jobs() if j["enabled"]])
        alive = scheduler._instance is not None and scheduler._instance.is_alive()
        add(OK if alive else WARN, f"planificateur {'actif' if alive else 'arrêté'} · {n} tâche(s) active(s)",
            "" if alive else "les tâches ne partent que si Sylvie est ouverte (terminal ou « sylvie serve »)")
    except Exception as e:
        add(WARN, "planificateur", str(e)[:100])

    free = shutil.disk_usage(os.path.expanduser("~")).free // 1_000_000
    add(OK if free > 300 else WARN, f"espace libre : {free} Mo", "" if free > 300 else "libère de la place (pkg clean)")
    if port:
        with socket.socket() as s:
            s.settimeout(0.5)
            used = s.connect_ex(("127.0.0.1", port)) == 0
        add(OK, f"port {port} " + ("occupé (Sylvie serve tourne)" if used else "libre"))
    add(WARN, "batterie : vérifie à la main", "Paramètres › Batterie › Termux › Aucune restriction, verrouille Termux dans les récents (sinon HyperOS le coupe)")
    if online:
        try:
            import requests
            r = requests.get("https://generativelanguage.googleapis.com/", timeout=6)
            add(OK, f"Internet et API Gemini joignables (HTTP {r.status_code})")
        except Exception as e:
            add(BAD, "API Gemini injoignable", f"{e.__class__.__name__} : vérifie la connexion")
    return out


def summary(items):
    bad = sum(1 for lvl, _, _ in items if lvl == BAD)
    warn = sum(1 for lvl, _, _ in items if lvl == WARN)
    return f"{len(items) - bad - warn} ok, {warn} à surveiller, {bad} à corriger"
