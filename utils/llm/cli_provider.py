"""LLM providers backed by locally installed agent CLIs (Codex, Claude Code).

These run on the CLI's own login/subscription instead of a metered API key. The
explainer supplies all transaction context in the prompt, so both CLIs run as
text-only summarizers: no shell, web, file or MCP tools. Each call is
non-interactive and runs in a fresh temp directory.

Both receive the same prompt and system prompt as the API providers. The child
environment contains only CLI authentication and basic process variables, not
RPC, Etherscan, Telegram, Tenderly or application LLM API credentials. CLI event
streams are checked for tool use; any tool activity disables that provider for
the process and lets the fallback chain try the next one.

Any process-level failure (binary missing, non-zero exit, timeout, auth or
credit errors) raises ``LLMUnavailableError`` so the fallback chain moves on.
"""

import json
import os
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
_BASE_ENV = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR")
_AUTH_ENV = {
    "codex": ("CODEX_HOME", "OPENAI_API_KEY"),
    "claude": ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
}
_NO_TOOLS_INSTRUCTIONS = (
    "Use only the supplied transaction context. You have no tools, shell or web access; "
    "do not claim a lookup you did not make."
)
# Codex items that are model output or status, not tool use. ``todo_list`` is the
# plan tool's checklist and touches nothing outside the model.
_CODEX_BENIGN_ITEMS = ("agent_message", "reasoning", "todo_list", "error")


def _cli_environment(provider: str) -> dict[str, str]:
    """Pass only process basics and the CLI's own authentication."""
    names = (*_BASE_ENV, *_AUTH_ENV[provider])
    return {name: os.environ[name] for name in names if name in os.environ}


def _jsonl_events(label: str, raw: str) -> list[dict[str, Any]]:
    """Parse CLI event output, skipping non-JSON lines such as CLI warnings.

    Raises:
        LLMError: If the output holds no JSON object events. This fails only the
            current call; it does not disable the provider.
    """
    events = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    if not events:
        raise LLMError(f"{label}: no JSON events in CLI output")
    return events


def _codex_tool_events(events: list[dict[str, Any]]) -> list[str]:
    """Return the item types of all tool activity in a Codex event stream."""
    tools = []
    for event in events:
        if event.get("type") not in ("item.started", "item.completed", "item.updated"):
            continue
        item = event.get("item")
        kind = item.get("type") if isinstance(item, dict) else None
        if kind not in _CODEX_BENIGN_ITEMS:
            tools.append(str(kind or "unknown"))
    return tools


def _claude_tool_events(events: list[dict[str, Any]]) -> list[str]:
    """Find tool_use blocks in full messages and streaming block starts."""
    tools = []
    for event in events:
        stream_event = event.get("event") if event.get("type") == "stream_event" else None
        block = stream_event.get("content_block") if isinstance(stream_event, dict) else None
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tools.append(str(block.get("name") or "unknown"))
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            tools.extend(
                str(part.get("name") or "unknown")
                for part in content
                if isinstance(part, dict) and part.get("type") == "tool_use"
            )
    return tools


def _tail(text: str) -> str:
    """Return the last ``_ERROR_SNIPPET_CHARS`` of ``text``, stripped, for error messages."""
    return text.strip()[-_ERROR_SNIPPET_CHARS:]


