#!/usr/bin/env bash
# Fresh-VPS provisioning for the yearn-monitor cron runner (native deploy — no
# Docker, no Caddy).
#
# Idempotent: re-running on an already-provisioned host is safe. Each step
# checks current state before mutating.
#
# Tested on Debian 12 (Hetzner CX22 default image). Should work on Ubuntu
# 22.04+ unchanged; Python comes from uv's managed-Python install so no
# deadsnakes PPA is needed.
#
# Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/yearn/monitoring/main/deploy/install.sh | bash
#
# Or, after cloning the repo to /srv/yearn-monitoring:
#   sudo bash /srv/yearn-monitoring/deploy/install.sh
#
# The repo is cloned as — and owned by — the invoking user (SUDO_USER, or
# whoami when not run via sudo); the systemd unit also runs as that user.
# Override with TARGET_USER=.
#
# After this script the operator still needs to (see deploy/runbook.md):
#   1. Install the SOPS age private key at /etc/yearn-monitoring/age.key
#      (mode 0600, root). The unit reads it via SOPS_AGE_KEY_FILE.
#   2. Encrypt deploy/secrets/prod.env into prod.env.enc and push it.
# Then: `systemctl enable --now yearn-monitor`.
#
# Private-repo auth over HTTPS resolves a token, in order:
#   1. $GITHUB_TOKEN in the environment (pass with `sudo -E`).
#   2. The invoking user's `gh` login.
#   3. root's own `gh` login.
# When found it's written to the target user's ~/.git-credentials (mode 600) so
# the clone AND subsequent `git pull`s authenticate without prompting. SSH
# remotes (git@github.com:...) skip this entirely.

set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/yearn/monitoring.git}"
REPO_DIR="${REPO_DIR:-/srv/yearn-monitoring}"
BRANCH="${BRANCH:-main}"
ETC_DIR="${ETC_DIR:-/etc/yearn-monitoring}"
CACHE_DIR="${CACHE_DIR:-/srv/cache}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
SOPS_VERSION="${SOPS_VERSION:-v3.10.2}"
AGE_VERSION="${AGE_VERSION:-v1.2.1}"
# Pinned to match the (now removed) docker/Dockerfile. linux/amd64 only — bump
# both together and re-verify the checksum from the release page.
SUPERCRONIC_VERSION="${SUPERCRONIC_VERSION:-v0.2.34}"
SUPERCRONIC_SHA256="${SUPERCRONIC_SHA256:-a51b340a83c5bd035742f0d7191555f9663876405e494dbf824537d64f3e39c6}"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "missing required command: $1"; }

(( EUID == 0 )) || die "run as root (sudo bash install.sh)"

# The human operating the box: the user who invoked sudo, or whoami when not run
# via sudo. The repo is owned by this user, git credentials are written to their
# home, and the systemd unit runs as this user. Override with TARGET_USER=.
TARGET_USER="${TARGET_USER:-${SUDO_USER:-$(whoami)}}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_HOME="${TARGET_HOME:-/root}"

as_user() {
  if [[ "$TARGET_USER" == "root" ]]; then
    "$@"
  else
    sudo -u "$TARGET_USER" -H "$@"
  fi
}

# Resolve a GitHub token (env → invoking user's gh → root's gh). Prints the
# token on stdout and returns 0 when found; returns 1 otherwise.
_github_token() {
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    printf '%s' "$GITHUB_TOKEN"; return 0
  fi
  if [[ -n "${SUDO_USER:-}" ]] && command -v gh >/dev/null 2>&1; then
    local tok
    if tok="$(sudo -u "$SUDO_USER" gh auth token 2>/dev/null)" && [[ -n "$tok" ]]; then
      printf '%s' "$tok"; return 0
    fi
  fi
  if command -v gh >/dev/null 2>&1; then
    local tok
    if tok="$(gh auth token 2>/dev/null)" && [[ -n "$tok" ]]; then
      printf '%s' "$tok"; return 0
    fi
  fi
  return 1
}

# ─── apt prereqs ───────────────────────────────────────────────────────
# util-linux ships `flock`, which the rendered crontab wraps each profile in.
log "installing apt prerequisites…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl git jq util-linux

# ─── uv (manages Python + venvs) ──────────────────────────────────────
if ! command -v uv >/dev/null; then
  log "installing uv (Astral) → /usr/local/bin/uv…"
  curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
else
  log "uv already installed; skipping."
fi
need uv

log "ensuring Python ${PYTHON_VERSION} is available via uv…"
uv python install "${PYTHON_VERSION}"

# ─── supercronic (cron engine) ─────────────────────────────────────────
if ! command -v supercronic >/dev/null; then
  log "installing supercronic ${SUPERCRONIC_VERSION}…"
  curl -fsSL -o /usr/local/bin/supercronic \
    "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64"
  echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c -
  chmod +x /usr/local/bin/supercronic
else
  log "supercronic already installed; skipping."
fi

# ─── sops + age (static binaries) ──────────────────────────────────────
if ! command -v sops >/dev/null; then
  log "installing sops ${SOPS_VERSION}…"
  arch="$(dpkg --print-architecture)"
  curl -fsSL "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${arch}" \
    -o /usr/local/bin/sops
  chmod +x /usr/local/bin/sops
