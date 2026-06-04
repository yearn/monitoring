# automation

Single source of truth for what runs on the Hetzner monitoring box.

## Files

- [`jobs.yaml`](./jobs.yaml) — profiles + cron + task lists
- [`config.py`](./config.py) — schema + parser
- [`runner.py`](./runner.py) — executes one profile, posts Telegram digest on failure
- [`__main__.py`](./__main__.py) — CLI: `list`, `render-crontab`, `run`

## CLI

```bash
# List profiles
uv run python -m automation list

# Print the crontab supercronic will execute
uv run python -m automation render-crontab

# Run a profile locally (executes the scripts)
uv run python -m automation run weekly

# Same, but only print the argv that would run
uv run python -m automation run weekly --dry-run
```

## Adding a script

1. Open `jobs.yaml`, find the profile whose cadence you want (`hourly` / `daily` / `weekly` / …).
2. Append a task:
   ```yaml
   - { name: "my-protocol", script: my-protocol/main.py }
   ```
   If the script needs CLI flags, use `args:`:
   ```yaml
   - name: "my-protocol"
     script: my-protocol/main.py
     args:
       cache-file: /srv/cache/my-protocol.json
   ```
3. `uv run python -m automation render-crontab` — confirm the line you expect appears.
4. Merge to `main`, then on the VPS `git pull` and `sudo systemctl restart yearn-monitor` to
   re-render the crontab (see [deploy/runbook.md](../deploy/runbook.md)).

## Profile shape

```yaml
profiles:
  <name>:
    cron: "<5-field cron expression>"
    enabled: true            # optional, default true
    description: "…"         # optional, for humans
    env:                     # optional, exported to every task in this profile
      CACHE_FILENAME: /srv/cache/foo.txt
    tasks:
      - name: "task-id"
        script: foo/main.py
        args: { cache-file: /srv/cache/bar.json }   # optional
        enabled: true                               # optional, default true
```

## Failure handling

The runner runs every task in declared order, capturing each subprocess's exit code (and its stdout/stderr) without aborting the profile. After all tasks finish, if any failed, a single Markdown digest is posted to Telegram via `utils.telegram.send_telegram_message` with `protocol="automation"` — set `TELEGRAM_BOT_TOKEN_AUTOMATION` / `TELEGRAM_CHAT_ID_AUTOMATION` in `.env` to route it to a dedicated channel, otherwise it falls back to `TELEGRAM_BOT_TOKEN_DEFAULT` / `TELEGRAM_CHAT_ID_DEFAULT`.

Each failing task contributes a header line (`❌ *name* (exit N, Ds)`) and, when the script produced output, the last few lines of its error tail in a fenced code block — so the alert is actionable without an SSH/`journalctl` round-trip. The tail prefers stderr (uncaught tracebacks) and falls back to stdout (`utils.logging` output). The full captured output is still re-emitted to the daemon logs at `warning` level; only the short tail (4 lines / 500 chars) travels to Telegram. Because the tail lives inside a `` ``` `` fence, tracebacks containing Markdown metacharacters (`_`, `*`, `[`) can't break parsing and silently drop the digest.

The profile's exit code is non-zero if any task failed, which supercronic surfaces in the journald logs (`journalctl -u yearn-monitor`).

### The `automation` Telegram channel

This channel is **errors-only**: a digest is sent *only* when one or more tasks in a profile fail. A run where every task exits 0 sends nothing, so it's safe to treat as a dedicated "internal automation errors" group — point `TELEGRAM_*_AUTOMATION` at a chat used for nothing else. (Leave those env vars unset and the failure digests fall into the `DEFAULT` channel and mix with everything else.)

Scope, so you know what does and doesn't land here:

- **Runner-level failures only.** It reports a monitoring script crashing or exiting non-zero (including subprocess spawn failures). It is *not* where the protocols' normal monitoring alerts go — those are still sent by each script to its own protocol channel (`TELEGRAM_*_AAVE`, etc.).
- **One digest per profile run, not per task.** If three tasks in the `hourly` profile fail, you get one message listing all three.
- **Per-script crashes route elsewhere.** An unhandled exception inside a script wrapped with `run_with_alert` (see [CLAUDE.md](../CLAUDE.md)) is alerted to *that script's* protocol channel, not the `automation` channel.

## Locking

`render-crontab` wraps each invocation in `flock -n /tmp/automation.<profile>.lock` so consecutive ticks can't overlap a still-running profile (mirrors `concurrency: cancel-in-progress: true` on the existing GH Actions workflows). `flock -n` returns non-zero immediately if the lock is held; supercronic logs the skip and the next tick tries again.
