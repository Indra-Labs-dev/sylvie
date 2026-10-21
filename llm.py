"""Interface simple vers les modèles d'IA : tout passe par router.py (failover, budgets, confidentialité)."""
import router

NoProvider = router.NoProvider


def generate(contents, system=None, tools=None, thinking=True, role="chat", private=True):
    """role : chat (conversation + outils) | fast (tâche secondaire) | search | audio.
    private=False : la requête ne contient aucune donnée personnelle, un fournisseur « public » peut la traiter."""
    return router.generate(role, contents, system=system, tools=tools, private=private, thinking=thinking)


def text_of(content):
    return router.text_of(content)
