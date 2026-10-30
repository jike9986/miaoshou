#!/usr/bin/env bash
set -euo pipefail
umask 077

backup_dir=/var/backups/miaoshou-listing-v2
next="$backup_dir/next.dump"
latest="$backup_dir/latest.dump"
previous="$backup_dir/previous.dump"

pg_dump --format=custom --dbname=miaoshou_listing_v2 --file="$next"
pg_restore --list "$next" >/dev/null
chmod 600 "$next"

if [[ -f "$latest" ]]; then
    cp -f -- "$latest" "$previous"
    chmod 600 "$previous"
fi

mv -f -- "$next" "$latest"
