"""Moteur d'API déclaratif : un catalogue JSON (apis.json) décrit chaque API, ce moteur les appelle.

Ajouter une API = ajouter une entrée dans apis.local.json (même format, jamais écrasé par les mises à jour).
Sécurité : HTTPS seulement, hôte figé par l'entrée du catalogue (jamais d'URL libre venue du modèle),
paramètres validés (type, motif, bornes), IP privées refusées, réponse plafonnée en taille.
"""
import html, ipaddress, json, math, os, re, sqlite3, time, unicodedata
from urllib.parse import quote, urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("SYLVIE_DB", os.path.join(HERE, "sylvie.db"))
UA = "Mozilla/5.0 (Linux; Android 16) Sylvie/1.0 (assistant personnel)"
MAX_BYTES = 3_000_000
DEFAULT_MAX_CHARS = 1800
DEFAULT_DAILY = 200

CATALOG = {}
_last_call = {}
_cache = {}


class ApiError(RuntimeError):
    pass


# ---------------------------------------------------------------- catalogue

def _host_regex(url):
    host = re.match(r"https://([^/?#]+)", url).group(1)
    return "^" + "".join("[a-z0-9-]{2,12}" if p.startswith("{") else re.escape(p)
                         for p in re.split(r"(\{\w+\})", host)) + "$"


def load():
    """(Re)charge apis.json puis apis.local.json (ajouts ou remplacements ; {"remove": [noms]} pour retirer)."""
    CATALOG.clear()
    for fname in ("apis.json", "apis.local.json"):
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise ApiError(f"{fname} illisible : {e}")
        for name in data.get("remove", []):
            CATALOG.pop(name, None)
        for e in data.get("apis", []):
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", e.get("name", "")):
                raise ApiError(f"{fname} : nom d'API invalide : {e.get('name')!r}")
            if not str(e.get("url", "")).startswith("https://"):
                raise ApiError(f"{fname} : {e['name']} doit utiliser https://")
            e.setdefault("params", {})
            e.setdefault("category", "Divers")
            e.setdefault("keywords", [])
            e["_host_re"] = _host_regex(e["url"])
            CATALOG[e["name"]] = e
    return CATALOG


def norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", str(s).lower()) if unicodedata.category(c) != "Mn")


def _optional(p):
    return bool(p.get("optional")) or "default" in p


def signature(e):
    return f"{e['name']}({', '.join(k + ('?' if _optional(p) else '') for k, p in e['params'].items())})"


def describe(name):
    e = CATALOG.get(name)
    if not e:
        raise ApiError(unknown_msg(name))
    return {"api": e["name"], "categorie": e["category"], "description": e["desc"], "signature": signature(e),
            "parametres": {k: {"type": p.get("type", "string"), "obligatoire": not _optional(p), "defaut": p.get("default"), "aide": p.get("desc", "")}
                           for k, p in e["params"].items()},
            "source": e.get("source", "")}


def unknown_msg(name):
    words = [w for w in re.split(r"\W+", norm(name)) if len(w) > 2]
    close = [n for n in CATALOG if any(w in n for w in words)][:5]
    return f"API inconnue : {name!r}." + (f" Proches : {', '.join(close)}." if close else " Voir la liste des API disponibles dans tes instructions.")


def names_by_category():
    cats = {}
    for e in CATALOG.values():
        cats.setdefault(e["category"], []).append(e["name"])
    return cats


def relevant(question, k=6):
    """Les k API les plus proches d'une question (mots-clés du catalogue), pour n'afficher que leurs signatures."""
    toks = {t for t in re.split(r"\W+", norm(question)) if len(t) >= 3}
    if not toks:
        return []
    scored = []
    for e in CATALOG.values():
        kws = {norm(w) for w in e["keywords"]}
        name_parts = set(e["name"].split("_"))
        desc_words = {w for w in re.split(r"\W+", norm(e["desc"])) if len(w) >= 4}
        s = 0
        for t in toks:
            if t in kws:
                s += 3
            elif len(t) >= 5 and any(k_.startswith(t[:5]) or t.startswith(k_[:5]) for k_ in kws if len(k_) >= 5):
                s += 2
            if t in name_parts:
                s += 2
            if t in desc_words:
                s += 1
        if s:
            scored.append((s, e["name"]))
    return [CATALOG[n] for _, n in sorted(scored, key=lambda x: (-x[0], x[1]))[:k]]


