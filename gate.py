"""Porte de confirmation : rien de risqué ne s'exécute sans ton accord explicite.

Ce contrôle est dans le code, le modèle ne peut pas le contourner.
"""
import json, os, subprocess, time

import ui

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "actions.log")
DIALOG = os.environ.get("SYLVIE_DIALOG", "1") != "0"  # 0 = confirmation dans le terminal


def log(ev, name, args):
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%F %T')} {ev} {name} {json.dumps(args, ensure_ascii=False)}\n")


def last_actions(n=10):
    try:
        with open(LOG) as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def _dialog(kind, title, text):
    """termux-dialog ; renvoie la réponse, ou None si indisponible."""
    if not DIALOG:
        return None
    try:
        r = subprocess.run(["termux-dialog", kind, "-t", title, "-i", text],
                           capture_output=True, text=True, timeout=90)
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return {"code": -2, "text": ""}
    except Exception:
        return None


def ask(name, args, risk, tainted=False):
    lines = [f"{k} : {v}" for k, v in args.items()] or ["(sans paramètre)"]
    if tainted:
        lines.append("⚠ Du contenu externe (web/SMS) a été lu juste avant :")
        lines.append("  vérifie que c'est bien TOI qui veux cette action.")
    detail = "\n".join([f"Action : {name}"] + lines)
    sensitive = risk == "sensitive"
    title = "ACTION SENSIBLE : tape OUI" if sensitive else "Autoriser cette action ?"

    ui.box(title, [f"Action : {name}"] + lines, color=ui.red if sensitive or tainted else ui.yellow)
    res = _dialog("text" if sensitive else "confirm", title, detail)
    if res is None:  # pas de termux-dialog : confirmation dans le terminal
        answer = input(f"  {'Tape OUI pour confirmer' if sensitive else 'Autoriser ? (o/N)'} : ").strip()
        ok = answer == "OUI" if sensitive else answer.lower() in ("o", "oui", "y")
    elif sensitive:
        ok = res.get("code") == -1 and res.get("text", "").strip().upper() == "OUI"
    else:
        ok = res.get("text") == "yes"

    (ui.ok if ok else ui.warn)("Autorisé" if ok else "Refusé")
    log("ACCEPTE" if ok else "REFUSE", name, args)
    return ok
