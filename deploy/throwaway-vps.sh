#!/usr/bin/env bash
# One-shot provisioner for a THROWAWAY VPS that stands up BOTH systems —
# `monitoring` (this repo) and `liquidity` (tapired/liquidity-monitoring) — on
# one box in fully neutered "no-send / shadow" mode, then runs
# deploy/integration-check.sh to prove they coexist without collisions.
#
# Nothing leaves the box and nothing on-chain happens:
#   - monitoring runs with LOG_LEVEL=DEBUG  → ALL Telegram sends are skipped.
#   - liquidity runs with SHADOW_MODE=true  → git-sync forced off, every task
#     dry-run, emergency/PR flow log-only (scheduler/app/settings.py). The
#     SOPS-encrypted prod env is removed so the daemon boots with no secrets.
#
# Every flag is baked in — you run ONE command and read the check output. When
# done, just delete the VPS (or run `… teardown` to undo on a reused box).
#
# Usage (as root on a fresh Debian 12 / Ubuntu 22.04+ box):
#   # token must have read access to BOTH private repos:
#   sudo GITHUB_TOKEN=ghp_xxx bash throwaway-vps.sh
#
#   # tear the two stacks back down (services + dirs) without deleting the box:
#   sudo bash throwaway-vps.sh teardown
#
# Overridable (all have sane defaults):
#   TARGET_USER, MON_REPO_URL/MON_BRANCH/MON_REPO, LIQ_REPO_URL/LIQ_BRANCH/LIQ_REPO

set -Eeuo pipefail

ACTION="${1:-provision}"

# ─── what to provision (override via env) ──────────────────────────────
MON_REPO_URL="${MON_REPO_URL:-https://github.com/yearn/monitoring.git}"
MON_BRANCH="${MON_BRANCH:-docker}"          # rename/deploy work still lives on docker
MON_REPO="${MON_REPO:-/srv/monitoring}"

LIQ_REPO_URL="${LIQ_REPO_URL:-https://github.com/tapired/liquidity-monitoring.git}"
LIQ_BRANCH="${LIQ_BRANCH:-main}"
LIQ_REPO="${LIQ_REPO:-/srv/liquidity-monitoring}"

log()  { printf '\033[1;34m[throwaway]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[throwaway]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[throwaway]\033[0m %s\n' "$*" >&2; exit 1; }

(( EUID == 0 )) || die "run as root (sudo bash throwaway-vps.sh)"

# Deploy user: the invoker (sudo) or root on a bare box. Both installers run
# their service as this user; using one user mirrors the real VPS and lets the
# integration check exercise the shared-git-credentials path.
TARGET_USER="${TARGET_USER:-${SUDO_USER:-$(whoami)}}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_HOME="${TARGET_HOME:-/root}"

as_user() {
  if [[ "$TARGET_USER" == "root" ]]; then "$@"; else sudo -u "$TARGET_USER" -H "$@"; fi
}

# ─── teardown path ─────────────────────────────────────────────────────
if [[ "$ACTION" == "teardown" ]]; then
  log "tearing down both stacks (services + dirs)…"
  for u in monitoring liquidity liquidity-healthcheck.timer liquidity-sync.timer; do
    systemctl disable --now "$u" 2>/dev/null || true
  done
  rm -f /etc/systemd/system/monitoring.service \
        /etc/systemd/system/liquidity*.service \
        /etc/systemd/system/liquidity*.timer \
        /etc/systemd/journald.conf.d/monitoring.conf
  systemctl daemon-reload
  rm -rf "$MON_REPO" "$LIQ_REPO" /etc/monitoring /etc/liquidity /srv/cache
  log "done. (the box itself is untouched — delete the VPS to fully clean up.)"
  exit 0
fi

# ─── 0. base tooling + git credentials for both private repos ──────────
log "installing base tooling (git curl jq)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git jq

# Resolve a GitHub token (env → invoking user's gh → root's gh). ONE token must
# reach BOTH repos (yearn/* and tapired/*) — the same constraint the check flags.
_github_token() {
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then printf '%s' "$GITHUB_TOKEN"; return 0; fi
  if [[ -n "${SUDO_USER:-}" ]] && command -v gh >/dev/null 2>&1; then
    local t; t="$(sudo -u "$SUDO_USER" gh auth token 2>/dev/null)" && [[ -n "$t" ]] && { printf '%s' "$t"; return 0; }
  fi
  if command -v gh >/dev/null 2>&1; then
    local t; t="$(gh auth token 2>/dev/null)" && [[ -n "$t" ]] && { printf '%s' "$t"; return 0; }
  fi
  return 1
}
if token="$(_github_token)"; then
  log "writing git credentials for ${TARGET_USER}…"
  cred="${TARGET_HOME}/.git-credentials"
  printf 'https://x-access-token:%s@github.com\n' "$token" > "$cred"
  chown "$TARGET_USER" "$cred"; chmod 600 "$cred"
  as_user git config --global credential.helper store
