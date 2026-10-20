# Sylvie

Agent IA personnel qui tourne sur Android via Termux, servi par l'API Gemini.
Elle parle, écoute, cherche sur le web, se souvient de toi et agit sur le téléphone, **mais n'agit jamais sans ton accord**.

## Installation (Termux depuis F-Droid)

```bash
git clone https://github.com/Indra-Labs-dev/sylvie ~/agent
cd ~/agent && bash install.sh
nano .env            # mets ta clé : export GEMINI_API_KEY="..."
```

Installe aussi l'appli **Termux:API** (F-Droid) et donne-lui les permissions utiles : micro, SMS, contacts, téléphone, localisation.
Ouvre une nouvelle session Termux, puis tape **`sylvie`**. Avec l'appli **Termux:Widget**, un raccourci « Sylvie » est aussi disponible sur l'écran d'accueil.

## Ce qu'elle sait faire

| Domaine | Outils | Validation |
|---|---|---|
| Base | heure, calcul | aucune (réponse instantanée, sans API) |
| Mémoire | retenir, rechercher / oublier un souvenir | auto / confirmer |
| Web | recherche Google (sources citées), ouvrir une page | auto / confirmer |
| Téléphone | batterie, Wi-Fi | auto |
| Téléphone | notification, vibreur, lampe, presse-papiers, position, contacts, lire les SMS | confirmer |
| Communication | envoyer un SMS, appeler | **taper OUI** |

Tape `/outils` pour la liste exacte.

## Commandes

| Commande | Effet |
|---|---|
| `v` | parler (Entrée pour terminer) ; la voix est transcrite en français par Gemini |
| `/voix` | lecture des réponses à voix haute |
| `/memoire`, `/oublie N` | voir / effacer ce qu'elle a retenu |
| `/outils`, `/journal` | outils disponibles, dernières actions autorisées ou refusées |
| `/modele [nom]` | voir ou changer le modèle |
| `/reset` | nouvelle conversation (la mémoire reste) |
| `q` | quitter |

## Sécurité

- La porte de confirmation (`gate.py`) est **dans le code** : le modèle ne peut pas la contourner.
- Trois niveaux : `auto` (lecture sans conséquence), `confirm` (boîte Oui/Non), `sensitive` (il faut taper **OUI**).
- Le contenu venu de l'extérieur (pages web, SMS, presse-papiers) est marqué non fiable. Si une action est demandée après sa lecture, la boîte affiche un **avertissement d'injection**, et l'écriture en mémoire n'est plus automatique.
- Les confirmations sont tactiles : une voix captée par le micro ne peut jamais valider une action.
- Tout est journalisé dans `actions.log`. La mémoire (`sylvie.db`), la clé (`.env`) et les enregistrements restent sur le téléphone et ne sont jamais commités. Les enregistrements vocaux sont supprimés après transcription.

## Réglages (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `GEMINI_MODEL` / `GEMINI_FALLBACK` | `gemini-3.6-flash` / `gemini-3.5-flash-lite` | modèle, et modèle de secours en cas de surcharge |
| `SYLVIE_THINKING` | `minimal` | niveau de réflexion du modèle |
| `SYLVIE_SPEAK=1` | off | lecture à voix haute dès le démarrage |
| `SYLVIE_STT=android` | `gemini` | utiliser la reconnaissance vocale du système |
| `SYLVIE_ANIM=0`, `NO_COLOR=1` | on | couper les animations / couleurs |
| `SYLVIE_DEBUG=1` | off | afficher le modèle et le temps de chaque appel |

## Mise à jour

```bash
bash update.sh
```

Affiche les commits et le diff, puis demande validation avant d'appliquer.

## Architecture

`agent.py` (boucle, commandes) · `llm.py` (API Gemini) · `tools.py` (outils et niveaux de risque) · `gate.py` (confirmation, journal) · `memory.py` (SQLite) · `voice.py` (micro, synthèse) · `fast.py` (réponses locales) · `ui.py` (couleurs, animations)

Pour ajouter un outil : écrire la fonction dans `tools.py`, puis l'enregistrer avec `tool(fonction, "auto"|"confirm"|"sensitive", "description", {paramètres})`.
