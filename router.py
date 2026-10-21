"""Routeur de fournisseurs d'IA : plusieurs services, une seule interface, jamais de blocage sec.

- Chaque fournisseur (providers.json + providers.local.json) a des rôles, un niveau de confiance et un budget.
- Quota épuisé, surcharge, clé refusée : le fournisseur est mis en pause (état dans sylvie.db) et on passe au suivant.
- Confidentialité : une requête privée (mémoire, SMS, position...) n'est JAMAIS envoyée à un fournisseur « public ».
  Les tâches sur données publiques (résumé d'une page web...) vont d'abord aux fournisseurs « public ».
- Un seul compte et une seule clé par fournisseur : on répartit entre services, on ne contourne pas leurs quotas.
"""
import copy, json, os, re, sqlite3, time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("SYLVIE_DB", os.path.join(HERE, "sylvie.db"))
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta/models/")
THINKING = os.environ.get("SYLVIE_THINKING", "minimal")  # minimal | low | medium | high | "" (défaut du modèle)
DEBUG = os.environ.get("SYLVIE_DEBUG") == "1"
TIMEOUT = 45
HTTP = requests.Session()  # réutilise les connexions TLS


class ProviderError(Exception):
    def __init__(self, kind, short, cooldown=0):
        super().__init__(short)
        self.kind, self.short, self.cooldown = kind, short, cooldown


class NoProvider(RuntimeError):
    pass


# ---------------------------------------------------------------- configuration