else
  warn "no GITHUB_TOKEN / gh login found — clones below will fail if either repo is private."
  warn "re-run as: sudo GITHUB_TOKEN=ghp_xxx bash throwaway-vps.sh"
fi

# ─── 1. clone both repos (so each installer is on disk) ────────────────
mkdir -p /srv
clone_repo() {  # url dir branch
  local url="$1" dir="$2" branch="$3"
  if [[ -d "$dir/.git" ]]; then
    log "repo present: ${dir} — fetching ${branch}…"
    as_user git -C "$dir" fetch --quiet origin "$branch"
    as_user git -C "$dir" checkout --quiet "$branch"
    as_user git -C "$dir" pull --ff-only --quiet
  else
    log "cloning ${url} (${branch}) → ${dir}…"
    install -d -o "$TARGET_USER" "$dir"
    as_user git clone --quiet --branch "$branch" "$url" "$dir"
  fi
}
clone_repo "$MON_REPO_URL" "$MON_REPO" "$MON_BRANCH"
clone_repo "$LIQ_REPO_URL" "$LIQ_REPO" "$LIQ_BRANCH"

# ─── 2. provision monitoring (its own installer does the heavy lifting) ─
log "running monitoring installer…"
GITHUB_TOKEN="${token:-}" BRANCH="$MON_BRANCH" REPO_DIR="$MON_REPO" TARGET_USER="$TARGET_USER" \
  bash "${MON_REPO}/deploy/install.sh"

# DEBUG suppresses ALL Telegram sends (deploy/runbook.md). The unit refuses to
# start without this file existing, so we drop a minimal one.
log "writing throwaway /etc/monitoring/.env (LOG_LEVEL=DEBUG → no Telegram)…"
install -d -m 0750 -o root -g "$TARGET_USER" /etc/monitoring
install -m 0640 -o root -g "$TARGET_USER" /dev/stdin /etc/monitoring/.env <<'MENV'
# THROWAWAY env. DEBUG skips every Telegram send (see deploy/runbook.md).
# No RPC URLs: jobs will error harmlessly in journald; supercronic still runs.
LOG_LEVEL=DEBUG
MENV

# ─── 3. provision liquidity, then neutralize it (shadow + no secrets) ──
log "running liquidity installer…"
GITHUB_TOKEN="${token:-}" BRANCH="$LIQ_BRANCH" REPO_DIR="$LIQ_REPO" TARGET_USER="$TARGET_USER" \
  bash "${LIQ_REPO}/deploy/install.sh"

# Remove the SOPS-encrypted prod env so the unit's ExecStartPre doesn't try to
# decrypt it without an age key (which would be fatal). With it gone and our
# .env present, the unit loads our shadow env as-is.
log "neutralizing liquidity secrets (rm prod.env.enc) + writing shadow env…"
rm -f "${LIQ_REPO}/deploy/secrets/prod.env.enc"
install -d -m 0750 -o root -g "$TARGET_USER" /etc/liquidity
install -m 0640 -o root -g "$TARGET_USER" /dev/stdin /etc/liquidity/.env <<'LENV'
# THROWAWAY env. SHADOW_MODE forces git-sync OFF, every task dry-run, and the
# emergency/PR flow to log-only (scheduler/app/settings.py). No signer key, no
# RPCs, no pushes — the daemon still boots and serves /healthz.
SHADOW_MODE=true
LOG_FORMAT=json
LENV

# ─── 4. start everything ───────────────────────────────────────────────
log "enabling + starting both stacks…"
systemctl daemon-reload
systemctl enable --now monitoring
systemctl enable --now liquidity liquidity-healthcheck.timer liquidity-sync.timer

# ─── 5. wait for the liquidity daemon to answer /healthz ───────────────
log "waiting for liquidity /healthz…"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    log "liquidity healthy ✓"; break
  fi
  sleep 1
done

# ─── 6. run the integration / coexistence check (as root → full coverage) ─
log "running integration check…"
echo
set +e
bash "${MON_REPO}/deploy/integration-check.sh"
rc=$?
set -e

cat <<NEXT

──────────────────────────────────────────────────────────────────────
throwaway provisioned. both stacks run in safe mode:
  monitoring → LOG_LEVEL=DEBUG  (no Telegram sends)
  liquidity  → SHADOW_MODE=true (dry-run, no signing, no git push)

re-run the check anytime:   sudo bash ${MON_REPO}/deploy/integration-check.sh
tear the stacks down:       sudo bash ${MON_REPO}/deploy/throwaway-vps.sh teardown
…or just delete the VPS.
──────────────────────────────────────────────────────────────────────
NEXT

exit "$rc"
