# Cutover Playbook — GitHub Actions → VPS (supercronic)

Operator-facing runbook for moving the scheduled monitoring off GitHub Actions
and onto the `yearn-monitor` systemd unit on the VPS.

Unlike a signing daemon, this is **pure read-only monitoring**: every job just
reads chain/API state and posts Telegram alerts, deduped by a file cache. So
there's no nonce race, no on-chain blast radius, and **no HTTP/localhost trigger
surface to build** — the "local trigger" is just supercronic firing the cron.
Cutover means: stop GitHub from firing the crons, let supercronic fire them,
without double-alerting operators or leaving a gap.

The whole sequence is ~1 week of shadow running followed by a single flip.

---

## What's being cut over

Four scheduled workflows (each `schedule:` + `workflow_dispatch:`, calling the
reusable `_run-monitoring.yml`):

| GitHub workflow | VPS profile (`automation/jobs.yaml`) | cron |
|---|---|---|
| `hourly.yml` | `hourly` + `yearn-stuck-triggers` | `26 * * * *` |
| `daily.yml` | `daily` | `19 8 * * *` |
| `weekly.yml` | `weekly` | `19 8 * * 0` |
| `multisig-checker.yml` | `multisig` | `0/10 * * * *` |

`ci.yml` is CI (push / PR / dispatch) and stays on GitHub Actions — it is **not**
part of this cutover. There are no `repository_dispatch` or cross-repo triggers.

The manual equivalent of `workflow_dispatch` on the VPS is:

```sh
cd /srv/yearn-monitoring && uv run python -m automation run <profile>
```

---

## Pre-flight

1. VPS provisioned per `deploy/runbook.md` (`install.sh` run, venv in place, the
   `yearn-monitor` unit installed) **but not yet enabled**.
2. `uv run python -m automation render-crontab` on the box shows the five
   expected lines.
3. A separate **shadow** Telegram chat exists. Grab its chat id — the VPS posts
   there during the shadow week so it doesn't double-alert operators alongside
   the live GitHub Actions output.

### The shadow env (important — read the routing note)

`utils/telegram.py` routes two ways, with an asymmetric fallback:

- **Topic mode:** if `TELEGRAM_TOPIC_ID_<PROTOCOL>` is set, the alert posts to
  `TELEGRAM_CHAT_ID_TOPICS` as a thread.
- **Legacy per-protocol:** `bot_token` falls back to `..._DEFAULT`, but
  **`chat_id` does not** — if `TELEGRAM_CHAT_ID_<PROTOCOL>` is unset, the alert
  is **dropped** with a "Missing Telegram credentials" warning.

So a naive shadow that sets only `..._DEFAULT` would silently drop every
per-protocol alert, and a topic-mode shadow would post to non-existent threads.
The reliable recipe is to funnel **everything** into the one shadow chat:

> Start from the production env, then **set every `TELEGRAM_CHAT_ID_*` value**
> (DEFAULT, TOPICS, and each per-protocol one) **to the shadow chat id**, and
> **delete every `TELEGRAM_TOPIC_ID_*`**. Keep the bot token(s) and all RPC /
> API keys real — they're read-only.

Concretely, on a copy of the prod env:

```sh
# point all chat ids at the shadow chat, drop all topic ids
sed -i -E "s/^(TELEGRAM_CHAT_ID_[A-Z0-9_]+)=.*/\1=<SHADOW_CHAT_ID>/" /etc/yearn-monitoring/.env
sed -i -E "/^TELEGRAM_TOPIC_ID_[A-Z0-9_]+=/d"                         /etc/yearn-monitoring/.env
```

> Do **not** use `LOG_LEVEL=DEBUG` to "mute" — it skips *all* Telegram sends, so
> you'd verify nothing.

---

## Shadow week (~7 days)

```sh
sudo systemctl enable --now yearn-monitor
journalctl -u yearn-monitor -f
```

GitHub Actions keeps running and posting to the **real** channels; the VPS posts
only to the **shadow** chat. Each day:

- Compare shadow-chat messages vs the real channels — for a given profile they
  should line up (same scripts, same schedule).
- Watch journald for task failures the VPS hits that GitHub didn't (a missing
  env var, a path/permission issue under the hardened unit).

This week also **warms `/srv/cache`**: by cutover the dedupe state is already
populated, so there's no cold-start alert burst when the VPS goes live.

If you see drift, **extend the shadow week** — don't rush the flip.

---

## The flip

Order matters. Duplicate alerts are merely annoying (deduped by cache); a **gap**
means missed alerts. So **go live before disabling GitHub**:

1. Restore the **real** channels in the VPS env (put back the production
   `TELEGRAM_CHAT_ID_*` and any `TELEGRAM_TOPIC_ID_*`), then:

   ```sh
   sudo systemctl restart yearn-monitor
   journalctl -u yearn-monitor -f
   ```

2. Confirm one good real tick lands in production Telegram. `multisig` (every
   10 min) is the fastest signal; for `hourly` wait for the `:26` tick.

3. Disable the GitHub crons so they stop double-firing:

   ```sh
   gh workflow disable hourly.yml daily.yml weekly.yml multisig-checker.yml \
     --repo yearn/monitoring
   ```

   (Or comment out just the `schedule:` block in each workflow via a PR, keeping
   `workflow_dispatch` as a manual fallback.)

Because everything is read-only and dedup-cached, an all-at-once flip is fine —
no per-profile staging needed. If you want extra caution, disable
`weekly.yml` / `daily.yml` first and `multisig-checker.yml` / `hourly.yml` last,
but it's optional.

---

## Rollback

No state to unwind — re-enable GitHub and quiet the VPS:

```sh
gh workflow enable hourly.yml daily.yml weekly.yml multisig-checker.yml \
  --repo yearn/monitoring
# then either stop the VPS unit…
sudo systemctl stop yearn-monitor
# …or flip its env back to the shadow chat and restart, to keep observing.
```

Both systems run in parallel again while you investigate, without time pressure.

---

## Done — cleanup

When all four profiles are live on the VPS:

- Keep the four workflow files **disabled, not deleted**, for at least 30 days as
  instant rollback. `workflow_dispatch` stays usable for manual one-offs.
- The `actions/cache` restore/save steps in `_run-monitoring.yml` only existed to
  fake persistence on ephemeral runners — once the GitHub crons are retired they
  are dead weight and can be stripped (separate PR).
- Update `deploy/runbook.md` to note the cutover is complete and drop the
  GitHub-Actions-era pointers.
