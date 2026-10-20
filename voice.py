"""Voix via Termux:API : écoute (termux-speech-to-text) et parole (termux-tts-speak)."""
import os, re, subprocess, time

_proc = None

def listen(timeout=30):
    """Ouvre la reconnaissance vocale Android et renvoie le texte compris ('' si rien)."""
    try:
        r = subprocess.run(["termux-speech-to-text"], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return r.stdout.strip()

def record(path, limit=30):
    """Enregistre le micro dans `path` (AAC/m4a) jusqu'à Entrée ou `limit` secondes."""
    subprocess.run(["termux-microphone-record", "-q"], capture_output=True)  # coupe un éventuel enregistrement resté ouvert
    try:
        os.remove(path)
    except OSError:
        pass
    try:
        r = subprocess.run(["termux-microphone-record", "-f", path, "-e", "aac", "-r", "16000", "-c", "1",
                            "-l", str(limit)], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if r.returncode != 0:
        return False
    try:
        input(f"Parle, puis appuie sur Entrée ({limit} s max)... ")
    finally:
        subprocess.run(["termux-microphone-record", "-q"], capture_output=True)
    time.sleep(0.7)  # laisse le fichier se finaliser
    return os.path.exists(path) and os.path.getsize(path) > 1000

def clean(text):
    """Retire ce qui se lit mal à voix haute (markdown, liens)."""
    text = re.sub(r"https?://\S+", "lien", text)
    # opérateurs entre chiffres -> mots (sinon le nettoyage markdown efface le "*")
    for sym, mot in (("*", "fois"), ("/", "divisé par"), ("+", "plus"), ("=", "égale")):
        text = re.sub(rf"(?<=[\d)])\s*{re.escape(sym)}\s*(?=[\d(])", f" {mot} ", text)
    text = re.sub(r"(?<=\d)\s*=\s*", " égale ", text)
    text = re.sub(r"(?<=\d) - (?=\d)", " moins ", text)
    text = re.sub(r"(?m)^\s*[-•]\s+", "", text)  # puces de liste
    text = re.sub(r"[*_#`>|~]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def stop():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
    _proc = None

def speak(text, lang="fr"):
    """Lit le texte sans bloquer. Une nouvelle phrase interrompt la précédente."""
    global _proc
    stop()
    text = clean(text)
    if not text:
        return False
    try:
        _proc = subprocess.Popen(["termux-tts-speak", "-l", lang], stdin=subprocess.PIPE, text=True)
        _proc.stdin.write(text)
        _proc.stdin.close()
    except FileNotFoundError:
        _proc = None
        return False
    return True
