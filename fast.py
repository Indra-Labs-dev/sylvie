"""Réponses locales instantanées pour les demandes triviales (sans appel API)."""
import re
from tools import get_time, calculate

_TIME = re.compile(r"\b(quelle|quel)\s+(heure|jour|date)\b|\bheure\s+est[- ]il\b|\bil est quelle heure\b", re.I)
_CALC = re.compile(r"^\s*(?:calcule|calcul|combien (?:font|fait|vaut))?\s*([\d\s.,+\-*/()%]+?)\s*\??\s*$", re.I)

def fast_path(q):
    """Renvoie une réponse (str) si la demande est triviale, sinon None."""
    if _TIME.search(q) and len(q) < 60:
        return f"Nous sommes le {get_time()}."
    m = _CALC.match(q)
    if m and re.search(r"\d\s*[+\-*/%]\s*\d|\d\s*[+\-*/%]\s*\(|\)\s*[+\-*/%]", m.group(1)):
        try:
            v = calculate(m.group(1).replace(",", ".").replace("%", "/100"))
            return f"{m.group(1).strip()} = {round(v, 10):g}"
        except Exception:
            return None
    return None