# ---------------------------------------------------------------- budget et santé

def _db():
    con = sqlite3.connect(DB, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS api_usage (api TEXT, day TEXT, calls INTEGER, PRIMARY KEY (api, day))")
    con.execute("CREATE TABLE IF NOT EXISTS api_health (api TEXT PRIMARY KEY, ok INTEGER, ts TEXT, note TEXT)")
    return con


def _count(name):
    day = time.strftime("%F")
    con = _db()
    try:
        r = con.execute("SELECT calls FROM api_usage WHERE api=? AND day=?", (name, day)).fetchone()
        return (r[0] if r else 0), day
    finally:
        con.close()


def _bump(name):
    n, day = _count(name)
    con = _db()
    try:
        con.execute("INSERT OR REPLACE INTO api_usage VALUES (?,?,?)", (name, day, n + 1))
        con.commit()
    finally:
        con.close()


def health():
    con = _db()
    try:
        return {r[0]: (bool(r[1]), r[2], r[3]) for r in con.execute("SELECT api, ok, ts, note FROM api_health")}
    finally:
        con.close()


def _set_health(name, ok, note):
    con = _db()
    try:
        con.execute("INSERT OR REPLACE INTO api_health VALUES (?,?,?,?)", (name, int(ok), time.strftime("%F %T"), note[:120]))
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- paramètres

def _coerce(name, spec, v):
    t = spec.get("type", "string")
    label = f"paramètre {name!r}"
    if isinstance(v, bool) or v is None:
        raise ApiError(f"{label} : valeur invalide")
    if t in ("number", "integer"):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise ApiError(f"{label} : nombre attendu, reçu {v!r}")
        if not math.isfinite(x):
            raise ApiError(f"{label} : nombre invalide")
        if t == "integer":
            if x != int(x):
                raise ApiError(f"{label} : entier attendu")
            x = int(x)
        if "min" in spec and x < spec["min"] or "max" in spec and x > spec["max"]:
            raise ApiError(f"{label} : hors limites ({spec.get('min')} à {spec.get('max')})")
        return x
    s = str(v).strip()
    if not s:
        raise ApiError(f"{label} : vide")
    if len(s) > spec.get("max_len", 300) or re.search(r"[\x00-\x1f\x7f]", s):
        raise ApiError(f"{label} : texte trop long ou invalide")
    if t == "ip":
        try:
            ip = ipaddress.ip_address(s)
        except ValueError:
            raise ApiError(f"{label} : adresse IP invalide")
        if not ip.is_global:
            raise ApiError(f"{label} : adresse privée ou réservée refusée (elle ne doit pas quitter ton réseau)")
        return str(ip)
    if "pattern" in spec and not re.fullmatch(spec["pattern"], s):
        raise ApiError(f"{label} : format invalide (attendu : {spec['pattern']})")
    if "enum" in spec and s not in spec["enum"]:
        raise ApiError(f"{label} : valeurs possibles {spec['enum']}")
    return s


def validate(e, raw):
    if not isinstance(raw, dict):
        raise ApiError("params doit être un objet JSON, ex {\"latitude\": 5.36}")
    unknown = [k for k in raw if k not in e["params"]]
    if unknown:
        raise ApiError(f"paramètre(s) inconnu(s) {unknown}. Signature : {signature(e)}")
    vals = {}
    for k, spec in e["params"].items():
        if k in raw and raw[k] not in (None, ""):
            vals[k] = _coerce(k, spec, raw[k])
        elif "default" in spec:
            vals[k] = spec["default"]
        elif not spec.get("optional"):
            raise ApiError(f"paramètre manquant : {k} ({spec.get('desc', '')}). Signature : {signature(e)}")
    return vals


# ---------------------------------------------------------------- extraction

def _walk(node, segs):
    if not segs:
        return node
    seg, rest = segs[0], segs[1:]
    many = seg.endswith("[]")
    key = seg[:-2] if many else seg
    if key:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if many:
        return [_walk(x, rest) for x in node] if isinstance(node, list) else None
    return _walk(node, rest)


def _path(p):
    return [s for s in p.split(".") if s] if p not in ("", None) else []


def _pick(node, keys):
    if not isinstance(node, dict):
        return node
    out = {}
    for k in keys:
        v = _walk(node, _path(k))
        name = k.split(".")[-1]
        if v is not None and name not in out:
            out[name] = v
    return out


def extract(data, spec):
    if spec is None:
        return data
    if isinstance(spec, list):
        out = {}
        for s in spec:
            v = extract(data, s)
            if v is not None:
                out[re.sub(r"\[\]", "", s.partition("|")[0]).split(".")[-1] or "data"] = v
        return out
    path, _, keys = spec.partition("|")
    node = _walk(data, _path(path))
    if node is None:
        return None
    ks = [k.strip() for k in keys.split(",") if k.strip()]
    if not ks:
        return node
    if isinstance(node, list):
        return [(_pick(x, ks) if isinstance(x, dict) else x) for x in node]
    return _pick(node, ks)


def _map_strings(x, fn):
    if isinstance(x, str):
        return fn(x)
    if isinstance(x, list):
        return [_map_strings(i, fn) for i in x]
    if isinstance(x, dict):
        return {k: _map_strings(v, fn) for k, v in x.items()}
    return x


def _search_list(data, cfg, vals):
    segs = _path(cfg.get("path", ""))
    lst = _walk(data, segs)
    if not isinstance(lst, list):
        return data
    lst = lst[cfg.get("skip", 0):]
    q = norm(vals.get(cfg.get("param", ""), "")) if cfg.get("param") else ""
    if q:
        def hay(x):
            parts = []
            for f in cfg.get("fields", []):
                v = x.get(f) if isinstance(x, dict) else None
                if v is not None:
                    parts.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
            return norm(" ".join(parts))
        words = q.split()
        lst = [x for x in lst if all(w in hay(x) for w in words)]
    if cfg.get("sort_desc"):
        lst = sorted(lst, key=lambda x: str(x.get(cfg["sort_desc"], "")), reverse=True)
    lst = lst[:cfg.get("limit", 8)]
    if not segs:
        return lst
    parent = _walk(data, segs[:-1])
    if isinstance(parent, dict):
        parent[segs[-1]] = lst
    return data


def _post(data, post, vals):
    if "filter_keys" in post:
        cfg = post["filter_keys"]
        wanted = {s.strip().upper() for s in str(vals.get(cfg["param"], "")).split(",") if s.strip()}
        node = data.get(cfg["path"]) if isinstance(data, dict) else None
        if wanted and isinstance(node, dict):
            data[cfg["path"]] = {k: v for k, v in node.items() if k in wanted}
    if post.get("strip_html"):
        data = _map_strings(data, lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)))
    if post.get("unescape"):
        data = _map_strings(data, html.unescape)
    if post.get("dedupe") and isinstance(data, list):
        seen, out = set(), []
        for x in data:
            key = json.dumps(x.get(post["dedupe"]) if isinstance(x, dict) else x, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                out.append(x)
        data = out
    if post.get("limit"):
        if isinstance(data, list):
            data = data[:post["limit"]]
        elif isinstance(data, dict):
            data = {k: (v[:post["limit"]] if isinstance(v, list) else v) for k, v in data.items()}
    return data


# ---------------------------------------------------------------- appel

class _Fmt(dict):
    def __missing__(self, k):
        return ""


def _fill(tpl, vals):
    """Remplit les {param} d'un modèle (str/dict/list) ; une clé dont la valeur est un seul {param} absent est retirée."""
    if isinstance(tpl, str):
        return tpl.format_map(_Fmt({k: v for k, v in vals.items()}))
    if isinstance(tpl, list):
        return [_fill(x, vals) for x in tpl]
    if isinstance(tpl, dict):
        out = {}
        for k, v in tpl.items():
            m = re.fullmatch(r"\{(\w+)\}", v) if isinstance(v, str) else None
            if m and m.group(1) not in vals:
                continue
            out[k] = _fill(v, vals)
        return out
    return tpl


def _throttle(e):
    gap = e.get("min_interval", 0)
    if not gap:
        return
    wait = _last_call.get(e["name"], 0) + gap - time.time()
    if wait > 0:
        if wait > 8:
            raise ApiError(f"{e['name']} limite la cadence : réessaie dans {int(wait)} s")
        time.sleep(wait)


def _fetch(e, url, query, body, form):
    headers = {"User-Agent": UA, "Accept": "application/json", **e.get("headers", {})}
    env = e.get("env_key")
    if env and os.environ.get(env):
        headers[e.get("key_header", "Authorization")] = e.get("key_format", "Bearer {key}").format(key=os.environ[env])
    try:
        r = requests.request(e.get("method", "GET"), url, params=query or None, json=body, data=form, headers=headers,
                             timeout=e.get("timeout", 20), stream=True)
    except requests.exceptions.Timeout:
        raise ApiError(f"{e['name']} : délai dépassé")
    except requests.exceptions.RequestException as ex:
        raise ApiError(f"{e['name']} : réseau ({ex.__class__.__name__})")
    host = urlparse(r.url).hostname or ""
    if not e.get("any_redirect") and not re.match(e["_host_re"], host):  # redirection hors de l'hôte prévu
        r.close()
        raise ApiError(f"{e['name']} : redirection vers {host} refusée")
    chunks, size = [], 0
    for c in r.iter_content(65536):
        chunks.append(c)
        size += len(c)
        if size > MAX_BYTES:
            break
    r.close()
    text = b"".join(chunks).decode("utf-8", "replace")
    if r.status_code >= 400:
        if r.status_code == 404:
            raise ApiError(f"{e['name']} : aucun résultat (404)")
        raise ApiError(f"{e['name']} : HTTP {r.status_code} {re.sub(r'<[^>]+>', ' ', text)[:120].strip()}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def call(name, params=None):
    e = CATALOG.get(name)
    if not e:
        raise ApiError(unknown_msg(name))
    if isinstance(params, str):
        try:
            params = json.loads(params) if params.strip() else {}
        except json.JSONDecodeError:
            raise ApiError("params : JSON invalide, ex {\"latitude\": 5.36, \"longitude\": -4.0}")
    vals = validate(e, params or {})
    used, _ = _count(name)
    if used >= e.get("calls_per_day", DEFAULT_DAILY):
        raise ApiError(f"budget du jour atteint pour {name} ({used} appels)")

    url = e["url"]
    for k in re.findall(r"\{(\w+)\}", url.split("?")[0]):
        if k not in vals:
            raise ApiError(f"paramètre manquant : {k}")
        url = url.replace("{" + k + "}", quote(str(vals[k]), safe=""))
    path_keys = set(re.findall(r"\{(\w+)\}", e["url"]))
    query = dict(e.get("fixed", {}))
    for k, spec in e["params"].items():
        if k in vals and k not in path_keys and spec.get("in") != "body":
            query[spec.get("query_name", k)] = vals[k]
    body = _fill(e["body"], vals) if "body" in e else None
    form = _fill(e["form"], vals) if "form" in e else None
    if not re.match(e["_host_re"], urlparse(url).hostname or ""):
        raise ApiError(f"{name} : hôte refusé")

    ckey = (name, json.dumps([url, query, body, form], sort_keys=True, default=str))
    ttl = e.get("cache_seconds", 0)
    if ttl and ckey in _cache and time.time() - _cache[ckey][0] < ttl:
        data = json.loads(json.dumps(_cache[ckey][1]))  # copie : les post-traitements modifient
    else:
        _throttle(e)
        _last_call[name] = time.time()
        data = _fetch(e, url, query, body, form)
        _bump(name)
        if ttl:
            _cache[ckey] = (time.time(), json.loads(json.dumps(data)))

    if isinstance(data, str):
        result = data.strip()[:e.get("max_chars", DEFAULT_MAX_CHARS)]
    else:
        if "pre" in e and "search_list" in e["pre"]:
            data = _search_list(data, e["pre"]["search_list"], vals)
        out = extract(data, e.get("extract"))
        out = _post(out, e.get("post", {}), vals) if out is not None else None
        if out in (None, [], {}):
            return {"api": name, "resultat": "aucun résultat"}
        result = out
        txt = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        limit = e.get("max_chars", DEFAULT_MAX_CHARS)
        if len(txt) > limit:
            result = txt[:limit] + "…(tronqué)"
    return {"api": name, "resultat": result}


def test(name):
    """Appelle l'API avec son exemple. Renvoie (ok, détail) et mémorise le résultat."""
    e = CATALOG[name]
    if "example" not in e:
        return None, "pas d'exemple défini"
    t0 = time.time()
    try:
        r = call(name, e["example"])
        ok = r["resultat"] != "aucun résultat"
        note = f"{time.time() - t0:.1f}s" if ok else "réponse vide"
    except ApiError as ex:
        ok, note = False, str(ex)
    _set_health(name, ok, note)
    return ok, note


load()
