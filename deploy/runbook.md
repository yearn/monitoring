# Operations Runbook — yearn-monitor cron runner

`yearn-monitor` is the single point of execution for the monitoring scripts on
the VPS. If it goes down the scheduled checks do not run and alerts go silent.
This runbook covers the operations on-call needs to recover from common
failures.

All command examples assume you are SSH'd into the VPS as the deploy user (the
same account the systemd unit runs as), with the repository checked out at
`/srv/yearn-monitoring`.

No Docker, no compose, no Caddy, no reverse proxy. One systemd unit runs
[supercronic](https://github.com/aptible/supercronic), which executes
`python -m automation run <profile>` on each cron tick per
`automation/jobs.yaml`.

---

## What's running

```sh
systemctl status yearn-monitor
```

See the schedule supercronic is actually running (rendered from `jobs.yaml`):

```sh
cd /srv/yearn-monitoring
uv run python -m automation list           # profiles + cron + task counts
uv run python -m automation render-crontab # the exact crontab supercronic runs
```

supercronic logs every job start and exit to journald (see below) — that's the
record of what ran and when.

---

## View logs

```sh
journalctl -u yearn-monitor -f --since '15 min ago'
```

supercronic prints a structured line per job (`channel=stdout`, `job.position`,
exit status). Filter to a single profile run by grepping the lock command:

```sh
journalctl -u yearn-monitor --since 1h | grep 'automation run hourly'
```

The Python tasks themselves log at `LOG_LEVEL` (default INFO). A profile with
failing tasks also posts one Telegram digest (see Failure handling below), so
you usually learn about a failure there before reading journald.

---

## Manually rerun a profile

Useful when a tick was skipped, or to validate a hand-edited config. Replaces
`workflow_dispatch`.

```sh
cd /srv/yearn-monitoring
# Print what would run without executing:
uv run python -m automation run hourly --dry-run
# Actually run it (sends real alerts / Telegram on failure):
uv run python -m automation run hourly
```

Available profiles: `hourly`, `yearn-stuck-triggers`, `daily`, `weekly`,
`multisig` (see `automation/jobs.yaml`).

---

## Propagating code or config edits

supercronic re-spawns `python -m automation run …` fresh on every tick, so for
any Python-only change (a script, `automation/jobs.yaml`, a config module):

```sh
cd /srv/yearn-monitoring
git pull --ff-only
sudo systemctl restart yearn-monitor   # re-renders the crontab from jobs.yaml
```

The restart re-renders the crontab (picking up cron/profile changes) and points
supercronic at the freshly-pulled tree. Total downtime is a few seconds and
only affects the scheduler, not an in-flight job.

For dependency changes (`pyproject.toml` / `uv.lock`), re-sync the venv first:

```sh
cd /srv/yearn-monitoring
git pull --ff-only
uv sync --frozen
sudo systemctl restart yearn-monitor
```

---

## Rotate a secret (Telegram token, RPC URL, API key)

Secrets live in `/etc/yearn-monitoring/.env` (mode 0640, root:<deploy-user>),
loaded by the unit via `EnvironmentFile`. They are **not** in git. Edit in place
and restart:

```sh
sudo $EDITOR /etc/yearn-monitoring/.env
sudo systemctl restart yearn-monitor
```

The restart re-reads the env and re-renders the crontab. Anyone who could read
the file has seen the old values, so a leaked credential should be rotated at
the provider, not just edited here.

---

## Host failover

Single-node failover (cattle, not pets):

1. Provision a fresh VPS: `sudo bash deploy/install.sh` (installs uv/Python/
   supercronic, clones the repo, creates the venv, installs the systemd unit).
   It can also be curled — see the header of `install.sh`.
2. Drop the production env at `/etc/yearn-monitoring/.env` (mode 0640,
   root:<deploy-user>) — copy it from the old host or recreate from
   `.env.example`.
3. Start it:
   ```sh
   sudo systemctl enable --now yearn-monitor
   systemctl status yearn-monitor
   ```
4. Confirm the schedule and the first ticks:
   ```sh
   cd /srv/yearn-monitoring && uv run python -m automation render-crontab
   journalctl -u yearn-monitor -f
   ```
5. Stop the old host so alerts aren't sent twice:
   ```sh
   sudo systemctl disable --now yearn-monitor
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
| `Active: failed` on start | `journalctl -u yearn-monitor -n 50` | Malformed `automation/jobs.yaml` (render-crontab aborts the start), or `/etc/yearn-monitoring/.env` missing (the unit refuses to start without it). |
| Telegram suddenly silent | Is `TELEGRAM_BOT_TOKEN_DEFAULT` valid? `LOG_LEVEL=DEBUG` skips sends. | Bot revoked, chat removed bot, or LOG_LEVEL left at DEBUG. |
| One profile never runs | `uv run python -m automation render-crontab` — is its line present? | Profile/task `enabled: false` in jobs.yaml, or its `flock` lock is stuck held by a hung run (restart clears it). |
| `ModuleNotFoundError` after a deploy | `journalctl -u yearn-monitor -n 50` | Forgot `uv sync --frozen` after a `pyproject.toml`/`uv.lock` change. |
| Cache/dedupe acting up | `ls -l /srv/cache` | Wrong perms (must be writable by the runner user) or a corrupt cache file — safe to delete; it re-seeds. |

---

## Where things live

- Source tree: `/srv/yearn-monitoring` (owned by the deploy user).
- Python venv: `/srv/yearn-monitoring/.venv` (created by `uv sync`).
- Cache / dedupe state: `/srv/cache` (owned by the deploy user; the unit grants
  it via `ReadWritePaths`). Paths are set per-profile in `automation/jobs.yaml`.
- Env file: `/etc/yearn-monitoring/.env` (mode 0640, root:<deploy-user>;
  operator-supplied, not in git).
- systemd unit: `/etc/systemd/system/yearn-monitor.service`.
- Rendered crontab: `/tmp/crontab` (per-service `PrivateTmp`; regenerated on
  every start).
