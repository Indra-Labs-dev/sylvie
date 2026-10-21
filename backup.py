"""Sauvegarde et restauration de la mémoire, de l'agenda, des tâches et des réglages locaux (jamais .env ni le jeton)."""
import contextlib, os, sqlite3, tempfile, time, zipfile

import paths

KEEP = 7
EXTRA = ("providers.local.json", "apis.local.json")


def _list():
    d = paths.backup_dir()
    try:
        return sorted(n for n in os.listdir(d) if n.startswith("sylvie-") and n.endswith(".zip"))
    except OSError:
        return []


def list_backups():
    d = paths.backup_dir()
    return [{"nom": n, "octets": os.path.getsize(os.path.join(d, n)), "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(os.path.join(d, n))))}
            for n in reversed(_list())]


def create(now=None):
    """Crée une archive datée et ne garde que les 7 dernières. Renvoie son chemin."""
    d = paths.backup_dir()
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    target = os.path.join(d, f"sylvie-{stamp}.zip")
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "sylvie.db")
        with contextlib.closing(sqlite3.connect(paths.db())) as src, contextlib.closing(sqlite3.connect(snap)) as dst:
            src.backup(dst)   # copie cohérente même si Sylvie écrit en même temps
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "sylvie.db")
            for n in EXTRA:
                p = os.path.join(paths.HERE, n)
                if os.path.exists(p):
                    z.write(p, n)
    os.chmod(target, 0o600)
    for old in _list()[:-KEEP]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(d, old))
    return target


def auto(max_age_days=7):
    """Sauvegarde si la dernière date de plus de N jours. Renvoie le chemin créé ou None."""
    last = _list()
    if last and time.time() - os.path.getmtime(os.path.join(paths.backup_dir(), last[-1])) < max_age_days * 86400:
        return None
    return create()


def restore(name):
    """Remplace le contenu de la base par celui de la sauvegarde (à n'appeler qu'après accord explicite)."""
    name = os.path.basename(str(name))
    path = os.path.join(paths.backup_dir(), name)
    if name not in _list():
        raise ValueError("sauvegarde introuvable (voir /sauvegarde)")
    with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory() as tmp:
        if "sylvie.db" not in z.namelist() or z.getinfo("sylvie.db").file_size > 500_000_000:
            raise ValueError("archive invalide")
        z.extract("sylvie.db", tmp)
        snap = os.path.join(tmp, "sylvie.db")
        with contextlib.closing(sqlite3.connect(snap)) as probe:
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("sauvegarde corrompue")
        with contextlib.closing(sqlite3.connect(snap)) as src, contextlib.closing(sqlite3.connect(paths.db())) as dst:
            src.backup(dst)
        for n in EXTRA:
            if n in z.namelist():
                with open(os.path.join(paths.HERE, n), "wb") as f:
                    f.write(z.read(n))
    return path
