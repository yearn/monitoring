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

# Print the crontab supercronic will execute inside the container
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
4. Rebuild the image (`docker compose build` locally, or merge to `main` and let GHA publish — see #257).

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

The runner runs every task in declared order, capturing each subprocess's exit code without aborting the profile. After all tasks finish, if any failed, a single Markdown digest is posted to Telegram via `utils.telegram.send_telegram_message` with `protocol="automation"` — set `TELEGRAM_BOT_TOKEN_AUTOMATION` / `TELEGRAM_CHAT_ID_AUTOMATION` in `.env` to route it to a dedicated channel, otherwise it falls back to `TELEGRAM_BOT_TOKEN_DEFAULT` / `TELEGRAM_CHAT_ID_DEFAULT`.

The profile's exit code is non-zero if any task failed, which supercronic surfaces in container logs.

## Locking

`render-crontab` wraps each invocation in `flock -n /tmp/automation.<profile>.lock` so consecutive ticks can't overlap a still-running profile (mirrors `concurrency: cancel-in-progress: true` on the existing GH Actions workflows). `flock -n` returns non-zero immediately if the lock is held; supercronic logs the skip and the next tick tries again.
