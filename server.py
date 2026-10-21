"""Serveur web local de Sylvie : interface de chat, approbations, tâches, objectifs. Lancer : sylvie serve

Sécurité (appliquée ici, pas par le modèle) :
  - le serveur n'écoute que sur 127.0.0.1 : rien n'est joignable depuis le Wi-Fi ;
  - un jeton secret (.token) est exigé pour tout, en cookie HttpOnly SameSite=Strict ou en en-tête ;
  - l'en-tête Host est vérifié (contre le DNS rebinding) et, pour les envois, Origin / Sec-Fetch-Site (contre le CSRF) ;
  - les autorisations d'actions se font dans l'interface : SMS et appels exigent de taper OUI.
"""
import contextlib, hmac, json, os, re, secrets, struct, sys, threading, time, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import agenda, agent, apis, backup, diagnostic, engine, files, gate, history, memory, planner, router, rules, scheduler, tools, voice
from fast import fast_path

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SYLVIE_PORT", "8765"))
TOKEN_FILE = os.path.join(HERE, ".token")
UI_DIR = os.path.join(HERE, "web_ui")
APPROVAL_TIMEOUT = 180
MAX_BODY = 20_000
MAX_UPLOAD = 11_000_000   # ~8 Mo de fichier une fois décodé
UPLOAD_EXT = files.TEXT_EXT | files.IMAGE_EXT | {".pdf", ".docx", ".xlsx", ".pptx"}


# ------------------------------------------------------------------ bus d'événements (SSE)

class Hub:
    def __init__(self, keep=400):
        self.cv, self.events, self.next, self.keep = threading.Condition(), [], 1, keep

    def publish(self, type_, **data):
        with self.cv:
            ev = {"id": self.next, "type": type_, "ts": time.time(), **data}
            self.next += 1
            self.events.append(ev)
            del self.events[:-self.keep]
            self.cv.notify_all()
        return ev

    def since(self, last, timeout=15):
        with self.cv:
            if not [e for e in self.events if e["id"] > last]:
                self.cv.wait(timeout)
            return [e for e in self.events if e["id"] > last]


hub = Hub()
scheduler.LISTENERS.append(lambda ev: hub.publish("notify", title=ev.get("title", ""), text=ev.get("text", "")) if ev.get("type") == "notify" else None)


# ------------------------------------------------------------------ état de la session web

class Web:
    contents = history.load("web")
    lock = threading.Lock()          # un seul tour de conversation à la fois
    cancel = threading.Event()
    failed = None
    speak = os.environ.get("SYLVIE_SPEAK") == "1"
    mic = None                       # Event d'arrêt de l'enregistrement en cours
    approvals = {}                   # id -> demande en attente d'une réponse dans l'interface


def token():
    if not os.path.exists(TOKEN_FILE):
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24))
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def web_approve(name, args, risk, tainted, timeout=APPROVAL_TIMEOUT):
    aid = secrets.token_hex(4)
    hint = rules.suggest(name, args) if risk == "confirm" and not tainted and isinstance(args, dict) else None
    req = {"aid": aid, "name": name, "args": args, "risk": risk, "tainted": bool(tainted), "event": threading.Event(), "ok": False, "hint": hint}
    Web.approvals[aid] = req
    hub.publish("approval", aid=aid, name=name, args=args, risk=risk, tainted=bool(tainted), ruleable=hint is not None, rule_hint=hint or "")
    with contextlib.suppress(Exception):
        tools.termux("termux-notification", "-t", "Sylvie attend ton accord", "-c", f"Action : {name}", "--id", "sylvie-ask",
                     "--priority", "high", timeout=8)
    req["event"].wait(timeout)
    ok = bool(req["ok"])
    Web.approvals.pop(aid, None)
    hub.publish("approval_done", aid=aid, ok=ok)
    gate.log("ACCEPTE" if ok else "REFUSE", name + " (web)", args)
    return ok


