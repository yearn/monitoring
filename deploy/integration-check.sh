#!/usr/bin/env bash
# Coexistence / integration smoke-check for the two monitoring systems that
# share one VPS:
#
#   - monitoring  (this repo)  — supercronic cron runner, no HTTP surface.
#   - liquidity   (tapired/liquidity-monitoring) — FastAPI+APScheduler daemon
#     bound to 127.0.0.1:8080, plus healthcheck + git-sync timers.
#
# The two are *designed* not to collide (separate repo dirs, /etc dirs, unit
# names, uv caches). This script verifies that on the live box and surfaces the
# soft shared surfaces that only appear when both run on the same host: one
# deploy user's git credentials must auth both GitHub repos, both hammer the
# same RPC providers from one IP, and they may share a Telegram bot.
#
# READ-ONLY: it never starts/stops services, never writes, never prints secret
# values (only hostnames / key names). Safe to run anytime.
#
# Usage:
#   sudo bash deploy/integration-check.sh        # full coverage (reads /etc/*/.env, maps :8080 owner)
#   bash deploy/integration-check.sh             # degrades gracefully without root
#
# Exit code: 0 if no FAILs (WARNs allowed), 1 if any hard collision / dead service.
#
# Override any path/port via env, e.g.:
#   LIQ_PORT=9090 MON_REPO=/srv/monitoring sudo bash deploy/integration-check.sh

set -uo pipefail   # NOT -e: every check must run; we tally results ourselves.

# ─── what to check (override via env) ──────────────────────────────────
MON_REPO="${MON_REPO:-/srv/monitoring}"
MON_UNIT="${MON_UNIT:-monitoring.service}"
MON_ETC="${MON_ETC:-/etc/monitoring}"
MON_CACHE="${MON_CACHE:-/srv/cache}"

LIQ_REPO="${LIQ_REPO:-/srv/liquidity-monitoring}"
LIQ_UNIT="${LIQ_UNIT:-liquidity.service}"
LIQ_ETC="${LIQ_ETC:-/etc/liquidity}"
LIQ_HOST="${LIQ_HOST:-127.0.0.1}"
LIQ_PORT="${LIQ_PORT:-8080}"
# Extra liquidity units that should be present/healthy (timers).
LIQ_TIMERS="${LIQ_TIMERS:-liquidity-healthcheck.timer liquidity-sync.timer}"

# ─── output helpers ────────────────────────────────────────────────────
if [[ -t 1 ]]; then G='\033[1;32m'; Y='\033[1;33m'; R='\033[1;31m'; B='\033[1;34m'; D='\033[0;90m'; N='\033[0m'
else G=''; Y=''; R=''; B=''; D=''; N=''; fi

PASS=0; WARN=0; FAIL=0
pass() { PASS=$((PASS+1)); printf "  ${G}✓ PASS${N} %s\n" "$*"; }
warn() { WARN=$((WARN+1)); printf "  ${Y}! WARN${N} %s\n" "$*"; }
fail() { FAIL=$((FAIL+1)); printf "  ${R}✗ FAIL${N} %s\n" "$*"; }
info() { printf "  ${D}· %s${N}\n" "$*"; }
section() { printf "\n${B}── %s ──${N}\n" "$*"; }

IS_ROOT=0; [[ "$(id -u)" -eq 0 ]] && IS_ROOT=1

