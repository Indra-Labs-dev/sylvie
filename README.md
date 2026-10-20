# Sylvie

Agent IA personnel qui tourne sur Android via Termux. Le modèle est servi par l'API Gemini, et **toute action modifiante demande ta confirmation**.

## Principe de sécurité

- Chaque outil est classé `auto` (lecture seule) ou `confirm` (action) dans `tools.py`.
- La porte de confirmation (`gate.py`) est dans le code, pas dans le prompt : le modèle ne peut pas la contourner.
- Chaque décision (acceptée, refusée, exécutée) est écrite dans `actions.log`.
- Le contenu renvoyé par les outils est traité comme une donnée, jamais comme une instruction.

## Installation (Termux depuis F-Droid)

```bash
pkg install -y python git termux-api
pip install requests
git clone https://github.com/Indra-Labs-dev/sylvie ~/agent
cd ~/agent
echo 'export GEMINI_API_KEY="ta_clé"' > .env && chmod 600 .env
```

Installer aussi l'appli **Termux:API** (F-Droid) et autoriser micro, SMS et notifications.

## Lancer

```bash
cd ~/agent && . ./.env && python agent.py
```

Modèle par défaut : `gemini-3.5-flash-lite`. Pour en changer : `export GEMINI_MODEL=nom`.

## Mise à jour

```bash
bash update.sh
```

Affiche les commits et le diff, puis demande validation avant d'appliquer.

## Ajouter un outil

1. Écrire la fonction dans `tools.py`.
2. L'ajouter à `TOOLS` avec son niveau de risque (`auto` ou `confirm`).
3. Déclarer ses paramètres dans `DECL`.

## Feuille de route

- [ ] Voix : écoute (`termux-speech-to-text`) et parole (`termux-tts-speak`)
- [ ] Recherche web (appel Gemini séparé avec grounding)
- [ ] Mémoire persistante (SQLite)
- [ ] Outils SMS / calendrier avec confirmation renforcée