def resolve_approval(aid, ok, text="", always=False):
    req = Web.approvals.get(aid)
    if not req:
        return False
    if ok and always and req.get("hint") is not None and req["risk"] == "confirm" and not req["tainted"]:
        try:   # « toujours autoriser » : règle limitée au site ou au dossier de cet appel
            rid = rules.add(req["name"], req["hint"])
            hub.publish("info", level="ok", text=f"Règle n° {rid} créée : {rules.describe([r for r in rules.rules() if r['id'] == rid][0])}")
        except ValueError as e:
            hub.publish("info", level="error", text=str(e))
    if ok and req["risk"] == "sensitive" and text.strip() != "OUI":
        ok = False   # action sensible : il faut taper OUI
    req["ok"] = ok
    req["event"].set()
    return True


class WebIO(engine.IO):
    def tool(self, name, risk, args): hub.publish("tool", name=name, risk=risk, args=args)
    def approve(self, name, args, risk, tainted): return web_approve(name, args, risk, tainted)
    def approve_plan(self, goal, plan):
        return web_approve("plan", {"objectif": goal, "étapes": planner.plan_lines(plan)}, "confirm", False)
    def info(self, msg): hub.publish("info", level="info", text=msg)
    def ok(self, msg): hub.publish("info", level="ok", text=msg)
    def error(self, msg): hub.publish("info", level="error", text=msg)
    def progress(self, msg): hub.publish("info", level="progress", text=msg)
    def image(self, data_url): hub.publish("image", src=data_url)

    def say(self, text):
        hub.publish("say", text=text)
        if Web.speak:
            voice.speak(text)

    @contextlib.contextmanager
    def busy(self, label):
        hub.publish("busy", label=label)
        try:
            yield
        finally:
            hub.publish("busy", label=None)


def start_turn(text):
    """Lance un tour en arrière-plan. Renvoie False si Sylvie est déjà occupée."""
    if not Web.lock.acquire(blocking=False):
        return False
    Web.cancel.clear()

    def work():
        io = WebIO()
        try:
            hub.publish("user", text=text)
            low = text.strip().lower()
            if low.startswith("/objectif "):
                io.say(planner.run_goal(text.strip()[10:], io, cancel=Web.cancel))
            elif low == "/r":
                q, Web.failed = Web.failed, None
                if q:
                    Web.failed = engine.run(q, Web.contents, io, cancel=Web.cancel)
                else:
                    io.info("Rien à réessayer.")
            elif low == "/reset":
                Web.contents.clear()
                history.clear("web")
                io.ok("Nouvelle conversation")
            elif (quick := fast_path(text)):
                io.say(quick)
                Web.contents += [{"role": "user", "parts": [{"text": text}]}, {"role": "model", "parts": [{"text": quick}]}]
            else:
                Web.failed = engine.run(text, Web.contents, io, cancel=Web.cancel, learn=True)
        except Exception as e:  # le serveur ne doit jamais tomber sur une erreur de tour
            io.error(str(e))
        finally:
            with contextlib.suppress(Exception):
                history.save("web", Web.contents)
            Web.lock.release()
            hub.publish("idle")

    threading.Thread(target=work, daemon=True, name="sylvie-turn").start()
    return True


def mic_toggle():
    """Premier appel : démarre l'enregistrement. Second appel : l'arrête, transcrit et envoie à Sylvie."""
    if Web.mic is not None:
        Web.mic.set()
        return "stop"
    if Web.lock.locked():
        return "busy"
    stop = Web.mic = threading.Event()
    hub.publish("mic", on=True)

    def work():
        try:
            if not voice.record(agent.REC_PATH, limit=30, wait=lambda: stop.wait(30)):
                hub.publish("info", level="error", text="Enregistrement impossible (permission Microphone de Termux:API ?)")
                return
            hub.publish("busy", label="Transcription")
            try:
                text = agent.transcribe(agent.REC_PATH)
            finally:
                hub.publish("busy", label=None)
                with contextlib.suppress(OSError):
                    os.remove(agent.REC_PATH)
            if text:
                start_turn(text)
            else:
                hub.publish("info", level="error", text="Je n'ai rien compris.")
        except Exception as e:
            hub.publish("info", level="error", text=str(e))
        finally:
            Web.mic = None
            hub.publish("mic", on=False)

    threading.Thread(target=work, daemon=True, name="sylvie-mic").start()
    return "start"


