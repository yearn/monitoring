# Operations Runbook — monitoring cron runner

`monitoring` is the single point of execution for the monitoring scripts on
the VPS. If it goes down the scheduled checks do not run and alerts go silent.
This runbook covers the operations on-call needs to recover from common
failures.

All command examples assume you are SSH'd into the VPS as the deploy user (the
same account the systemd unit runs as), with the repository checked out at
`/srv/monitoring`.

No Docker, no compose, no Caddy, no reverse proxy. One systemd unit runs
[supercronic](https://github.com/aptible/supercronic), which executes
`python -m automation run <profile>` on each cron tick per
`automation/jobs.yaml`.

---

## What's running

```sh
systemctl status monitoring
```

See the schedule supercronic is actually running (rendered from `jobs.yaml`):

```sh
cd /srv/monitoring
uv run python -m automation list           # profiles + cron + task counts
uv run python -m automation render-crontab # the exact crontab supercronic runs
```

supercronic logs every job start and exit to journald (see below) — that's the
record of what ran and when.

---

## View logs

```sh
journalctl -u monitoring -f --since '15 min ago'
```

supercronic prints a structured line per job (`channel=stdout`, `job.position`,
exit status). Filter to a single profile run by grepping the lock command:

```sh
journalctl -u monitoring --since 1h | grep 'automation run hourly'
```

The Python tasks themselves log at `LOG_LEVEL` (default INFO). A profile with
failing tasks also posts one Telegram digest (see Failure handling below), so
you usually learn about a failure there before reading journald.

---

## Manually rerun a profile

Useful when a tick was skipped, or to validate a hand-edited config. Replaces
`workflow_dispatch`.

```sh
cd /srv/monitoring
# Print what would run without executing:
uv run python -m automation run hourly --dry-run
# Actually run it (sends real alerts / Telegram on failure):
uv run python -m automation run hourly
```

Available profiles: `hourly`, `yearn-stuck-triggers`, `daily`, `weekly`,
`multisig` (see `automation/jobs.yaml`).

---

## Propagating code or config edits

Contributors merge to `main` through PRs; nobody normally SSHes in to ship code.
Two things move `main` onto the box, split on *"does the change need the
scheduler restarted to take effect?"*

**Auto-sync (no restart, the common case).** The `multisig` profile runs
`git pull --ff-only` before its tasks every 10 minutes (`sync_before_run: true`
in `jobs.yaml`). Since supercronic re-spawns every profile fresh against the
on-disk tree, that one pull keeps the whole checkout current for *all* profiles
within ~10 minutes — every other profile rides along for free. The pull is
best-effort: a failure is logged (`journalctl -u monitoring | grep 'pre-run git
sync'`) and the multisig checks run against the existing checkout anyway, so a
git hiccup never silences an alert. This covers **scripts, config modules, data,
and `jobs.yaml` task bodies** — anything read fresh by a subprocess.

So for the common case — a script tweak, a new task in a profile — **merge the
PR and the next ~10-min multisig tick pulls it in; no SSH needed.**

**Manual restart (cadence + deps).** Two kinds of change land on disk via the
auto-sync but stay inert until a restart, because they're read once at scheduler
boot, not per-tick:

- a `cron:` *cadence* change in `jobs.yaml` (the crontab is rendered at unit
  start by `ExecStartPre`), and
- a `pyproject.toml` / `uv.lock` change (the venv).

For those, after the PR merges:

```sh
cd /srv/monitoring
git pull --ff-only            # or just let the next multisig tick land it
uv sync --frozen --extra ai   # only if pyproject.toml / uv.lock changed (--extra ai: openai client for the AI explainer)
sudo systemctl restart monitoring   # re-renders the crontab and re-points supercronic at the tree
```

The restart re-renders the crontab and points supercronic at the freshly-pulled
tree. Total downtime is a few seconds and only affects the scheduler, not an
in-flight job.

---

## Rotate a secret (Telegram token, RPC URL, API key)

Secrets live in `/etc/monitoring/.env` (mode 0640, root:<deploy-user>),
loaded by the unit via `EnvironmentFile`. They are **not** in git. Edit in place
and restart:

```sh
sudo $EDITOR /etc/monitoring/.env
sudo systemctl restart monitoring
```

The restart re-reads the env and re-renders the crontab. Anyone who could read
the file has seen the old values, so a leaked credential should be rotated at
the provider, not just edited here.

---

## Test / staging run (route every alert to one dummy group)

To validate the whole fleet without spamming production chats — e.g. comparing
this VPS's output against the old GitHub Actions runs — set
`TELEGRAM_TEST_CHAT_ID` in the env. While it is set, **every** alert from every
protocol is sent to that single chat via the default bot, prefixed with a
`[protocol]` label and with no topic threading, so production routing
(`TELEGRAM_TOPIC_ID_*` / per-protocol chats) is bypassed entirely:

```sh
sudo $EDITOR /etc/monitoring/.env
#   TELEGRAM_TEST_CHAT_ID=-1001234567890   # the dummy group
#   (the default bot, TELEGRAM_BOT_TOKEN_DEFAULT, must be a member of it)
sudo systemctl restart monitoring
```

Keep `LOG_LEVEL=INFO` (the default) — `LOG_LEVEL=DEBUG` skips all Telegram sends,
so nothing would arrive. Comment the line out and restart to restore normal
per-protocol routing.

---

## Host failover

Single-node failover (cattle, not pets):

1. Provision a fresh VPS: `sudo bash deploy/install.sh` (installs uv/Python/
   supercronic, clones the repo, creates the venv, installs the systemd unit).
   It can also be curled — see the header of `install.sh`.
2. Drop the production env at `/etc/monitoring/.env` (mode 0640,
   root:<deploy-user>) — copy it from the old host or recreate from
   `.env.example`.
3. Start it:
   ```sh
   sudo systemctl enable --now monitoring
   systemctl status monitoring
   ```
4. Confirm the schedule and the first ticks:
   ```sh
   cd /srv/monitoring && uv run python -m automation render-crontab
   journalctl -u monitoring -f
   ```
5. Stop the old host so alerts aren't sent twice:
   ```sh
   sudo systemctl disable --now monitoring
   ```

---

## Watchdog

systemd restarts the unit on crash (`Restart=on-failure`, capped at 5
restarts/60s). There is no HTTP healthcheck (and no daemon to wedge): supercronic
is a foreground process whose death systemd observes directly. If supercronic is
running, the crontab is being ticked. If a single job hangs it blocks only its
own profile (each is wrapped in `flock -n`, so the next tick of that profile is
skipped, not queued) — the others keep ticking.

---

## Common failure modes

| Symptom | First thing to check | Likely cause |
|---|---|---|
| `Active: failed` on start | `journalctl -u monitoring -n 50` | Malformed `automation/jobs.yaml` (render-crontab aborts the start), or `/etc/monitoring/.env` missing (the unit refuses to start without it). |
| Telegram suddenly silent | Is `TELEGRAM_BOT_TOKEN_DEFAULT` valid? `LOG_LEVEL=DEBUG` skips sends. | Bot revoked, chat removed bot, or LOG_LEVEL left at DEBUG. |
| One profile never runs | `uv run python -m automation render-crontab` — is its line present? | Profile/task `enabled: false` in jobs.yaml, or its `flock` lock is stuck held by a hung run (restart clears it). |
| `ModuleNotFoundError` after a deploy | `journalctl -u monitoring -n 50` | Forgot `uv sync --frozen` after a `pyproject.toml`/`uv.lock` change. |
| Cache/dedupe acting up | `ls -l /srv/cache` | Wrong perms (must be writable by the runner user) or a corrupt cache file — safe to delete; it re-seeds. |

---

## Where things live

- Source tree: `/srv/monitoring` (owned by the deploy user).
- Python venv: `/srv/monitoring/.venv` (created by `uv sync`).
- Cache / dedupe state: `/srv/cache` (owned by the deploy user; the unit grants
  it via `ReadWritePaths` and sets `CACHE_DIR=/srv/cache`, which `utils.cache`
  resolves every cache file against). A profile only overrides a cache *basename*
  in `automation/jobs.yaml` when it needs an isolated file (e.g. daily).
- Env file: `/etc/monitoring/.env` (mode 0640, root:<deploy-user>;
  operator-supplied, not in git).
- systemd unit: `/etc/systemd/system/monitoring.service`.
- Rendered crontab: `/tmp/crontab` (per-service `PrivateTmp`; regenerated on
  every start).
- Code sync: no separate unit — the `multisig` profile's pre-run `git pull
  --ff-only` (`sync_before_run` in `jobs.yaml`) every 10 min. The unit grants
  the repo write access via `ReadWritePaths=/srv/cache /srv/monitoring` so the
  pull can update `.git/` under `ProtectSystem=strict`.
