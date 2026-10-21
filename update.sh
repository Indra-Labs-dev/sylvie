#!/data/data/com.termux/files/usr/bin/bash
# Mise à jour semi-automatique de Sylvie : tu vois les changements, tu valides, les tests tournent, retour arrière si ça casse.
set -e
cd "$(dirname "$0")"
git fetch -q origin main
if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
    echo "Déjà à jour."
    exit 0
fi
echo "=== Commits à appliquer ==="
git log --oneline HEAD..origin/main
echo
echo "=== Fichiers modifiés ==="
git diff --stat HEAD origin/main
echo
read -p "Voir le détail du code ? (o/N) " d
[ "$d" = "o" ] && git diff HEAD origin/main | less
read -p "Appliquer ces changements ? (o/N) " r
if [ "$r" != "o" ]; then
    echo "Annulé."
    exit 0
fi
OLD=$(git rev-parse HEAD)
git merge --ff-only origin/main
if [ "$SYLVIE_SKIP_TESTS" = "1" ]; then
    echo "OK, mis à jour (tests ignorés)."
    exit 0
fi
LOG="$(pwd)/.update-tests.log"   # pas /tmp : non inscriptible sous Termux
echo "→ Vérification (tests)..."
if python -B -m unittest discover -s tests >"$LOG" 2>&1; then
    echo "OK, mis à jour : $(tail -n 3 "$LOG" | head -n 1)"
else
    echo "✖ Les tests échouent sur ce téléphone :"
    grep -E "^(FAIL|ERROR):" "$LOG" | head -n 8
    read -p "Revenir à la version précédente ? (O/n) " b
    if [ "$b" != "n" ]; then
        git reset -q --hard "$OLD"
        echo "Retour à la version précédente. Détail : "$LOG""
    else
        echo "Mise à jour gardée malgré les échecs."
    fi
fi
