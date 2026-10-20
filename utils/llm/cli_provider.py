"""LLM providers backed by locally installed agent CLIs (Codex, Claude Code).

These run on the CLI's own login/subscription instead of a metered API key, so
the factory tries them before the API provider. Each call runs non-interactively
with approvals disabled (``codex exec --yolo`` / ``claude -p
--dangerously-skip-permissions``) so the CLI never blocks on a prompt, inside a
fresh temp directory so neither agent picks up this repo's AGENTS.md/CLAUDE.md.

Both receive the same prompt and system prompt as the API providers. Claude is
also cut off from user settings, rules and skills. Codex gets every configured
MCP server explicitly disabled plus the apps/plugins features turned off, but
still loads the host user's global ``~/.codex/AGENTS.md`` and
``~/.agents/skills`` (there is no supported switch for those).

Any process-level failure (binary missing, non-zero exit, timeout, auth or
credit errors) raises ``LLMUnavailableError`` so the fallback chain moves on.
"""

import json
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from utils.llm.base import LLMError, LLMProvider, LLMUnavailableError
from utils.logger import get_logger

logger = get_logger("utils.llm.cli_provider")

DEFAULT_CLI_TIMEOUT_SECONDS = 300
# Cap on CLI stderr/result text quoted in error messages.
_ERROR_SNIPPET_CHARS = 500
# `codex mcp list` reads config only; it should never take long.
_CODEX_MCP_LIST_TIMEOUT_SECONDS = 30
# Server names codex's `-c` dotted-path parser can address. It splits on dots
# even inside quotes and keeps quotes as part of the key, so other names can't
# be disabled per server and fail closed instead.
_CODEX_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _tail(text: str) -> str:
    """Return the last ``_ERROR_SNIPPET_CHARS`` of ``text``, stripped, for error messages."""
    return text.strip()[-_ERROR_SNIPPET_CHARS:]


def _kill_process_group(pgid: int) -> None:
    """SIGKILL every process in ``pgid``; a group that's already gone is fine."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_cli(label: str, args: list[str], prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a CLI with ``prompt`` on stdin, mapping process failures to ``LLMUnavailableError``.

    The CLI is an agent that can spawn its own commands, so it runs as the leader
    of a new process group and the whole group is killed when it exits or times
    out — otherwise agent-spawned children outlive the call. Output goes to temp
    files rather than pipes so a backgrounded child holding stdout open can't
    keep us waiting after the CLI itself has exited.

    Args:
        label: Provider name used in error messages.
        args: Full argv, binary first.
        prompt: Text written to the process's stdin.
        cwd: Working directory for the process.
        timeout: Seconds before the process group is killed.

    Returns:
        The completed process (exit code 0).

    Raises:
        LLMUnavailableError: If the binary is missing, times out, or exits non-zero.
    """
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=out, stderr=err, text=True, cwd=cwd, start_new_session=True
            )
        except FileNotFoundError as e:
            raise LLMUnavailableError(f"{label}: binary not found ({args[0]})") from e
        except OSError as e:
            raise LLMUnavailableError(f"{label}: failed to start: {e}") from e
        try:
            proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _kill_process_group(proc.pid)
            proc.wait()
            raise LLMUnavailableError(f"{label}: timed out after {timeout}s") from e
        finally:
            # Leftover agent-spawned processes; also covers the timeout path's raise.
            _kill_process_group(proc.pid)
        out.seek(0)
        err.seek(0)
        result = subprocess.CompletedProcess(args, proc.returncode, out.read(), err.read())
    if result.returncode != 0:
        detail = _tail(result.stderr) or _tail(result.stdout)
        raise LLMUnavailableError(f"{label}: exited with code {result.returncode}: {detail}")
    return result


