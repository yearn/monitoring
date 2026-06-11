#!/usr/bin/env bash
# Import the live legacy text cache files into $CACHE_DIR/monitoring.db.
#
# Usage:
#   sudo deploy/migrate-file-cache-to-db.sh
#
# Override REPO_DIR, CACHE_DIR, ETC_DIR, TARGET_USER, or PYTHON when testing.

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/srv/monitoring}"
CACHE_DIR="${CACHE_DIR:-/srv/cache}"
TARGET_USER="${TARGET_USER:-${SUDO_USER:-$(whoami)}}"
PYTHON="${PYTHON:-${REPO_DIR}/.venv/bin/python}"

log() { printf '\033[1;34m[migrate-cache]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[migrate-cache]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -d "$REPO_DIR" ]] || die "repo not found: ${REPO_DIR}"
[[ -x "$PYTHON" ]] || die "python not executable: ${PYTHON}"
install -d -m 0755 "$CACHE_DIR"

log "importing legacy cache files from ${CACHE_DIR} into ${CACHE_DIR}/monitoring.db"
if [[ "${EUID}" -eq 0 && "$TARGET_USER" != "root" ]]; then
  chown "$TARGET_USER:$TARGET_USER" "$CACHE_DIR"
  sudo -u "$TARGET_USER" -H bash -c '
    repo_dir="$1"; cache_dir="$2"; python="$3"; log_level="$4"; shift 4
    cd "$repo_dir"
    env CACHE_DIR="$cache_dir" LOG_LEVEL="$log_level" "$python" -m utils.migrate_cache_to_db --checkpoint "$@"
  ' bash "$REPO_DIR" "$CACHE_DIR" "$PYTHON" "${LOG_LEVEL:-INFO}" "$@"
else
  cd "$REPO_DIR"
  env CACHE_DIR="$CACHE_DIR" LOG_LEVEL="${LOG_LEVEL:-INFO}" "$PYTHON" -m utils.migrate_cache_to_db --checkpoint "$@"
fi
log "done"
