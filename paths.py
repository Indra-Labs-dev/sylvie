"""Emplacements communs (base de données, dossier de sauvegarde)."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def db():
    return os.environ.get("SYLVIE_DB", os.path.join(HERE, "sylvie.db"))


def backup_dir():
    env = os.environ.get("SYLVIE_BACKUP_DIR")
    if env:
        return os.path.expanduser(env)
    shared = os.path.join(os.path.expanduser("~"), "storage", "shared")
    # hors des dossiers que les outils de fichiers peuvent lire : une sauvegarde contient ta mémoire
    return os.path.join(shared, "Sylvie-backups") if os.path.isdir(shared) else os.path.join(os.path.expanduser("~"), "sylvie-backups")