# Read a KEY=value from an env file without sourcing it (avoids executing the
# file). Prints the raw value (last match wins) or empty. Caller must have read
# perms; returns 1 if the file is unreadable.
env_get() {
  local file="$1" key="$2"
  [[ -r "$file" ]] || return 1
  # strip optional surrounding quotes; take the last definition
  sed -n "s/^[[:space:]]*${key}=//p" "$file" | tail -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# Hostname out of a URL (no scheme/creds/path/port) — used to compare RPC
# providers WITHOUT printing API keys embedded in the full URL.
url_host() { sed -E 's#^[a-z]+://##; s#^[^@]*@##; s#[:/].*$##' <<<"$1"; }

# unit_state <unit> -> prints "load active sub enabled"
unit_state() {
  systemctl show "$1" -p LoadState -p ActiveState -p SubState -p UnitFileState \
    --value 2>/dev/null | paste -sd' ' -
}

# ─── 0. prerequisites ──────────────────────────────────────────────────
section "prerequisites"
if command -v systemctl >/dev/null; then pass "systemctl present"; else fail "systemctl missing — not a systemd host?"; fi
if command -v uv >/dev/null; then
  pass "uv present ($(command -v uv))"
else
  fail "uv not on PATH — both deploys need it"
fi
if command -v jq >/dev/null; then info "jq present (health JSON will be parsed)"; else info "jq absent (health check falls back to raw)"; fi
[[ $IS_ROOT -eq 1 ]] || warn "not root — /etc/*/.env reads and :${LIQ_PORT} owner mapping may be skipped (re-run with sudo for full coverage)"

# ─── 1. path isolation ─────────────────────────────────────────────────
section "path isolation (no shared directories)"
for pair in "MON_REPO:$MON_REPO" "LIQ_REPO:$LIQ_REPO" "MON_ETC:$MON_ETC" "LIQ_ETC:$LIQ_ETC"; do
  name="${pair%%:*}"; path="${pair#*:}"
  if [[ -d "$path" ]]; then info "$name = $path (exists)"; else warn "$name = $path (missing — that system not installed yet?)"; fi
done
if [[ "$MON_REPO" != "$LIQ_REPO" ]]; then pass "repo dirs distinct"; else fail "repo dirs collide: both at $MON_REPO"; fi
if [[ "$MON_ETC"  != "$LIQ_ETC"  ]]; then pass "config dirs distinct"; else fail "config dirs collide: both at $MON_ETC"; fi
# monitoring's writable cache must not sit inside the liquidity tree.
case "$MON_CACHE" in
  "$LIQ_REPO"/*|"$LIQ_REPO") fail "monitoring CACHE_DIR ($MON_CACHE) is inside the liquidity repo" ;;
  *) pass "monitoring CACHE_DIR ($MON_CACHE) outside liquidity tree" ;;
esac

# ─── 2. runner user & git credentials ──────────────────────────────────
section "runner user & git auth (shared deploy user)"
mon_owner="$(stat -c '%U' "$MON_REPO" 2>/dev/null || echo '?')"
liq_owner="$(stat -c '%U' "$LIQ_REPO" 2>/dev/null || echo '?')"
info "owner: $MON_REPO=$mon_owner   $LIQ_REPO=$liq_owner"
if [[ "$mon_owner" == "$liq_owner" && "$mon_owner" != "?" ]]; then
  info "both repos owned by '$mon_owner' — single deploy user (expected)."
  # A single ~/.git-credentials authenticates github.com for ALL repos, so the
  # token must grant access to BOTH (yearn/monitoring AND tapired/...). Probe.
  for repo in "$MON_REPO" "$LIQ_REPO"; do
    remote="$(sudo -u "$mon_owner" git -C "$repo" remote get-url origin 2>/dev/null)"
    if sudo -u "$mon_owner" git -C "$repo" ls-remote --exit-code origin -h >/dev/null 2>&1; then
      pass "git auth OK for $repo (origin: ${remote:-?})"
    else
      fail "git auth/reachability FAILED for $repo (origin: ${remote:-?}) — the shared deploy token may not cover this repo"
    fi
  done
elif [[ "$mon_owner" != "?" && "$liq_owner" != "?" ]]; then
  pass "repos owned by different users ($mon_owner / $liq_owner) — git creds isolated"
else
  warn "could not stat one repo owner — skipping git-auth probe"
fi

# ─── 3. systemd units (both present & healthy, names distinct) ──────────
section "systemd units"
for u in "$MON_UNIT" "$LIQ_UNIT"; do
  st="$(unit_state "$u")"
  read -r load active sub enabled <<<"$st"
  if [[ "$load" == "not-found" || -z "$load" ]]; then
    warn "$u not installed ($st)"
  elif [[ "$active" == "active" || "$active" == "activating" ]]; then
    pass "$u — $active/$sub, $enabled"
  else
    fail "$u — $active/$sub (expected active); check 'journalctl -u ${u%.service} -n50'"
  fi
done
# masked check — caught us out before.
for u in "$MON_UNIT" "$LIQ_UNIT"; do
  [[ "$(unit_state "$u" | awk '{print $1}')" == "masked" ]] && fail "$u is MASKED (systemctl unmask ${u%.service})"
done
for t in $LIQ_TIMERS; do
  st="$(unit_state "$t")"; active="$(awk '{print $2}' <<<"$st")"
  if [[ "$active" == "active" ]]; then pass "$t active"; else warn "$t not active ($st)"; fi
done

# ─── 4. port 8080 (liquidity only; exactly one listener; not stolen) ────
section "loopback port ${LIQ_PORT} (liquidity daemon)"
listeners=""
if command -v ss >/dev/null; then
  listeners="$(ss -ltnHp 2>/dev/null | awk -v p=":${LIQ_PORT}\$" '$4 ~ p {print}')"
elif command -v netstat >/dev/null; then
  listeners="$(netstat -ltnp 2>/dev/null | awk -v p=":${LIQ_PORT}\$" '$4 ~ p {print}')"
else
  warn "neither ss nor netstat available — cannot inspect port ${LIQ_PORT}"
fi
if command -v ss >/dev/null || command -v netstat >/dev/null; then
  count="$(grep -c . <<<"$listeners" 2>/dev/null || echo 0)"; [[ -z "$listeners" ]] && count=0
  if [[ "$count" -eq 0 ]]; then
    warn "nothing listening on ${LIQ_PORT} — liquidity daemon down, or it binds a different port"
  elif [[ "$count" -eq 1 ]]; then
    proc="$(grep -oE 'users:\(\("[^"]+"' <<<"$listeners" | head -1 | sed -E 's/.*\("//')"
    if [[ -z "$proc" && $IS_ROOT -eq 0 ]]; then
      pass "single listener on ${LIQ_PORT} (run as root to confirm it's the liquidity daemon)"
    elif [[ "$proc" =~ python|uvicorn|uv ]]; then
      pass "single listener on ${LIQ_PORT} owned by '$proc' (liquidity daemon)"
    else
      warn "single listener on ${LIQ_PORT} owned by '$proc' — expected uvicorn/python; is something else using it?"
    fi
  else
    fail "multiple ($count) listeners on ${LIQ_PORT} — port contention"
  fi
fi

# ─── 5. liquidity health probe ─────────────────────────────────────────
section "liquidity /healthz probe"
if command -v curl >/dev/null; then
  body="$(curl -fsS --max-time 5 "http://${LIQ_HOST}:${LIQ_PORT}/healthz" 2>/dev/null)"
  if [[ -n "$body" ]]; then
    if command -v jq >/dev/null && jq -e . >/dev/null 2>&1 <<<"$body"; then
      status="$(jq -r '.status // .ok // "?"' <<<"$body" 2>/dev/null)"
      pass "/healthz responded (status: ${status})"
    else
      pass "/healthz responded ($(head -c80 <<<"$body"))"
    fi
  else
    warn "/healthz unreachable on http://${LIQ_HOST}:${LIQ_PORT} — daemon down or still starting"
  fi
else
  warn "curl missing — cannot probe /healthz"
fi

# ─── 6. shared RPC providers (same IP → shared rate limit) ─────────────
section "shared RPC providers (read-only env compare)"
mon_env="$MON_ETC/.env"; liq_env="$LIQ_ETC/.env"
if [[ -r "$mon_env" && -r "$liq_env" ]]; then
  collect_hosts() {  # print sorted unique hostnames from RPC-ish URL values
    local f="$1"
    grep -iE '^[[:space:]]*[A-Z0-9_]*RPC[A-Z0-9_]*=' "$f" 2>/dev/null \
      | sed -E 's/^[^=]*=//' \
      | while read -r v; do v="${v%\"}"; v="${v#\"}"; [[ "$v" == http* ]] && url_host "$v"; done \
      | sort -u
  }
  shared="$(comm -12 <(collect_hosts "$mon_env") <(collect_hosts "$liq_env"))"
  if [[ -n "$shared" ]]; then
    warn "both deploys hit the same RPC host(s) from this IP — shared rate limit / ban radius:"
    while read -r h; do [[ -n "$h" ]] && info "shared: $h"; done <<<"$shared"
    info "consider separate API keys/providers per service if you see 429s."
  else
    pass "no overlapping RPC hostnames between the two envs"
  fi
else
  warn "cannot read both env files ($mon_env / $liq_env) — re-run as root (or a user in their groups) to compare RPC providers"
fi

# ─── 7. shared Telegram bot (possible cross-posting) ───────────────────
section "telegram bot overlap"
if [[ -r "$mon_env" && -r "$liq_env" ]]; then
  overlap=0
  # Compare every TELEGRAM_BOT_TOKEN* value present in monitoring against liquidity.
  while read -r key; do
    [[ -z "$key" ]] && continue
    mv="$(env_get "$mon_env" "$key")"; lv="$(env_get "$liq_env" "$key")"
    if [[ -n "$mv" && -n "$lv" && "$mv" == "$lv" ]]; then
      warn "both envs define $key with the SAME bot token — alerts may cross-post; use distinct bots/chats"
      overlap=1
    fi
  done < <(grep -hoE '^[[:space:]]*TELEGRAM_BOT_TOKEN[A-Z0-9_]*' "$mon_env" 2>/dev/null | tr -d ' ')
  [[ "$overlap" -eq 0 ]] && pass "no shared Telegram bot tokens detected"
else
  info "env files unreadable — skipping Telegram comparison (re-run as root)"
fi

# ─── 8. resource headroom (daemon + bursty cron on one box) ────────────
section "resource headroom"
if command -v free >/dev/null; then
  read -r total avail < <(free -m | awk '/^Mem:/ {print $2, $7}')
  if [[ -n "$avail" ]]; then
    pct=$(( avail * 100 / total ))
    if   [[ "$pct" -lt 10 ]]; then fail "only ${avail}MiB / ${total}MiB RAM available (${pct}%) — daemon + cron spikes will OOM"
    elif [[ "$pct" -lt 20 ]]; then warn "${avail}MiB / ${total}MiB RAM available (${pct}%) — tight when a cron tick overlaps the daemon"
    else pass "RAM headroom ${avail}MiB / ${total}MiB (${pct}%)"; fi
  fi
fi
disk_avail="$(df -Pm /srv 2>/dev/null | awk 'NR==2 {print $4}')"
if [[ -n "$disk_avail" ]]; then
  if   [[ "$disk_avail" -lt 1024 ]]; then fail "/srv has <1GiB free (${disk_avail}MiB) — venvs/journald/caches will fill it"
  elif [[ "$disk_avail" -lt 3072 ]]; then warn "/srv has ${disk_avail}MiB free — keep an eye on it"
  else pass "/srv free space ${disk_avail}MiB"; fi
fi
if command -v nproc >/dev/null; then
  cores="$(nproc)"; load1="$(awk '{print $1}' /proc/loadavg 2>/dev/null)"
  info "load1=${load1:-?} across ${cores} core(s)"
fi

# ─── summary ───────────────────────────────────────────────────────────
printf '\n%b── summary ──%b\n' "$B" "$N"
printf '  %b%d passed%b   %b%d warnings%b   %b%d failures%b\n' "$G" "$PASS" "$N" "$Y" "$WARN" "$N" "$R" "$FAIL" "$N"
if [[ "$FAIL" -gt 0 ]]; then
  printf '  %bintegration check FAILED — resolve the collisions above before relying on both services.%b\n' "$R" "$N"
  exit 1
fi
if [[ "$WARN" -gt 0 ]]; then
  printf '  %bcoexistence OK, with warnings to review (shared providers / headroom).%b\n' "$Y" "$N"
else
  printf '  %bclean — the two systems coexist with no detected collisions.%b\n' "$G" "$N"
fi
exit 0
