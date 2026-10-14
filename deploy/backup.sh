#!/usr/bin/env bash
# Copia cifrada de salud a Google Drive (rclone, remoto gdrive:).
#
# Incluye la base de datos (snapshot consistente con la API de backup de
# SQLite, sin parar la web), el .env del despliegue, el perfil y la sesión de
# Garmin. Se cifra con gpg simétrico: son datos de salud y van a un servicio
# de terceros. La frase está en ~/.config/salud/backup_passphrase y tiene
# copia en Vaultwarden; sin ella la copia no sirve.
#
# Restaurar:
#   gpg --batch --passphrase-file <frase> -d salud-<fecha>.tar.gz.gpg | tar -xz
set -euo pipefail

DB="${SALUD_DB_PATH:-$HOME/.local/share/salud/salud.db}"
ENV_FILE="$HOME/salud/.env"
PERFIL="$HOME/.config/salud/perfil.toml"
GARMIN="$HOME/.local/share/salud/garmin"
PASSPHRASE="$HOME/.config/salud/backup_passphrase"
DEST_LOCAL="$HOME/.local/share/salud/backups"
DEST_REMOTE="gdrive:salud-backups"
DIAS_LOCAL=30
DIAS_REMOTO=180

fecha="$(date +%Y-%m-%d_%H%M)"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
umask 077
mkdir -p "$DEST_LOCAL"

sqlite3 "$DB" ".backup '$staging/salud.db'"
cp "$ENV_FILE" "$staging/env"
[ -f "$PERFIL" ] && cp "$PERFIL" "$staging/"
[ -d "$GARMIN" ] && cp -r "$GARMIN" "$staging/garmin"

archivo="$DEST_LOCAL/salud-$fecha.tar.gz.gpg"
tar -C "$staging" -cz . \
  | gpg --batch --yes --symmetric --cipher-algo AES256 \
        --passphrase-file "$PASSPHRASE" -o "$archivo"

rclone copy "$archivo" "$DEST_REMOTE"
find "$DEST_LOCAL" -name 'salud-*.tar.gz.gpg' -mtime +"$DIAS_LOCAL" -delete
rclone delete --min-age "${DIAS_REMOTO}d" "$DEST_REMOTE"

echo "$(date -Is) copia $(basename "$archivo") ($(stat -c %s "$archivo") bytes) subida a $DEST_REMOTE"
