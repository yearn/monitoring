#!/usr/bin/env bash
# Re-tag or delete small parent-vault flow alerts in $CACHE_DIR/monitoring.db.
#
# Why: until TELEGRAM_CHAT_ID_SMALL_DEPOSITS was set on this box, the monitor's
# alerts fell back to the `yearn` channel (utils/telegram.py: resolve_channel),
# and the pre-#364 rows were also stored under protocol `yearn`, so they show up
# on the public Yearn monitoring page (the API filters protocol = 'yearn').
#
# Usage (dry run first — nothing is written without --apply):
#   sudo deploy/clean-small-flow-alerts.sh
#   sudo deploy/clean-small-flow-alerts.sh --apply                 # retag -> yearn-internal
#   sudo deploy/clean-small-flow-alerts.sh --mode delete --apply   # remove the rows
#
# Options:
#   --apply             Actually write. Without it the script only reports.
#   --mode retag        Set protocol = 'yearn-internal' (default). Keeps the history.
#   --mode delete       Delete the matching rows.
#   --retag-channel     With retag, also rewrite channel -> 'small_deposits'.
#                       Off by default: `channel` records where a message really
#                       went, and rewriting it makes delivery history lie.
#   --include-envio     Also match this monitor's Envio error rows (source =
#                       'small_parent_flows'), not just the flow alerts.
#   --since YYYY-MM-DD  Only rows created at/after this UTC date.
#   --until YYYY-MM-DD  Only rows created before this UTC date.
#   --no-backup         Skip the pre-write DB snapshot (not recommended).
#
# Override REPO_DIR, CACHE_DIR, DB, TARGET_USER, or SQLITE when testing.

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/srv/monitoring}"
CACHE_DIR="${CACHE_DIR:-/srv/cache}"
DB="${DB:-${CACHE_DIR}/monitoring.db}"
TARGET_USER="${TARGET_USER:-${SUDO_USER:-$(whoami)}}"
SQLITE="${SQLITE:-sqlite3}"

MODE="retag"
APPLY=0
BACKUP=1
INCLUDE_ENVIO=0
RETAG_CHANNEL=0
SINCE=""
UNTIL=""

log() { printf '\033[1;34m[clean-small-flows]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[clean-small-flows]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)          APPLY=1; shift ;;
    --mode)           MODE="${2:-}"; shift 2 ;;
    --retag-channel)  RETAG_CHANNEL=1; shift ;;
    --include-envio)  INCLUDE_ENVIO=1; shift ;;
    --since)          SINCE="${2:-}"; shift 2 ;;
    --until)          UNTIL="${2:-}"; shift 2 ;;
    --no-backup)      BACKUP=0; shift ;;
    -h|--help)        sed -n '2,32p' "$0"; exit 0 ;;
    *)                die "unknown argument: $1" ;;
  esac
done

[[ "$MODE" == "retag" || "$MODE" == "delete" ]] || die "--mode must be retag or delete, got '${MODE}'"
[[ -f "$DB" ]] || die "database not found: ${DB}"
command -v "$SQLITE" >/dev/null || die "sqlite3 not found (deploy/install.sh installs it)"

# Run sqlite as the deploy user so it never leaves root-owned -wal/-shm files
# next to a DB the monitoring unit has to keep writing.
run_sql() {
  local sql prelude
  sql="$(cat)"
  # PRAGMA prints its result row in the CLI, so set it with output muted —
  # otherwise it pollutes every value this script captures.
  prelude=$'.output /dev/null\nPRAGMA busy_timeout = 10000;\n.output stdout'
  if [[ "$EUID" -eq 0 && "$TARGET_USER" != "root" ]]; then
    printf '%s\n%s\n' "$prelude" "$sql" | sudo -u "$TARGET_USER" -H "$SQLITE" "$DB"
  else
    printf '%s\n%s\n' "$prelude" "$sql" | "$SQLITE" "$DB"
  fi
}

# Both generations of the monitor put this literal in the message:
#   "Small parent-vault deposit" / "... withdrawal"   (per-flow, PR #345)
#   "Small parent-vault flows: N more ..."            (old overflow summary)
#   "Small parent-vault flows — N in this run"        (aggregate, PR #364)
MATCH="message LIKE '%Small parent-vault %'"
if [[ "$INCLUDE_ENVIO" -eq 1 ]]; then
  MATCH="(${MATCH} OR source = 'small_parent_flows')"
fi

# Guard rail: never touch a row this monitor could not have written.
WHERE="${MATCH} AND protocol IN ('yearn', 'yearn-internal')"
# created_at is stored fixed-width as YYYY-MM-DDTHH:MM:SS.ffffffZ (store.format_utc_iso),
# so the bounds are built in the same shape and compared as plain text.
check_date() { [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "date must be YYYY-MM-DD, got '$1'"; }
if [[ -n "$SINCE" ]]; then
  check_date "$SINCE"
  WHERE="${WHERE} AND created_at >= '${SINCE}T00:00:00.000000Z'"
fi
if [[ -n "$UNTIL" ]]; then
  check_date "$UNTIL"
  WHERE="${WHERE} AND created_at < '${UNTIL}T00:00:00.000000Z'"
fi

log "database: ${DB} (as ${TARGET_USER})"
log "mode: ${MODE}$([[ $APPLY -eq 1 ]] && echo ' (apply)' || echo ' (dry run)')"
log "matching rows:"
run_sql <<SQL
.mode column
.headers on
SELECT protocol, channel, severity, count(*) AS rows,
       min(created_at) AS first_seen, max(created_at) AS last_seen
FROM alert_events
WHERE ${WHERE}
GROUP BY protocol, channel, severity
ORDER BY rows DESC;
SQL

TOTAL="$(run_sql <<SQL
SELECT count(*) FROM alert_events WHERE ${WHERE};
SQL
)"
log "total matched: ${TOTAL}"

if [[ "$TOTAL" -eq 0 ]]; then
  log "nothing to do"
  exit 0
fi

if [[ "$APPLY" -eq 0 ]]; then
  log "dry run — re-run with --apply to write"
  exit 0
fi

if [[ "$BACKUP" -eq 1 ]]; then
  SNAPSHOT="${DB}.bak-$(date -u +%Y%m%dT%H%M%SZ)"
  log "snapshotting to ${SNAPSHOT}"
  # .backup uses SQLite's online backup API, so it is WAL-safe with the
  # monitoring unit still running.
  run_sql <<SQL
.backup '${SNAPSHOT}'
SQL
fi

if [[ "$MODE" == "retag" ]]; then
  SET_CLAUSE="protocol = 'yearn-internal'"
  [[ "$RETAG_CHANNEL" -eq 1 ]] && SET_CLAUSE="${SET_CLAUSE}, channel = 'small_deposits'"
  STATEMENT="UPDATE alert_events SET ${SET_CLAUSE} WHERE ${WHERE};"
else
  STATEMENT="DELETE FROM alert_events WHERE ${WHERE};"
fi

log "applying: ${STATEMENT}"
run_sql <<SQL
BEGIN IMMEDIATE;
${STATEMENT}
SELECT 'rows affected: ' || changes();
COMMIT;
.output /dev/null
PRAGMA wal_checkpoint(TRUNCATE);
.output stdout
SQL

log "remaining rows tagged protocol='yearn' for this monitor:"
run_sql <<SQL
SELECT count(*) FROM alert_events WHERE ${MATCH} AND protocol = 'yearn';
SQL
log "done"
