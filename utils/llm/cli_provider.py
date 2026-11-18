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
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from utils.llm.base import LLMError, LLMProvider, LLMUnavailableError
from utils.logger import get_logger
from utils.process_groups import kill_process_group, register_process_group, unregister_process_group

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
# The resolved model is only reported in Codex's OpenTelemetry trace events, so
# INFO is enabled for that one target. RUST_LOG cannot select a single event by
# field, so the whole target is enabled: about 26 short lines (~10 KB) per call
# with CLI 0.170.0, since stream events are logged once each, not per delta.
_CODEX_TRACE_TARGET = "codex_otel.trace_safe"
_CODEX_LOG_FILTER = f"error,opentelemetry_sdk=off,opentelemetry_otlp=off,{_CODEX_TRACE_TARGET}=info"
_CODEX_TRACE_LINE = re.compile(rf"\b(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+{re.escape(_CODEX_TRACE_TARGET)}\b")
_CODEX_SLUG = re.compile(r'\bslug=(?:"(?P<quoted>[^"\s]+)"|(?P<bare>[^\s"]+))')
_CODEX_REROUTE_NOTICE = re.compile(r"model\s+rerouted", re.IGNORECASE)
_CODEX_REROUTED_MODEL = re.compile(
    r"model\s+rerouted\s*:?\s*(?:from\s+)?[`'\"]?[^\s`'\"]+[`'\"]?\s*(?:->|→|=>|\bto\b)\s*[`'\"]?(?P<model>[^\s`'\"(),;]+)",
    re.IGNORECASE,
)
# Label used when Codex reports a reroute whose destination cannot be parsed, so
# the report does not credit the requested model with the answer.
CODEX_UNKNOWN_REROUTE = "rerouted"


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


def _codex_reroute(events: list[dict[str, Any]]) -> str:
    """Return the destination of the last reroute notice in a Codex event stream.

    Returns:
        The destination model, ``CODEX_UNKNOWN_REROUTE`` if a reroute was reported
        but its destination could not be parsed, or ``""`` if there was no reroute.
    """
    for event in reversed(events):
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, dict) or item.get("type") != "error":
            continue
        message = item.get("message")
        if not isinstance(message, str) or not _CODEX_REROUTE_NOTICE.search(message):
            continue
        reroute = _CODEX_REROUTED_MODEL.search(message)
        model = reroute["model"].rstrip(".:") if reroute else ""
        if model:
            return model
        logger.warning("codex: could not parse reroute destination from %r", _tail(message))
        return CODEX_UNKNOWN_REROUTE
    return ""


def _codex_trace_slug(stderr: str) -> str:
    """Read the model slug from Codex trace lines, preferring the conversation start event."""
    slugs = []
    for line in stderr.splitlines():
        if not _CODEX_TRACE_LINE.search(line):
            continue
        match = _CODEX_SLUG.search(line)
        if not match:
            continue
        slug = match["quoted"] or match["bare"]
        if "codex.conversation_starts" in line:
            return slug
        slugs.append(slug)
    return slugs[-1] if slugs else ""


def _codex_model(events: list[dict[str, Any]], stderr: str) -> str:
    """Return the model that answered: a reported reroute wins over the traced model slug."""
    return _codex_reroute(events) or _codex_trace_slug(stderr)


def _codex_error_stderr(stderr: str) -> str:
    """Drop Codex's INFO-and-below trace lines from stderr quoted in errors.

    WARN and ERROR trace lines are kept because they explain failures. Indented
    or blank lines directly after a dropped line are treated as its continuation.
    """
    kept = []
    dropping = False
    for line in stderr.splitlines():
        trace = _CODEX_TRACE_LINE.search(line)
        if trace:
            dropping = trace["level"] not in ("WARN", "ERROR")
        elif dropping and (not line.strip() or line[0].isspace()):
            continue
        else:
            dropping = False
        if not dropping:
            kept.append(line)
    return "\n".join(kept)


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


def _claude_model(events: list[dict[str, Any]]) -> str:
    """Read the initialized model, updating it from subsequent assistant responses."""
    model = ""
    for event in events:
        if (event.get("type"), event.get("subtype")) == ("system", "init"):
            candidate = event.get("model")
        elif event.get("type") == "assistant":
            message = event.get("message")
            candidate = message.get("model") if isinstance(message, dict) else None
        else:
            continue
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if candidate not in ("", "<synthetic>"):
            model = candidate
    return model


def _tail(text: str) -> str:
    """Return the last ``_ERROR_SNIPPET_CHARS`` of ``text``, stripped, for error messages."""
    return text.strip()[-_ERROR_SNIPPET_CHARS:]


def _run_cli(
    label: str,
    args: list[str],
    prompt: str,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    error_stderr: Callable[[str], str] | None = None,
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
        error_stderr: Optional filter applied to stderr before it is quoted in
            the non-zero exit error, e.g. to drop provider diagnostics.

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
        # The group is outside the script's own, so a terminating script must kill it explicitly.
        register_process_group(proc.pid)
        try:
            proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            kill_process_group(proc.pid)
            proc.wait()
            raise LLMUnavailableError(f"{label}: timed out after {timeout}s") from e
        finally:
            # Leftover agent-spawned processes; also covers the timeout path's raise.
            kill_process_group(proc.pid)
            unregister_process_group(proc.pid)
        out.seek(0)
        err.seek(0)
        result = subprocess.CompletedProcess(args, proc.returncode, out.read(), err.read())
    if result.returncode != 0:
        # Only stderr: stdout is the event stream, which carries prompt-derived text.
        stderr = error_stderr(result.stderr) if error_stderr else result.stderr
        detail = _tail(stderr) or "no stderr"
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
            model: Optional model override; empty uses the CLI's default.
            timeout: Seconds to wait for a single completion.
        """
        self._binary = binary
        self._model = model
        self._reported_model = ""
        self._warned_missing_model = False
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
        self._reported_model = ""
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

            env = {**_cli_environment("codex"), "RUST_LOG": _CODEX_LOG_FILTER}
            proc = _run_cli("codex", args, prompt, workdir, self._timeout, env, _codex_error_stderr)
            events = _jsonl_events("codex", proc.stdout)
            unexpected = _codex_tool_events(events)
            if unexpected:
                raise LLMUnavailableError(f"codex: unexpected tool activity: {', '.join(sorted(set(unexpected)))}")
            if not any(event.get("type") == "turn.completed" for event in events):
                raise LLMError("codex: turn did not complete")
            text = output_path.read_text().strip() if output_path.exists() else ""
        if not text:
            raise LLMError("codex: empty response")
        self._reported_model = _codex_model(events, proc.stderr)
        if not self._reported_model and not self._warned_missing_model:
            # Usually a CLI change to the trace format; the label falls back silently otherwise.
            logger.warning("codex: no model slug in CLI diagnostics; labelling reports as %s", self.model_name)
            self._warned_missing_model = True
        return text

    @property
    def model_name(self) -> str:
        """Return the reported model, falling back to the configured model or CLI label."""
        model = self._reported_model or self._model
        return f"codex-cli:{model}" if model else "codex-cli"


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
        self._reported_model = ""
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
        self._reported_model = ""
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
        self._reported_model = _claude_model(events)
        return envelope

    @property
    def model_name(self) -> str:
        """Return the reported model, falling back to the configured model or CLI label."""
        model = self._reported_model or self._model
        return f"claude-cli:{model}" if model else "claude-cli"