else
  log "sops already installed; skipping."
fi

if ! command -v age >/dev/null; then
  log "installing age ${AGE_VERSION}…"
  arch="$(dpkg --print-architecture)"
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-${arch}.tar.gz" \
    | tar -xz -C "$tmp"
  install -m 0755 "$tmp/age/age"        /usr/local/bin/age
  install -m 0755 "$tmp/age/age-keygen" /usr/local/bin/age-keygen
  rm -rf "$tmp"
else
  log "age already installed; skipping."
fi

# ─── git credentials (private repo over HTTPS) ─────────────────────────
if [[ "$REPO_URL" == https://* ]]; then
  if token="$(_github_token)"; then
    log "configuring git credentials for ${TARGET_USER} from GitHub token…"
    cred_file="${TARGET_HOME}/.git-credentials"
    printf 'https://x-access-token:%s@github.com\n' "$token" > "$cred_file"
    chown "$TARGET_USER" "$cred_file"
    chmod 600 "$cred_file"
    as_user git config --global credential.helper store
  else
    warn "no GITHUB_TOKEN / gh login found. If ${REPO_URL} is private the"
    warn "clone below will fail. Re-run with a token, e.g.:"
    warn "    sudo -E GITHUB_TOKEN=ghp_xxx bash deploy/install.sh"
  fi
fi

# ─── repo checkout ─────────────────────────────────────────────────────
# Owned by the target user so they can `git pull` without sudo.
if [[ ! -d "${REPO_DIR}/.git" ]]; then
  log "cloning ${REPO_URL} → ${REPO_DIR} as ${TARGET_USER}…"
  mkdir -p "$(dirname "$REPO_DIR")"
  install -d -o "$TARGET_USER" "$REPO_DIR"
  as_user git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
else
  log "repo present at ${REPO_DIR}; pulling latest ${BRANCH} as ${TARGET_USER}…"
  as_user git -C "$REPO_DIR" fetch --quiet origin "$BRANCH"
  as_user git -C "$REPO_DIR" checkout --quiet "$BRANCH"
  as_user git -C "$REPO_DIR" pull --ff-only --quiet
fi

# ─── Python venv + project deps ────────────────────────────────────────
log "installing Python deps into ${REPO_DIR}/.venv (as ${TARGET_USER})…"
as_user bash -c "cd '${REPO_DIR}' && uv sync --frozen"

# ─── writable cache dir ────────────────────────────────────────────────
# jobs.yaml points CACHE_FILENAME / NONCE_FILENAME / etc. at ${CACHE_DIR};
# the systemd unit grants it via ReadWritePaths. Owned by the runner user.
log "ensuring ${CACHE_DIR} exists (owned by ${TARGET_USER})…"
install -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" -d "$CACHE_DIR"

# ─── /etc/yearn-monitoring scaffolding ─────────────────────────────────
log "ensuring ${ETC_DIR} exists with the right perms…"
install -m 0750 -o root -g "$TARGET_USER" -d "$ETC_DIR"

# ─── systemd unit ──────────────────────────────────────────────────────
log "installing systemd unit (User=${TARGET_USER})…"
sed "s|__MONITOR_USER__|${TARGET_USER}|g" \
  "${REPO_DIR}/deploy/systemd/yearn-monitor.service" \
  > /etc/systemd/system/yearn-monitor.service
chmod 0644 /etc/systemd/system/yearn-monitor.service
systemctl daemon-reload

cat <<NEXT

──────────────────────────────────────────────────────────────────────
✓ host provisioned (repo owned by ${TARGET_USER} at ${REPO_DIR}).
  remaining manual steps:

  1. Install the age private key the service decrypts with:
       sudo install -m 600 -o root -g root /dev/stdin ${ETC_DIR}/age.key   # paste, Ctrl-D
     The systemd unit reads it via SOPS_AGE_KEY_FILE=${ETC_DIR}/age.key.

  2. Make sure deploy/secrets/prod.env.enc exists and is encrypted to this
     host's age recipient (see deploy/secrets/README.md). The unit decrypts it
     to ${ETC_DIR}/.env on start. To validate by hand:
       SOPS_AGE_KEY_FILE=${ETC_DIR}/age.key \\
         sops -d --input-type dotenv --output-type dotenv \\
         ${REPO_DIR}/deploy/secrets/prod.env.enc | sudo tee ${ETC_DIR}/.env >/dev/null
       sudo chown root:${TARGET_USER} ${ETC_DIR}/.env && sudo chmod 640 ${ETC_DIR}/.env

  3. Start the runner:
       sudo systemctl enable --now yearn-monitor
       systemctl status yearn-monitor

  4. Watch the first ticks:
       journalctl -u yearn-monitor -f
       # or dry-run a profile immediately:
       sudo -u ${TARGET_USER} bash -c 'cd ${REPO_DIR} && uv run python -m automation run hourly --dry-run'

See deploy/runbook.md for ongoing operations.
──────────────────────────────────────────────────────────────────────
NEXT