def save_upload(name, b64):
    """Dépose un fichier envoyé depuis l'interface dans le dossier inbox de Sylvie."""
    import base64, binascii
    base = re.sub(r"[^\w.\- ]", "_", os.path.basename(str(name or "").replace("\\", "/"))).strip(" .")[:80]
    ext = os.path.splitext(base)[1].lower()
    if not base or ext not in UPLOAD_EXT:
        raise ValueError("type de fichier non pris en charge")
    try:
        raw = base64.b64decode(str(b64 or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("fichier illisible")
    if not raw or len(raw) > 8_000_000:
        raise ValueError("fichier vide ou trop gros (8 Mo maximum)")
    target = os.path.join(files.inbox_dir(), base)
    if os.path.exists(target):
        target = os.path.join(files.inbox_dir(), time.strftime("%H%M%S-") + base)
    with open(target, "wb") as f:
        f.write(raw)
    gate.log("DEPOT", os.path.basename(target), {"octets": len(raw)})
    return {"path": "inbox/" + os.path.basename(target), "octets": len(raw)}


def start_goal(goal):
    return start_turn("/objectif " + goal)


def agenda_week():
    evs = agenda.agenda_list(7)
    return evs if isinstance(evs, list) else []


def state():
    return {
        "busy": Web.lock.locked(), "speak": Web.speak, "api_names": sorted(apis.CATALOG), "mic": Web.mic is not None, "memory": memory.count(), "apis": len(apis.CATALOG),
        "providers": router.table(),
        "jobs": [{"id": j["id"], "kind": j["kind"], "title": j["title"], "when": scheduler.describe_schedule(j["schedule"]),
                  "next": scheduler.fmt(j["next_run"]) if j["enabled"] else "", "enabled": j["enabled"], "last": j["last_result"][:200]}
                 for j in scheduler.jobs()],
        "pending": scheduler.pending_list(),
        "rules": [{"id": r["id"], "text": rules.describe(r)} for r in rules.rules()],
        "agenda": agenda_week(),
        "goals": [{k: g[k] for k in ("id", "goal", "status", "result", "done", "calls")} | {"steps": len((g["plan"] or {}).get("steps", []))}
                  for g in planner.goals(8)],
    }


def run_pending(pid, ok, text):
    p = scheduler.pending_get(pid)
    if not p or p["status"] != "attente":
        return {"error": "demande introuvable ou déjà traitée"}
    if ok and p["risk"] == "sensitive" and text.strip() != "OUI":
        ok = False
    if not ok:
        scheduler.pending_close(pid, "refusee")
        gate.log("REFUSE", p["tool"] + " (en attente)", p["args"])
        return {"status": "refusee"}
    scheduler.pending_close(pid, "acceptee")
    gate.log("ACCEPTE", p["tool"] + " (en attente)", p["args"])
    try:
        res = engine.execute_approved(p["tool"], p["args"])
        return {"status": "acceptee", "result": res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)[:600]}
    except Exception as e:
        return {"status": "acceptee", "error": str(e)}


# ------------------------------------------------------------------ icônes (PNG générés, sans dépendance)

_icons = {}


def png_icon(n):
    if n in _icons:
        return _icons[n]
    c, r = n / 2, n * 0.34
    rows = []
    for y in range(n):
        row = bytearray([0])
        for x in range(n):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d < r:
                t = y / n
                px = (int(150 + 70 * t), int(110 + 50 * t), 255, 255)
            elif d < r + n * 0.03:
                px = (110, 80, 200, 255)
            else:
                px = (22, 18, 38, 255)
            row += bytes(px)
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    _icons[n] = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))
    return _icons[n]


