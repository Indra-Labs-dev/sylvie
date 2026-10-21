# Sylvie

Agent IA personnel qui tourne sur Android via Termux, servi par l'API Gemini, avec un routeur qui bascule vers d'autres fournisseurs d'IA quand un quota est épuisé.
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
| Web | recherche Google (sources citées), repli DuckDuckGo si quota épuisé ; lire et résumer une page (`read_url`) ; ouvrir une page ; musique | auto / confirmer |
| API gratuites | 52 API (météo, change, crypto, CVE, IP, DNS, Wikipédia, lieux proches, emplois…) via `api_call` | auto, confirmer si du contenu externe a été lu juste avant |
| Téléphone | batterie, Wi-Fi | auto |
| Téléphone | notification, vibreur, lampe, presse-papiers, position (quartier et ville via OpenStreetMap), contacts, lire les SMS | confirmer |
| Communication | envoyer un SMS, appeler | **taper OUI** |

Tape `/outils` pour la liste exacte.

## Commandes

| Commande | Effet |
|---|---|
| `v` | parler (Entrée pour terminer) ; la voix est transcrite en français par Gemini |
| `/voix` | lecture des réponses à voix haute |
| `/memoire`, `/oublie N` | voir / effacer ce qu'elle a retenu |
| `/outils`, `/journal` | outils disponibles, dernières actions autorisées ou refusées |
| `/modele [nom]` | voir ou changer le modèle principal |
| `/fournisseurs [aide]` | état, budgets et pauses des fournisseurs d'IA ; tester, choisir un modèle, régler la confiance |
| `/apis [mot]`, `/apitest [nom]` | catalogue d'API, et test de chacune avec son exemple |
| `/r` | réessayer la dernière question après une panne de l'API |
| `/reset` | nouvelle conversation (la mémoire reste) |
| `q` | quitter |

## Sécurité

- La porte de confirmation (`gate.py`) est **dans le code** : le modèle ne peut pas la contourner.
- Trois niveaux : `auto` (lecture sans conséquence), `confirm` (boîte Oui/Non), `sensitive` (il faut taper **OUI**).
- Le contenu venu de l'extérieur (pages web, SMS, presse-papiers) est marqué non fiable. Si une action est demandée après sa lecture, la boîte affiche un **avertissement d'injection**, et l'écriture en mémoire n'est plus automatique.
- Les confirmations sont tactiles : une voix captée par le micro ne peut jamais valider une action.
- Tout est journalisé dans `actions.log`. La mémoire (`sylvie.db`), la clé (`.env`) et les enregistrements restent sur le téléphone et ne sont jamais commités. Les enregistrements vocaux sont supprimés après transcription.

## Fournisseurs d'IA (anti-blocage)

`providers.json` liste les fournisseurs, essayés dans l'ordre. Quand l'un répond « quota » ou « surcharge », il est mis en pause et le suivant prend le relais ; chaque fournisseur a un budget d'appels par jour.

- **Gemini** (flash puis lite) : seul à avoir la recherche Google et la transcription, et seul « privé ».
- **groq, openrouter, mistral, cerebras, nvidia, github-models** : actifs seulement si leur clé est dans `.env` **et** si tu as choisi un modèle (`/fournisseurs modeles groq`, puis `/fournisseurs modele groq <modèle>`). Les adresses sont à vérifier avec `/fournisseurs test`.
- **Confidentialité** : un fournisseur « public » ne reçoit jamais SMS, contacts, position ni mémoire, seulement des tâches anodines (résumé d'une page web, par exemple). `/fournisseurs confiance <id> oui` demande de taper OUI.
- Tes réglages vont dans `providers.local.json` (ignoré par git, jamais écrasé par une mise à jour). Clés possibles : `overrides`, `providers`, `order`.
- Utiliser plusieurs clés ou projets d'un **même** fournisseur pour contourner son quota n'est pas prévu : cela viole en général les conditions d'utilisation.

## Catalogue d'API

`apis.json` décrit chaque API de façon déclarative (adresse, paramètres validés, extraction du résultat, budget quotidien, cache). Seul le domaine écrit dans le catalogue peut être appelé, en HTTPS, ce qui empêche le modèle d'envoyer des requêtes ailleurs. Les adresses n'ont pas pu être vérifiées en direct : lance `/apitest` sur le téléphone.

Pour ajouter ou retirer une API sans toucher au code, crée `apis.local.json` : `{"apis": [ {...même format que apis.json...} ], "remove": ["nom"]}`.

## Réglages (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `GEMINI_API_KEY` | | clé Gemini (obligatoire) |
| `GEMINI_MODEL` / `GEMINI_FALLBACK` | `gemini-3.6-flash` / `gemini-3.5-flash-lite` | modèles Gemini principal et de secours |
| `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `CEREBRAS_API_KEY`, `NVIDIA_API_KEY`, `GITHUB_MODELS_TOKEN` | | fournisseurs facultatifs ; `GITHUB_TOKEN` facultatif pour les API GitHub |
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

`agent.py` (boucle, commandes) · `llm.py` + `router.py` (fournisseurs, pauses, budgets) · `apis.py` + `apis.json` (catalogue d'API) · `web.py` (lecture de pages sûre) · `tools.py` (outils et niveaux de risque) · `gate.py` (confirmation, journal) · `memory.py` (SQLite) · `voice.py` (micro, synthèse) · `fast.py` (réponses locales) · `ui.py` (couleurs, animations)

Pour ajouter un outil : écrire la fonction dans `tools.py`, puis l'enregistrer avec `tool(fonction, "auto"|"confirm"|"sensitive", "description", {paramètres})`.
