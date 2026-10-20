import json, subprocess, time

def log(ev, name, args):
    with open("actions.log", "a") as f:
        f.write(f"{time.strftime('%F %T')} {ev} {name} {json.dumps(args, ensure_ascii=False)}\n")

def ask(name, args):
    txt = f"{name}\n{json.dumps(args, ensure_ascii=False, indent=1)}"
    try:
        out = subprocess.run(["termux-dialog", "confirm", "-t", "Autoriser cette action ?", "-i", txt],
                             capture_output=True, text=True, timeout=60).stdout
        ok = json.loads(out).get("text") == "yes"
    except subprocess.TimeoutExpired:
        ok = False
    except Exception:
        ok = input(f"Autoriser {txt} ? (o/N) ").lower() == "o"
    log("ACCEPTE" if ok else "REFUSE", name, args)
    return ok