def _kill_process_group(pgid: int) -> None:
    """SIGKILL every process in ``pgid``; a group that's already gone is fine."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_cli(
    label: str, args: list[str], prompt: str, cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
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
        env: Explicit child environment. ``None`` is for process handling tests.

    Returns:
        The completed process (exit code 0).

    Raises:
        LLMUnavailableError: If the binary is missing, times out, or exits non-zero.
    """
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=out, stderr=err, text=True, cwd=cwd, env=env, start_new_session=True
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
        # Only stderr: stdout is the event stream, which carries prompt-derived text.
        detail = _tail(result.stderr) or "no stderr"
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
    """LLM provider that shells out to tool-free ``codex exec``."""

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

    def _exec(self, prompt: str, system_prompt: str, schema: dict[str, Any] | None) -> str:
        """Run one ``codex exec`` session and return its last message.

        ``codex exec`` has no system-prompt flag, so static instructions are
        prepended to the prompt in a tagged block.
        """
        instructions = f"{_NO_TOOLS_INSTRUCTIONS}\n\n{system_prompt}" if system_prompt else _NO_TOOLS_INSTRUCTIONS
        prompt = f"<system_instructions>\n{instructions}\n</system_instructions>\n\n{prompt}"
        with tempfile.TemporaryDirectory(prefix="llm-codex-") as tmp:
            workdir = Path(tmp)
            output_path = workdir / "last_message.txt"
            args = [
                self._binary,
                "exec",
                "--sandbox",
                "read-only",
                "--strict-config",
                "--ignore-user-config",
                "--ignore-rules",
                "--json",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                # No shell, code execution, browser, image or agent handoff tools.
                "--disable",
                "shell_tool",
                "--disable",
                "multi_agent",
                "--disable",
                "hooks",
                "--disable",
                "browser_use",
                "--disable",
                "computer_use",
                "--disable",
                "image_generation",
                "--disable",
                "view_image",
                "--config",
                'approval_policy="never"',
                "--config",
                'web_search="disabled"',
                # --ignore-user-config also drops this setting, and the default
                # (file) misses a login kept in the OS keyring.
                "--config",
                'cli_auth_credentials_store="auto"',
                "--disable",
                "apps",
                "--disable",
                "plugins",
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

            proc = _run_cli("codex", args, prompt, workdir, self._timeout, _cli_environment("codex"))
            events = _jsonl_events("codex", proc.stdout)
            unexpected = _codex_tool_events(events)
            if unexpected:
                raise LLMUnavailableError(f"codex: unexpected tool activity: {', '.join(sorted(set(unexpected)))}")
            if not any(event.get("type") == "turn.completed" for event in events):
                raise LLMError("codex: turn did not complete")
            text = output_path.read_text().strip() if output_path.exists() else ""
        if not text:
            raise LLMError("codex: empty response")
        return text

    @property
    def model_name(self) -> str:
        """Return a label identifying the CLI and model."""
        return f"codex-cli:{self._model}" if self._model else "codex-cli"


class ClaudeCLIProvider(LLMProvider):
    """LLM provider that shells out to tool-free ``claude -p``."""

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
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--no-session-persistence",
                # Without --mcp-config this loads no MCP servers at all.
                "--strict-mcp-config",
                # No user/project settings (rules, CLAUDE.md) and no skills: the
                # model sees only our system prompt and the transaction prompt.
                "--setting-sources",
                "",
                "--disable-slash-commands",
                "--verbose",
                "--output-format",
                "stream-json",
            ]
            if self._model:
                args += ["--model", self._model]
            system_path = workdir / "system_prompt.txt"
            system_path.write_text(
                f"{_NO_TOOLS_INSTRUCTIONS}\n\n{system_prompt}" if system_prompt else _NO_TOOLS_INSTRUCTIONS
            )
            args += ["--system-prompt-file", str(system_path)]
            if schema is not None:
                args += ["--json-schema", json.dumps(schema)]

            proc = _run_cli("claude", args, prompt, workdir, self._timeout, _cli_environment("claude"))

        events = _jsonl_events("claude", proc.stdout)
        # Claude implements --json-schema through an internal StructuredOutput
        # formatting tool. It cannot access the filesystem or network.
        unexpected = [
            name for name in _claude_tool_events(events) if not (schema is not None and name == "StructuredOutput")
        ]
        if unexpected:
            raise LLMUnavailableError(f"claude: unexpected tool activity: {', '.join(sorted(set(unexpected)))}")
        results = [event for event in events if event.get("type") == "result"]
        if len(results) != 1:
            raise LLMError("claude: missing or duplicate result event")
        envelope = results[0]
        if envelope.get("is_error"):
            # Auth, rate-limit and out-of-credit failures land here.
            raise LLMUnavailableError(f"claude: {_tail(str(envelope.get('result') or envelope.get('subtype')))}")
        return envelope

    @property
    def model_name(self) -> str:
        """Return a label identifying the CLI and model."""
        return f"claude-cli:{self._model}" if self._model else "claude-cli"
