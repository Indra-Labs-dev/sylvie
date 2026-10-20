#!/data/data/com.termux/files/usr/bin/bash
# Mise à jour semi-automatique de Sylvie : tu vois les changements, puis tu valides.
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
if [ "$r" = "o" ]; then
    git merge --ff-only origin/main && echo "OK, mis à jour."
else
    echo "Annulé."
fi
