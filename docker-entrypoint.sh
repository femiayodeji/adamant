#!/bin/sh
# Hosts mount the log disk over /data/logs at run time (Render disks, compose
# volumes), which hides the ownership the image set at build time — a fresh
# Render disk is root-owned. The server cannot write a match log it cannot open,
# so fix ownership while still root, then drop to the unprivileged user.
set -e

LOG_DIR="${ARENA_LOG_DIR:-/data/logs}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$LOG_DIR"
    chown -R arena:arena "$LOG_DIR"
    # `exec` keeps uvicorn as PID 1 so it receives SIGTERM directly.
    exec setpriv --reuid=arena --regid=arena --init-groups "$@"
fi

exec "$@"