def _parse_json_object(label: str, raw: str) -> dict[str, Any]:
    """Decode ``raw`` as a JSON object or raise ``LLMError``."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"{label}: structured response was not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise LLMError(f"{label}: structured response was not a JSON object")
    return parsed


class CodexCLIProvider(LLMProvider):
    """LLM provider that shells out to ``codex exec --yolo``."""

    def __init__(self, binary: str, model: str = "", timeout: int = DEFAULT_CLI_TIMEOUT_SECONDS) -> None:
        """Initialize the provider.

        Args:
            binary: Path to the ``codex`` executable.
            model: Optional model override; empty uses the CLI's configured default.
            timeout: Seconds to wait for a single completion.
        """
        self._binary = binary
        self._model = model
        self._timeout = timeout
        self._isolation_args: list[str] | None = None
        logger.info("Initialized Codex CLI provider: binary=%s model=%s", binary, model or "<cli default>")

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Generate a completion via ``codex exec``."""
        return self._exec(prompt, system_prompt, schema=None)

    @property
    def supports_structured_output(self) -> bool:
        """Codex enforces ``--output-schema`` on the final message."""
        return True

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        """Request a schema-constrained final message via ``--output-schema`` and parse it."""
        return _parse_json_object("codex", self._exec(prompt, system_prompt, schema=schema))

    def _mcp_isolation_args(self) -> list[str]:
        """Return codex flags that disable every configured MCP server, apps and plugins.

        ``--config mcp_servers={}`` does not work: codex merges overrides into the
        loaded config, so existing servers stay enabled. Each server has to be
        disabled by name, so the configured servers are listed once per process.

        Raises:
            LLMUnavailableError: If the servers can't be listed or a name can't be
                addressed. Codex is skipped rather than run with MCP access.
        """
        if self._isolation_args is not None:
            return self._isolation_args
        with tempfile.TemporaryDirectory(prefix="llm-codex-mcp-") as tmp:
            proc = _run_cli(
                "codex mcp list",
                [self._binary, "mcp", "list", "--json"],
                "",
                Path(tmp),
                _CODEX_MCP_LIST_TIMEOUT_SECONDS,
            )
        try:
            servers = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMUnavailableError(f"codex: could not parse MCP server list to disable it: {e}") from e
        if not isinstance(servers, list):
            raise LLMUnavailableError("codex: MCP server list was not a JSON array")

        args = ["--disable", "apps", "--disable", "plugins"]
        for server in servers:
            name = server.get("name") if isinstance(server, dict) else None
            if not isinstance(name, str) or not _CODEX_MCP_NAME_RE.match(name):
                raise LLMUnavailableError(f"codex: cannot disable MCP server {name!r}; refusing to run with it enabled")
            args += ["--config", f"mcp_servers.{name}.enabled=false"]
        logger.info("Codex CLI: disabling %d MCP server(s) for explainer runs", len(servers))
        self._isolation_args = args
        return args

    def _exec(self, prompt: str, system_prompt: str, schema: dict[str, Any] | None) -> str:
        """Run one ``codex exec`` session and return its last message.

        ``codex exec`` has no system-prompt flag, so static instructions are
        prepended to the prompt in a tagged block.
        """
        if system_prompt:
            prompt = f"<system_instructions>\n{system_prompt}\n</system_instructions>\n\n{prompt}"
        isolation_args = self._mcp_isolation_args()
        with tempfile.TemporaryDirectory(prefix="llm-codex-") as tmp:
            workdir = Path(tmp)
            output_path = workdir / "last_message.txt"
            args = [
                self._binary,
                "exec",
                "--yolo",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                # No MCP servers, apps or plugins: prompt content can't reach them.
                *isolation_args,
                "--cd",
                str(workdir),
                "--output-last-message",
                str(output_path),
            ]
            if self._model:
                args += ["--model", self._model]
            if schema is not None:
                schema_path = workdir / "schema.json"
                schema_path.write_text(json.dumps(schema))
                args += ["--output-schema", str(schema_path)]
            args.append("-")  # read the prompt from stdin

            proc = _run_cli("codex", args, prompt, workdir, self._timeout)
            text = output_path.read_text().strip() if output_path.exists() else proc.stdout.strip()
        if not text:
            raise LLMError("codex: empty response")
        return text

    @property
    def model_name(self) -> str:
        """Return a label identifying the CLI and model."""
        return f"codex-cli:{self._model}" if self._model else "codex-cli"


class ClaudeCLIProvider(LLMProvider):
    """LLM provider that shells out to ``claude -p --dangerously-skip-permissions``."""

    def __init__(self, binary: str, model: str = "", timeout: int = DEFAULT_CLI_TIMEOUT_SECONDS) -> None:
        """Initialize the provider.

        Args:
            binary: Path to the ``claude`` executable.
            model: Optional model override; empty uses the CLI's configured default.
            timeout: Seconds to wait for a single completion.
        """
        self._binary = binary
        self._model = model
        self._timeout = timeout
        logger.info("Initialized Claude CLI provider: binary=%s model=%s", binary, model or "<cli default>")

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Generate a completion via ``claude -p``."""
        result = self._exec(prompt, system_prompt, schema=None)
        text = str(result.get("result") or "").strip()
        if not text:
            raise LLMError("claude: empty response")
        return text

    @property
    def supports_structured_output(self) -> bool:
        """Claude Code enforces ``--json-schema`` and returns ``structured_output``."""
        return True

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        """Request a schema-constrained response via ``--json-schema`` and return it."""
        result = self._exec(prompt, system_prompt, schema=schema)
        structured = result.get("structured_output")
        if isinstance(structured, dict):
            return structured
        # Older CLI versions only put the JSON in ``result``.
        return _parse_json_object("claude", str(result.get("result") or ""))

    def _exec(self, prompt: str, system_prompt: str, schema: dict[str, Any] | None) -> dict[str, Any]:
        """Run one ``claude -p`` session and return its decoded JSON result envelope."""
        with tempfile.TemporaryDirectory(prefix="llm-claude-") as tmp:
            workdir = Path(tmp)
            args = [
                self._binary,
                "-p",
                "--dangerously-skip-permissions",
                "--no-session-persistence",
                "--strict-mcp-config",
                # No user/project settings (rules, CLAUDE.md) and no skills: the
                # model sees only our system prompt and the transaction prompt.
                "--setting-sources",
                "",
                "--disable-slash-commands",
                "--output-format",
                "json",
            ]
            if self._model:
                args += ["--model", self._model]
            if system_prompt:
                system_path = workdir / "system_prompt.txt"
                system_path.write_text(system_prompt)
                args += ["--system-prompt-file", str(system_path)]
            if schema is not None:
                args += ["--json-schema", json.dumps(schema)]

            proc = _run_cli("claude", args, prompt, workdir, self._timeout)

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"claude: output was not valid JSON: {e}") from e
        if not isinstance(envelope, dict):
            raise LLMError("claude: output was not a JSON object")
        if envelope.get("is_error"):
            # Auth, rate-limit and out-of-credit failures land here.
            raise LLMUnavailableError(f"claude: {_tail(str(envelope.get('result') or envelope.get('subtype')))}")
        return envelope

    @property
    def model_name(self) -> str:
        """Return a label identifying the CLI and model."""
        return f"claude-cli:{self._model}" if self._model else "claude-cli"
