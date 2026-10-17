#!/bin/sh
# Starts as root only to hand the OAuth storage directory on a mounted volume
# (Railway volumes are root-owned) to the unprivileged `brain` user, then drops
# privileges for good before running the server.
set -eu

if [ "$(id -u)" = "0" ]; then
  if [ -n "${SB_OAUTH_STORAGE_DIR:-}" ]; then
    mkdir -p "$SB_OAUTH_STORAGE_DIR"
    chown -R brain:brain "$SB_OAUTH_STORAGE_DIR"
    chmod 700 "$SB_OAUTH_STORAGE_DIR"
  fi
  exec setpriv --reuid=brain --regid=brain --init-groups --no-new-privs -- "$@"
fi

exec "$@"
