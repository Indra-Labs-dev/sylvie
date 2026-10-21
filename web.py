"""Lecture sûre de pages web pour read_url : HTTP(S) public uniquement, redirections revérifiées, taille plafonnée."""
import html, ipaddress, re, socket
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (Linux; Android 16) Sylvie/1.0 (assistant personnel)"
MAX_BYTES = 1_500_000


class WebError(RuntimeError):
    pass


def _check_host(host):
    if not host:
        raise WebError("adresse invalide")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise WebError(f"domaine introuvable : {host}")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise WebError(f"adresse non publique refusée ({host}) : Sylvie ne lit pas ton réseau local")


def html_to_text(page):
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    page = re.sub(r"<(script|style|noscript|svg|nav|footer|header|form|iframe)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    page = re.sub(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section|article|table)\b[^>]*>", "\n", page, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines())
    return title, re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_text(url, max_chars=30000):
    """Renvoie (url_finale, titre, texte)."""
    cur = url.strip()
    for _ in range(5):
        u = urlparse(cur)
        if u.scheme not in ("http", "https"):
            raise WebError("seules les adresses http(s) sont autorisées")
        if u.username or u.password or u.port not in (None, 80, 443):
            raise WebError("adresse refusée (identifiants ou port inhabituel)")
        _check_host(u.hostname)
        try:
            r = requests.get(cur, headers={"User-Agent": UA, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"},
                             timeout=20, stream=True, allow_redirects=False)
        except requests.exceptions.RequestException as e:
            raise WebError(f"page injoignable ({e.__class__.__name__})")
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            cur = urljoin(cur, r.headers["location"])
            r.close()
            continue
        break
    else:
        raise WebError("trop de redirections")
    if r.status_code >= 400:
        r.close()
        raise WebError(f"la page répond HTTP {r.status_code}")
    ctype = r.headers.get("content-type", "")
    if not re.search(r"text/|html|json|xml", ctype, re.I):
        r.close()
        raise WebError(f"type non lisible ({ctype or 'inconnu'})")
    chunks, size = [], 0
    for c in r.iter_content(65536):
        chunks.append(c)
        size += len(c)
        if size > MAX_BYTES:
            break
    r.close()
    body = b"".join(chunks).decode(r.encoding or "utf-8", "replace")
    title, text = html_to_text(body) if "html" in ctype.lower() else ("", body.strip())
    if not text:
        raise WebError("page vide (contenu chargé par JavaScript ?)")
    return cur, title, text[:max_chars]
