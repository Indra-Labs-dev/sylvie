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

## Autonomie : tâches, objectifs, interface web

Sylvie peut agir seule, mais **tu valides le cadre une fois**, et le code applique la règle ensuite.

- **Tâches programmées** (`schedule_add`, `/taches`, onglet Tâches) : rappels, briefing du matin (`/briefing 07:30`), surveillance d'une API (alerte seulement si le résultat change : aucun jeton consommé), tâches IA récurrentes. À la création, la boîte de confirmation montre exactement ce que la tâche pourra faire. Ensuite, sans toi : seuls les outils listés sont utilisables ; SMS et appels sont interdits ; tout outil non prévu est refusé et **mis en attente** (onglet Attente, `/attente`) ; après lecture de contenu externe, plus aucune action. Un téléphone éteint ne déclenche pas de rafale : une tâche récurrente manquée de plus de 12 h est sautée.
- **Objectifs** (`/objectif ...`, onglet Objectifs, ou simplement une demande complexe) : Sylvie propose un **plan** (étapes, type, outils prévus), tu valides, puis des sous-agents spécialisés l'exécutent : recherche, données, téléphone, rédaction, vérification. Chacun n'a accès qu'aux outils de son type. SMS et appels demandent toujours de taper OUI. Budget de 40 appels au modèle, arrêt à tout moment, tout est dans `/journal`.
- **Interface web** : `sylvie serve` démarre un serveur **local** (127.0.0.1, invisible depuis le Wi-Fi) et ouvre la page. Chat, micro, validations d'actions (taper OUI pour les sensibles), tâches, objectifs, fournisseurs. Un jeton secret (`.token`) est exigé, avec contrôle de l'hôte et de l'origine.
- **Popup** : une notification permanente donne **Parler / Mini / Stop** sans ouvrir Termux. `/mini` est une version compacte : installe la page (menu du navigateur, « Installer l'application »), puis ouvre-la en fenêtre flottante depuis la barre latérale HyperOS. Une vraie bulle par-dessus les autres applis (comme Messenger) demande une application Android native, ce que Termux ne peut pas créer ; l'alternative est **Termux:Float** (F-Droid) pour une fenêtre de terminal flottante où lancer `sylvie`.
- Pour que les tâches tournent écran éteint : `sylvie serve` prend un wake-lock ; désactive l'optimisation de batterie de Termux (Paramètres › Batterie › Termux › Aucune restriction, et verrouille l'appli dans les récents).

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
| `/objectif <texte>` | mission en plusieurs étapes avec plan à valider |
| `/taches`, `/briefing HH:MM`, `/attente` | tâches programmées, briefing du matin, actions en attente de ton accord |
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

`agent.py` (terminal, commandes) · `engine.py` (boucle modèle ↔ outils commune) · `scheduler.py` (rappels, tâches, surveillances) · `planner.py` (objectifs et sous-agents) · `server.py` + `web_ui/` (interface web) · `llm.py` + `router.py` (fournisseurs, pauses, budgets) · `apis.py` + `apis.json` (catalogue d'API) · `web.py` (lecture de pages sûre) · `tools.py` (outils et niveaux de risque) · `gate.py` (confirmation, journal) · `memory.py` (SQLite) · `voice.py` (micro, synthèse) · `fast.py` (réponses locales) · `ui.py` (couleurs, animations)

Pour ajouter un outil : écrire la fonction dans `tools.py`, puis l'enregistrer avec `tool(fonction, "auto"|"confirm"|"sensitive", "description", {paramètres})`.