MANIFEST = {
    "name": "Sylvie", "short_name": "Sylvie", "description": "Assistante personnelle", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#16121f", "theme_color": "#16121f", "orientation": "portrait",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
}
SW = "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',()=>{});"
PUBLIC = {"/manifest.webmanifest": ("application/manifest+json", lambda: json.dumps(MANIFEST).encode()),
          "/sw.js": ("text/javascript", lambda: SW.encode()),
          "/icon-192.png": ("image/png", lambda: png_icon(192)), "/icon-512.png": ("image/png", lambda: png_icon(512))}


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Sylvie"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # --- outils
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _authed(self):
        got = self.headers.get("X-Sylvie-Token", "")
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sylvie_t":
                got = got or v
        return bool(got) and hmac.compare_digest(got, self.server.token)

    def _body(self, limit=MAX_BODY):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("requête trop grande")
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # --- GET
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "hôte refusé"})
        u = urlparse(self.path)
        if u.path in PUBLIC:
            ctype, fn = PUBLIC[u.path]
            return self._send(200, fn(), ctype)
        qs = parse_qs(u.query)
        if "t" in qs:   # lien d'ouverture : pose le cookie puis retire le jeton de l'adresse
            if hmac.compare_digest(qs["t"][0], self.server.token):
                return self._send(302, b"", "text/plain", {"Location": "/mini" if u.path == "/mini" else "/", "Set-Cookie":
                                  f"sylvie_t={self.server.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"})
            return self._send(401, "Jeton invalide.", "text/plain")
        if not self._authed():
            return self._send(401, "Accès refusé. Ouvre Sylvie avec le lien affiché par « sylvie serve » dans Termux.", "text/plain")
        if u.path in ("/", "/mini", "/index.html"):
            try:
                with open(os.path.join(UI_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html")
            except OSError:
                return self._send(500, "interface introuvable (dossier web_ui)", "text/plain")
        if u.path == "/api/state":
            return self._send(200, state())
        if u.path == "/api/events":
            return self._events(int((qs.get("last") or [self.headers.get("Last-Event-ID") or "0"])[0] or 0))
        self._send(404, {"error": "introuvable"})

    def _events(self, last):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                evs = hub.since(last, 15)
                if not evs:
                    self.wfile.write(b": ping\n\n")
                for e in evs:
                    last = e["id"]
                    self.wfile.write(f"id: {e['id']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # --- POST
    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "hôte refusé"})
        if not self._authed():
            return self._send(401, {"error": "non autorisé"})
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._send(403, {"error": "origine refusée"})
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return self._send(403, {"error": "requête externe refusée"})
        try:
            path = urlparse(self.path).path
            code, out = self.route(path, self._body(MAX_UPLOAD if path == "/api/upload" else MAX_BODY))
        except (ValueError, json.JSONDecodeError) as e:
            code, out = 400, {"error": str(e)}
        except Exception as e:
            code, out = 500, {"error": str(e)}
        self._send(code, out)

    def route(self, path, d):
        if path == "/api/chat":
            text = str(d.get("text", "")).strip()[:4000]
            if not text:
                raise ValueError("message vide")
            return (200, {"ok": True}) if start_turn(text) else (409, {"error": "Sylvie est occupée, attends sa réponse ou arrête-la."})
        if path == "/api/goal":
            goal = str(d.get("goal", "")).strip()
            if not goal:
                raise ValueError("objectif vide")
            return (200, {"ok": True}) if start_goal(goal) else (409, {"error": "Sylvie est occupée."})
        if path == "/api/stop":
            Web.cancel.set()
            if Web.mic:
                Web.mic.set()
            for a in list(Web.approvals):
                resolve_approval(a, False)
            hub.publish("info", level="info", text="Arrêt demandé")
            return 200, {"ok": True}
        if path == "/api/approve":
            return (200, {"ok": True}) if resolve_approval(str(d.get("aid")), bool(d.get("ok")), str(d.get("text", "")), bool(d.get("always"))) else (404, {"error": "demande expirée"})
        if path == "/api/pending":
            return 200, run_pending(int(d.get("id")), bool(d.get("ok")), str(d.get("text", "")))
        if path == "/api/mic":
            return 200, {"result": mic_toggle()}
        if path == "/api/speak":
            Web.speak = bool(d.get("on"))
            if not Web.speak:
                voice.stop()
            return 200, {"speak": Web.speak}
        if path == "/api/rule":
            if d.get("action") != "delete":
                raise ValueError("action inconnue")   # une règle se crée depuis une demande d'accord ou dans le terminal, pas en un clic aveugle
            return (200, {"ok": True}) if rules.remove(int(d.get("id"))) else (404, {"error": "règle introuvable"})
        if path == "/api/diagnostic":
            items = diagnostic.run(port=PORT)
            return 200, {"items": [{"level": a, "label": b, "hint": c} for a, b, c in items], "summary": diagnostic.summary(items)}
        if path == "/api/backup":
            p = backup.create()
            return 200, {"path": p, "backups": backup.list_backups()[:5]}
        if path == "/api/upload":
            return 200, save_upload(d.get("name"), d.get("data"))
        if path == "/api/job/add":
            kw = {k: d.get(k, "") for k in ("kind", "title", "text", "at", "daily", "weekdays", "tools_list", "api", "params")}
            kw["every_minutes"] = int(d.get("every_minutes") or 0)
            if isinstance(kw["params"], dict):
                kw["params"] = json.dumps(kw["params"])
            return 200, scheduler.schedule_add(**kw)
        if path == "/api/job":
            jid, act = int(d.get("id")), d.get("action")
            job = scheduler.get(jid)
            if not job:
                return 404, {"error": "tâche introuvable"}
            if act == "delete":
                scheduler.remove(jid)
            elif act in ("on", "off"):
                scheduler.set_enabled(jid, act == "on")
            elif act == "run":
                threading.Thread(target=scheduler.run_job, args=(job,), daemon=True).start()
            else:
                raise ValueError("action inconnue")
            gate.log("TACHE-" + act.upper(), str(jid), {})
            return 200, {"ok": True}
        return 404, {"error": "introuvable"}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, tok):
        super().__init__(addr, handler)
        self.token = tok

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return   # le navigateur a fermé la page : rien d'anormal
        super().handle_error(request, client_address)


