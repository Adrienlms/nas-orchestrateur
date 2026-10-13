#!/bin/sh
# Démarrage du conteneur : prépare /data puis lance l'interface.
#
# Lancé en root (par défaut), le script donne /data à PUID:PGID (568:568, l'utilisateur
# « apps » de TrueNAS) puis abandonne les droits root. Cela évite l'erreur
# « Permission denied » quand le dossier monté appartient encore à root.
set -e
PUID="${PUID:-568}"
PGID="${PGID:-568}"
DATA="${SUPER_AGENT_DATA:-/data}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA" "$HOME"
  # Ne change que ce qui n'appartient pas déjà au bon utilisateur.
  find "$DATA" \( ! -uid "$PUID" -o ! -gid "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
  if ! setpriv --reuid="$PUID" --regid="$PGID" --clear-groups test -w "$DATA"; then
    echo "ERREUR : $DATA n'est pas accessible en écriture pour l'utilisateur $PUID:$PGID." >&2
    echo "Vérifie les permissions du dataset monté sur $DATA (Datasets > Permissions)." >&2
    exit 1
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

if ! mkdir -p "$HOME" 2>/dev/null || [ ! -w "$DATA" ]; then
  echo "ERREUR : $DATA n'est pas accessible en écriture pour l'utilisateur $(id -u):$(id -g)." >&2
  echo "Retire la ligne « user: » du YAML (le conteneur règle alors les droits lui-même)" >&2
  echo "ou donne le dataset à cet utilisateur dans TrueNAS (Datasets > Permissions)." >&2
  exit 1
fi
exec "$@"
