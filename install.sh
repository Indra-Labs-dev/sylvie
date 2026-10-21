#!/data/data/com.termux/files/usr/bin/bash
# Installation / réparation de Sylvie dans Termux (relançable sans risque).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "→ Paquets"
pkg install -y python python-pip termux-api >/dev/null
python -m pip install -q requests pypdf
pkg install -y python-pillow >/dev/null 2>&1 || echo "(Pillow indisponible : les photos ne seront pas réduites)"
echo "→ Commande 'sylvie'"
LINE="alias sylvie='cd $DIR && . ./.env && python agent.py'"
grep -qxF "$LINE" ~/.bashrc 2>/dev/null || echo "$LINE" >> ~/.bashrc
echo "→ Raccourci Termux:Widget (~/.shortcuts/Sylvie)"
mkdir -p ~/.shortcuts
printf '#!/data/data/com.termux/files/usr/bin/bash\ncd %s && . ./.env && python agent.py\n' "$DIR" > ~/.shortcuts/Sylvie
chmod 700 ~/.shortcuts/Sylvie
printf '#!/data/data/com.termux/files/usr/bin/bash\ncd %s && . ./.env && python agent.py serve\n' "$DIR" > ~/.shortcuts/Sylvie-serveur
chmod 700 ~/.shortcuts/Sylvie-serveur
[ -f "$DIR/.env" ] || { echo 'export GEMINI_API_KEY="COLLE_TA_CLE_ICI"' > "$DIR/.env"; echo "⚠ Mets ta clé dans $DIR/.env"; }
chmod 600 "$DIR/.env"
echo "✔ Terminé. Ouvre une nouvelle session Termux puis tape : sylvie"
