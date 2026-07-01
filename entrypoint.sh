#!/bin/sh
# Ensure /data is owned by appuser, then drop to appuser and exec the CMD.
# This runs as root (USER is removed from the Dockerfile) so chown works on
# the root-owned named volume. setpriv then re-execs as UID 1000.
set -e
mkdir -p /data
chown -R appuser:appuser /data
exec setpriv --reuid=1000 --regid=1000 --clear-groups -- "$@"