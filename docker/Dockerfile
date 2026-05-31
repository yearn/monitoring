# syntax=docker/dockerfile:1.7
#
# NOTE: the `# syntax=` directive MUST be the very first line — Docker stops looking for
# parser directives once it sees a regular comment or blank line. Without it BuildKit
# ignores `RUN --mount=type=cache` below and falls back to the legacy frontend.
#
# Builds the cron container that runs the yearn/monitoring scripts on the Hetzner box.
#
# Layout:
#   - Stage `builder` installs uv + dependencies into /app/.venv. Deps are baked in at
#     build time; the runtime container never runs `uv sync`.
#   - Stage `runtime` carries the venv + source + supercronic + flock. Non-root.

ARG PYTHON_VERSION=3.12
ARG SUPERCRONIC_VERSION=v0.2.34
# Tied to ${SUPERCRONIC_VERSION}; bump both together. Verified for linux/amd64.
ARG SUPERCRONIC_SHA256=a51b340a83c5bd035742f0d7191555f9663876405e494dbf824537d64f3e39c6

FROM python:${PYTHON_VERSION}-slim AS builder

# Pin uv via the official static image so the builder is reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# Resolve deps first against just the lockfile so the layer caches across code-only edits.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Now copy the source. Listing the script dirs explicitly (rather than `COPY . .`) keeps
# stray repo-root files (notes, CLAUDE.md, .git, .worktrees, etc.) out of the image.
COPY 3jane ./3jane
COPY aave ./aave
COPY automation ./automation
COPY bad-debt ./bad-debt
COPY cap ./cap
COPY common-abi ./common-abi
COPY compound ./compound
COPY ethena ./ethena
COPY euler ./euler
COPY fluid ./fluid
COPY infinifi ./infinifi
COPY lido ./lido
COPY lrt-pegs ./lrt-pegs
COPY maker ./maker
COPY maple ./maple
COPY moonwell ./moonwell
COPY morpho ./morpho
COPY pendle ./pendle
COPY resolv ./resolv
COPY rtoken ./rtoken
COPY safe ./safe
COPY silo ./silo
COPY spark ./spark
COPY stables ./stables
COPY stargate ./stargate
COPY strata ./strata
COPY timelock ./timelock
COPY usd0 ./usd0
COPY usdai ./usdai
COPY ustb ./ustb
COPY utils ./utils
COPY yearn ./yearn

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen


FROM python:${PYTHON_VERSION}-slim AS runtime

ARG SUPERCRONIC_VERSION
ARG SUPERCRONIC_SHA256

# - ca-certificates: scripts hit Telegram, RPC endpoints, GraphQL APIs over HTTPS.
# - util-linux:     provides `flock`, used to gate overlapping profile runs.
# - curl:           one-shot for the supercronic download; removed before the layer closes.
# - tini:           PID 1 reaper so supercronic-spawned Python subprocesses don't zombify.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates util-linux curl tini \
    && curl -fsSL -o /usr/local/bin/supercronic \
       "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 app \
    && useradd  --system --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /srv/cache \
    && chown app:app /srv/cache

WORKDIR /app
COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod +x /app/docker/entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REPO_ROOT=/app \
    # supercronic picks SHELL from env to exec crontab commands; the app user's login
    # shell is /usr/sbin/nologin (defense-in-depth) which can't `-c`, so override here.
    SHELL=/bin/sh

USER app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/docker/entrypoint.sh"]
