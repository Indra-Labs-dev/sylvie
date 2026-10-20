"""Client minimal de l'API Gemini (REST) avec reprise et modèle de secours."""
import os, time, requests

BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta/models/")
MODELS = [os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
          os.environ.get("GEMINI_FALLBACK", "gemini-3.5-flash-lite")]
THINKING = os.environ.get("SYLVIE_THINKING", "minimal")  # minimal | low | medium | high, "" = défaut du modèle
DEBUG = os.environ.get("SYLVIE_DEBUG") == "1"
HTTP = requests.Session()  # réutilise la connexion TLS entre les appels


def _key():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY absente : lance d'abord  . ./.env")
    return key


def generate(contents, system=None, tools=None, thinking=True):
    """Renvoie le premier candidat de la réponse (dict avec 'content', éventuellement 'groundingMetadata')."""
    body = {"contents": contents}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = tools
    if thinking and THINKING:
        body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": THINKING}}
    errors = {}
    for model in MODELS:
        for _ in range(2):
            t0 = time.time()
            try:
                r = HTTP.post(f"{BASE}{model}:generateContent", headers={"x-goog-api-key": _key()},
                              timeout=45, json=body)
            except requests.exceptions.RequestException as e:
                errors[model] = f"réseau ({e.__class__.__name__})"
                continue
            if DEBUG:
                print(f"\n[{model} {r.status_code} {time.time() - t0:.1f}s]")
            if r.ok:
                data = r.json()
                cands = data.get("candidates") or []
                if not cands or "content" not in cands[0]:
                    reason = (cands[0].get("finishReason") if cands else None) or \
                             data.get("promptFeedback", {}).get("blockReason", "réponse vide")
                    raise RuntimeError(f"Gemini n'a pas répondu ({reason}).")
                cands[0]["content"].setdefault("parts", [])
                cands[0]["model"] = model
                return cands[0]
            errors[model] = f"{r.status_code} {_short_error(r)}"
            if r.status_code not in (429, 500, 503):
                raise RuntimeError(f"{model} : {errors[model]}")  # erreur permanente (clé, modèle inconnu...)
            if r.status_code == 429 and "quota" in r.text.lower():
                break  # quota épuisé : inutile de réessayer ce modèle, on passe au suivant
            time.sleep(1)
    raise RuntimeError("Gemini indisponible : " + " ; ".join(f"{m} → {e}" for m, e in errors.items()))


def _short_error(r):
    try:
        msg = r.json()["error"]["message"]
    except Exception:
        msg = r.text
    if "quota" in msg.lower():
        return "quota dépassé (limite gratuite atteinte)"
    if "high demand" in msg.lower():
        return "surchargé"
    return msg.split(". ")[0][:120]


def text_of(content):
    return " ".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()
