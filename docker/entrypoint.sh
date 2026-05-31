#!/usr/bin/env bash
# Renders the cron schedule from automation/jobs.yaml and execs supercronic. Failure to
# render aborts the container so a malformed jobs.yaml surfaces immediately instead of
# becoming a silent no-op.

set -Eeuo pipefail

CRONTAB="${CRONTAB:-/tmp/crontab}"

echo "[entrypoint] rendering crontab to ${CRONTAB}"
python -m automation render-crontab > "${CRONTAB}"

echo "[entrypoint] rendered crontab:"
sed 's/^/  /' "${CRONTAB}"

echo "[entrypoint] starting supercronic"
exec supercronic "${CRONTAB}"
