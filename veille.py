"""Veille sécurité : surveille un produit dans le catalogue CISA KEV (failles exploitées) et/ou les CVE du NIST.

Ce sont des « surveillances » du planificateur : aucun jeton d'IA, une alerte seulement si le résultat change.
Les titres commencent par « Veille » pour les retrouver.
"""
import json, re

import scheduler, tools

SOURCES = {"kev": ("cisa_kev", "q"), "cve": ("cve_recherche", "keywordSearch")}


def _product(p):
    p = re.sub(r"\s+", " ", str(p)).strip()
    if not re.fullmatch(r"[\w .+\-]{2,40}", p):
        raise ValueError("produit : 2 à 40 caractères (lettres, chiffres, espace, . + -)")
    return p


def veille_add(produit, source="kev", daily="08:30"):
    produit, source = _product(produit), str(source or "kev").lower()
    names = list(SOURCES) if source in ("les deux", "both", "tout") else [source]
    if any(n not in SOURCES for n in names):
        raise ValueError("source : kev (failles exploitées), cve ou les deux")
    out = []
    for n in names:
        api, param = SOURCES[n]
        r = scheduler.schedule_add("surveillance", f"Veille {n.upper()} : {produit}", daily=daily, api=api, params=json.dumps({param: produit}))
        out.append({"source": n.upper(), "id": r["id"], "quand": r["quand"]})
    return out


def _mine():
    return [j for j in scheduler.jobs() if j["kind"] == "surveillance" and j["title"].startswith("Veille ")]


def veille_list():
    return [{"id": j["id"], "titre": j["title"], "quand": scheduler.describe_schedule(j["schedule"]),
             "dernier_resultat": j["last_result"][:100]} for j in _mine()] or "aucune veille en cours"


def veille_remove(job_id):
    if int(job_id) not in {j["id"] for j in _mine()}:
        raise ValueError(f"pas de veille n° {job_id}")
    return scheduler.schedule_remove(int(job_id))


S = tools.S
tools.tool(veille_add, "confirm", "Surveille un produit ou éditeur (ex : Huawei, Wazuh, Zabbix, OpenSearch) : alerte quand une nouvelle faille exploitée "
           "(CISA KEV) ou une nouvelle CVE apparaît. Vérification quotidienne, sans coût d'IA.",
           {"produit": (S, "produit ou éditeur"), "source": (S, "kev | cve | les deux (kev par défaut)"), "daily": (S, "heure de vérification HH:MM, 08:30 par défaut")},
           required=["produit"], writes=True)
tools.tool(veille_list, "auto", "Liste les veilles sécurité en cours")
tools.tool(veille_remove, "confirm", "Arrête une veille sécurité", {"job_id": (tools.I, "numéro de la veille")}, writes=True)