def _read(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def providers():
    base = _read("providers.json").get("providers", [])
    local = _read("providers.local.json")
    by_id, order = {p["id"]: dict(p) for p in base}, [p["id"] for p in base]
    for pid, var in (("gemini-flash", "GEMINI_MODEL"), ("gemini-lite", "GEMINI_FALLBACK")):  # anciens réglages .env
        if os.environ.get(var) and pid in by_id:
            by_id[pid]["model"] = os.environ[var]
    for pid, ov in local.get("overrides", {}).items():
        if pid in by_id:
            by_id[pid].update(ov)
    for p in local.get("providers", []):
        if p["id"] not in by_id:
            order.append(p["id"])
        by_id[p["id"]] = dict(p)
    if local.get("order"):
        first = [i for i in local["order"] if i in by_id]
        order = first + [i for i in order if i not in first]
    return [by_id[i] for i in order]


def get(pid):
    return next((p for p in providers() if p["id"] == pid), None)


def save_local(fn):
    """Modifie providers.local.json : fn(dict) -> None (le fichier est créé si besoin)."""
    path = os.path.join(HERE, "providers.local.json")
    data = _read("providers.local.json")
    fn(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- état (pauses, budgets)

def _con():
    con = sqlite3.connect(DB, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS cooldown (provider TEXT PRIMARY KEY, until REAL, reason TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS usage (provider TEXT, day TEXT, calls INTEGER, PRIMARY KEY (provider, day))")
    return con


def _sql(q, args=(), fetch=None):
    con = _con()
    try:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
    finally:
        con.close()


def cooldown(pid):
    r = _sql("SELECT until, reason FROM cooldown WHERE provider=?", (pid,), "one")
    return r if r and r[0] > time.time() else None


def pause(pid, seconds, reason):
    if seconds > 0:
        _sql("INSERT OR REPLACE INTO cooldown VALUES (?,?,?)", (pid, time.time() + seconds, reason))


def reset_pauses():
    _sql("DELETE FROM cooldown")


def usage_today(pid):
    r = _sql("SELECT calls FROM usage WHERE provider=? AND day=?", (pid, time.strftime("%F")), "one")
    return r[0] if r else 0


def _bump(pid):
    _sql("INSERT OR REPLACE INTO usage VALUES (?,?,?)", (pid, time.strftime("%F"), usage_today(pid) + 1))


def status(p, private=True):
    """(utilisable, raison). Les raisons « clé absente » et « modèle à choisir » signalent un fournisseur non configuré."""
    if not os.environ.get(p.get("env", "")):
        return False, "clé absente"
    if not p.get("model"):
        return False, "modèle à choisir"
    if private and p.get("trust") != "private":
        return False, "non fiable pour les données privées"
    cd = cooldown(p["id"])
    if cd:
        return False, f"en pause {int((cd[0] - time.time()) / 60) + 1} min ({cd[1]})"
    lim = p.get("daily_calls", 0)
    if lim and usage_today(p["id"]) >= lim:
        return False, f"budget du jour atteint ({lim})"
    return True, "ok"


def primary():
    """Le fournisseur qui répondra à la prochaine requête privée de conversation (pour l'affichage)."""
    for p in providers():
        if "chat" in p.get("roles", []) and status(p, True)[0]:
            return p
    return None


# ---------------------------------------------------------------- erreurs

def _msg(r):
    try:
        return str(r.json()["error"]["message"])
    except Exception:
        return r.text[:200]


def _classify(r):
    code, msg = r.status_code, _msg(r)
    low = msg.lower()
    if code == 429:
        m = re.search(r'"retryDelay":\s*"(\d+)', r.text)
        ra = r.headers.get("retry-after", "")
        secs = int(m.group(1)) if m else int(ra) if ra.isdigit() else None
        if "perday" in r.text.lower().replace(" ", "") or secs is None or secs > 300:
            return ProviderError("quota", "429 quota dépassé (limite gratuite atteinte)", 3600)
        return ProviderError("rate", f"429 trop de requêtes (~{secs} s)", max(secs, 5))
    if code in (500, 502, 503, 504):
        return ProviderError("overload", f"{code} surchargé", 45)
    if code in (401, 403) or (code == 400 and "api key" in low and "not valid" in low):
        return ProviderError("auth", f"{code} clé refusée", 43200)
    if code == 404:
        return ProviderError("notfound", "404 modèle ou adresse introuvable", 43200)
    return ProviderError("bad", f"{code} {msg.split('. ')[0][:100]}", 0)


def _post(p, url, headers, body):
    err = None
    for _ in range(2):
        t0 = time.time()
        try:
            r = HTTP.post(url, headers=headers, json=body, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            err = ProviderError("network", f"réseau ({e.__class__.__name__})", 30)
            time.sleep(1)
            continue
        if DEBUG:
            print(f"\n[{p['id']} {p['model']} {r.status_code} {time.time() - t0:.1f}s]")
        if r.ok:
            return r
        err = _classify(r)
        if err.kind != "overload":
            raise err
        time.sleep(1)
    raise err


# ---------------------------------------------------------------- conversion d'historique

def _kind_of(pid):
    p = get(pid)
    return p["kind"] if p else "gemini"


def _prep_gemini(contents):
    """Copie propre pour Gemini. Les échanges d'outils produits par un autre type de fournisseur
    (sans signatures de réflexion) deviennent du texte, sinon Gemini 3 les rejetterait."""
    cs, foreign = copy.deepcopy(contents), False
    for c in cs:
        prov = c.pop("_provider", None)
        parts = c.get("parts", [])
        if c.get("role") == "model" and prov and _kind_of(prov) != "gemini":
            has = any("functionCall" in p for p in parts)
            c["parts"] = [{"text": f"[appel de l'outil {p['functionCall']['name']} {json.dumps(p['functionCall'].get('args', {}), ensure_ascii=False)}]"}
                          if "functionCall" in p else p for p in parts]
            foreign = has
        elif foreign and c.get("role") == "user" and any("functionResponse" in p for p in parts):
            c["parts"] = [{"text": f"[résultat de {p['functionResponse']['name']} : {json.dumps(p['functionResponse'].get('response'), ensure_ascii=False)[:6000]}]"}
                          if "functionResponse" in p else p for p in parts]
            foreign = False
        else:
            foreign = False
    return cs


def _lower_types(x):
    if isinstance(x, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _lower_types(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [_lower_types(i) for i in x]
    return x


def _to_openai(contents, system):
    msgs = [{"role": "system", "content": system}] if system else []
    pending, n = [], 0
    for c in contents:
        parts = [p for p in c.get("parts", []) if not p.get("thought")]
        if any("inlineData" in p for p in parts):
            raise ProviderError("bad", "audio non supporté par ce fournisseur", 0)
        if c["role"] == "model":
            calls = []
            for p in parts:
                if "functionCall" in p:
                    n += 1
                    cid = f"call_{n}"
                    pending.append((cid, p["functionCall"]["name"]))
                    calls.append({"id": cid, "type": "function", "function": {
                        "name": p["functionCall"]["name"], "arguments": json.dumps(p["functionCall"].get("args") or {}, ensure_ascii=False)}})
            m = {"role": "assistant", "content": " ".join(p["text"] for p in parts if "text" in p) or None}
            if calls:
                m["tool_calls"] = calls
            msgs.append(m)
        else:
            for p in parts:
                if "functionResponse" in p:
                    name = p["functionResponse"]["name"]
                    idx = next((i for i, (_, nm) in enumerate(pending) if nm == name), 0 if pending else None)
                    cid = pending.pop(idx)[0] if idx is not None else f"call_x{n}"
                    msgs.append({"role": "tool", "tool_call_id": cid,
                                 "content": json.dumps(p["functionResponse"].get("response"), ensure_ascii=False)[:8000]})
            text = " ".join(p["text"] for p in parts if "text" in p)
            if text:
                msgs.append({"role": "user", "content": text})
    return msgs


# ---------------------------------------------------------------- appels

def _call_gemini(p, contents, system, tools, thinking):
    body = {"contents": _prep_gemini(contents)}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = tools
    level = p.get("thinking", THINKING)
    if thinking and level:
        body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": level}}
    r = _post(p, f"{GEMINI_BASE}{p['model']}:generateContent", {"x-goog-api-key": os.environ[p["env"]]}, body)
    data = r.json()
    cands = data.get("candidates") or []
    if not cands or "content" not in cands[0]:
        reason = (cands[0].get("finishReason") if cands else None) or data.get("promptFeedback", {}).get("blockReason", "réponse vide")
        raise ProviderError("bad", f"pas de réponse ({reason})", 0)
    cand = cands[0]
    cand["content"].setdefault("parts", [])
    cand["content"]["role"] = "model"
    return cand


def _call_openai(p, contents, system, tools, thinking):
    body = {"model": p["model"], "messages": _to_openai(contents, system)}
    decls = [d for t in (tools or []) for d in t.get("functionDeclarations", [])]
    if decls:
        body["tools"] = [{"type": "function", "function": {"name": d["name"], "description": d.get("description", ""),
                                                           "parameters": _lower_types(d.get("parameters") or {"type": "OBJECT", "properties": {}})}} for d in decls]
    r = _post(p, p["base_url"].rstrip("/") + "/chat/completions", {"Authorization": "Bearer " + os.environ[p["env"]]}, body)
    choices = r.json().get("choices") or []
    m = (choices[0].get("message") if choices else None) or {}
    parts = [{"text": m["content"]}] if m.get("content") else []
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        parts.append({"functionCall": {"name": fn.get("name", ""), "args": args if isinstance(args, dict) else {}}})
    if not parts:
        raise ProviderError("bad", "réponse vide", 0)
    return {"content": {"role": "model", "parts": parts}}


def _call(p, contents, system, tools, thinking):
    return (_call_gemini if p["kind"] == "gemini" else _call_openai)(p, contents, system, tools, thinking)


def generate(role, contents, system=None, tools=None, private=True, thinking=True):
    """Premier fournisseur disponible pour ce rôle. Renvoie un candidat Gemini {"content", "provider", ...}."""
    provs = providers()
    if not private:  # données publiques : on ménage d'abord les fournisseurs de confiance
        provs = sorted(provs, key=lambda p: p.get("trust") == "private")
    wants_search = any("google_search" in t for t in (tools or []))
    errors, skipped = {}, {}
    for p in provs:
        if role not in p.get("roles", []):
            continue
        ok, why = status(p, private)
        if ok and wants_search and p["kind"] != "gemini":
            ok, why = False, "pas de recherche intégrée"
        if not ok:
            skipped[p["id"]] = why
            continue
        try:
            cand = _call(p, contents, system, tools, thinking)
        except ProviderError as e:
            errors[p["id"]] = e.short
            pause(p["id"], e.cooldown, e.short)
            continue
        _bump(p["id"])
        cand["provider"], cand["model"] = p["id"], p["model"]
        return cand
    noise = ("clé absente", "modèle à choisir")
    reasons = [f"{i} → {w}" for i, w in {**skipped, **errors}.items() if w not in noise]
    if not reasons:
        raise NoProvider(f"Aucun fournisseur configuré pour « {role} » : ajoute ta clé dans .env (voir /fournisseurs).")
    raise NoProvider("Aucun fournisseur disponible : " + " ; ".join(reasons))


def text_of(content):
    return " ".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()


# ---------------------------------------------------------------- outils d'administration

def table():
    rows = []
    for p in providers():
        ok, why = status(p, private=False)
        rows.append({"id": p["id"], "model": p.get("model") or "?", "kind": p["kind"], "roles": ",".join(p.get("roles", [])),
                     "trust": p.get("trust", "public"), "ok": ok, "why": why, "calls": usage_today(p["id"]), "limit": p.get("daily_calls", 0)})
    return rows


def list_models(pid):
    p = get(pid)
    if not p or not os.environ.get(p.get("env", "")):
        raise RuntimeError("fournisseur inconnu ou clé absente")
    key = os.environ[p["env"]]
    if p["kind"] == "gemini":
        r = HTTP.get(GEMINI_BASE.rstrip("/"), headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=20)
        ids = [m["name"].split("/")[-1] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])] if r.ok else []
    else:
        r = HTTP.get(p["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + key}, timeout=20)
        ids = [m.get("id", "") for m in r.json().get("data", [])] if r.ok else []
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} : {_msg(r)[:120]}")
    return sorted(ids)


def test(pid):
    """Un aller-retour minimal (ignore les pauses). Renvoie (ok, détail)."""
    p = get(pid)
    if not p:
        return False, "inconnu"
    if not os.environ.get(p.get("env", "")):
        return False, f"clé absente ({p.get('env')})"
    if not p.get("model"):
        return False, "modèle à choisir : /fournisseurs modeles " + pid
    t0 = time.time()
    try:
        cand = _call(p, [{"role": "user", "parts": [{"text": "Réponds uniquement : OK"}]}], None, None, False)
    except ProviderError as e:
        return False, e.short
    except Exception as e:
        return False, str(e)[:100]
    pause(pid, 0, "")
    _sql("DELETE FROM cooldown WHERE provider=?", (pid,))
    return True, f"{time.time() - t0:.1f}s · {text_of(cand['content'])[:40]}"
