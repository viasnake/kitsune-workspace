#!/bin/sh
set -eu

: "${KITSUNE_WORKSPACE_CONFIG:=/etc/kitsune/workspace.toml}"
export KITSUNE_WORKSPACE_CONFIG

kitsune workspace migrate --config "$KITSUNE_WORKSPACE_CONFIG"
exec "$@"