def quick_actions(url_base, tok):
    """Notification permanente : accès rapide (ouvrir, parler, stop) sans ouvrir Termux."""
    curl = f"curl -s -X POST -H 'X-Sylvie-Token: {tok}' -H 'Content-Type: application/json' -d '{{}}' http://127.0.0.1:{PORT}/api"
    try:
        tools.termux("termux-notification", "--id", "sylvie-live", "--ongoing", "-t", "Sylvie", "-c", "Prête · touche pour ouvrir",
                     "--priority", "low", "--group", "sylvie-live",
                     "--action", f"termux-open-url '{url_base}/?t={tok}'",
                     "--button1", "Parler", "--button1-action", curl + "/mic",
                     "--button2", "Mini", "--button2-action", f"termux-open-url '{url_base}/mini?t={tok}'",
                     "--button3", "Stop", "--button3-action", curl + "/stop", timeout=10)
    except Exception:
        pass


def main():
    tok = token()
    try:
        httpd = Server(("127.0.0.1", PORT), Handler, tok)
    except OSError as e:
        print(f"Port {PORT} indisponible ({e}). Sylvie serve tourne peut-être déjà ; sinon change SYLVIE_PORT.")
        return 1
    base = f"http://127.0.0.1:{PORT}"
    for cmd in (["termux-wake-lock"],):   # garde le téléphone assez éveillé pour que les tâches s'exécutent
        with contextlib.suppress(Exception):
            tools.termux(*cmd, timeout=5)
    scheduler.start()
    with contextlib.suppress(Exception):
        backup.auto()
    quick_actions(base, tok)
    print(f"Sylvie écoute sur {base}  (visible seulement depuis ce téléphone)")
    print(f"Lien d'ouverture : {base}/?t={tok}")
    if os.environ.get("SYLVIE_OPEN", "1") != "0":
        with contextlib.suppress(Exception):
            tools.termux("termux-open-url", f"{base}/?t={tok}", timeout=8)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for cmd in (["termux-notification-remove", "sylvie-live"], ["termux-wake-unlock"]):
            with contextlib.suppress(Exception):
                tools.termux(*cmd, timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
