"""Tests for the CLI-backed LLM providers, the fallback chain, and CLI-first factory wiring."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from utils.llm.base import LLMError, LLMProvider, LLMUnavailableError
from utils.llm.cli_provider import ClaudeCLIProvider, CodexCLIProvider, _run_cli
from utils.llm.factory import get_llm_provider, reset_provider
from utils.llm.fallback import FallbackProvider

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"summary": {"type": "string"}}}


def _completed(args: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.args, proc.stdout, proc.stderr, proc.returncode = args, stdout, stderr, returncode
    return proc


def _arg_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is running (not exited, not a zombie awaiting reaping)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


class TestRunCLI(unittest.TestCase):
    """Process handling in ``_run_cli``, exercised with real shell scripts."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.pid_file = self.dir / "child.pid"

    def _script(self, body: str) -> str:
        path = self.dir / "fake-cli"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def _assert_child_killed(self) -> None:
        pid = int(self.pid_file.read_text())
        deadline = time.monotonic() + 5
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(pid), "agent-spawned child outlived the CLI call")

    def test_success_returns_output_and_reads_stdin(self) -> None:
        binary = self._script('cat; echo "warn" >&2')
        proc = _run_cli("fake", [binary], "the prompt", self.dir, timeout=10)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "the prompt", "warn\n"))

    def test_nonzero_exit_is_unavailable(self) -> None:
        binary = self._script('echo "ERROR: 401 Unauthorized" >&2; exit 1')
        with self.assertRaises(LLMUnavailableError) as ctx:
            _run_cli("fake", [binary], "", self.dir, timeout=10)
        self.assertIn("exited with code 1: ERROR: 401 Unauthorized", str(ctx.exception))

    def test_missing_binary_is_unavailable(self) -> None:
        with self.assertRaises(LLMUnavailableError):
            _run_cli("fake", [str(self.dir / "missing")], "", self.dir, timeout=10)

    def test_timeout_kills_whole_process_group(self) -> None:
        binary = self._script(f"sleep 60 & echo $! > {self.pid_file}; sleep 60")
        start = time.monotonic()
        with self.assertRaises(LLMUnavailableError) as ctx:
            _run_cli("fake", [binary], "", self.dir, timeout=1)
        self.assertIn("timed out after 1s", str(ctx.exception))
        self.assertLess(time.monotonic() - start, 10)
        self._assert_child_killed()

    def test_background_child_holding_stdout_does_not_block_or_survive(self) -> None:
        # The CLI exits right away but leaves a child that inherited stdout.
        binary = self._script(f"sleep 60 & echo $! > {self.pid_file}; echo done")
        start = time.monotonic()
        proc = _run_cli("fake", [binary], "", self.dir, timeout=30)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(proc.stdout, "done\n")
        self._assert_child_killed()


def _codex_fake(
    last_message: str, servers: list[dict[str, Any]] | None = None, seen: list[list[str]] | None = None
) -> Any:
    """Fake ``_run_cli`` for codex: answers ``mcp list`` and writes ``last_message`` for ``exec``."""

    def fake_run(label: str, args: list[str], prompt: str, cwd: Path, timeout: int) -> MagicMock:
        if seen is not None:
            seen.append(args)
        if args[1:3] == ["mcp", "list"]:
            return _completed(args, stdout=json.dumps(servers or []))
        if "--output-schema" in args and seen is not None:
            seen.append(["schema", Path(_arg_after(args, "--output-schema")).read_text()])
        Path(_arg_after(args, "--output-last-message")).write_text(last_message)
        return _completed(args)

    return fake_run


class TestCodexCLIProvider(unittest.TestCase):
    """Tests for CodexCLIProvider."""

    @patch("utils.llm.cli_provider._run_cli")
    def test_complete_reads_last_message_and_runs_yolo(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = _codex_fake("  Pauses the vault. LOW.\n")
        provider = CodexCLIProvider(binary="/bin/codex", model="gpt-x", timeout=7)

        result = provider.complete("Explain this tx", system_prompt="Be terse.")

        self.assertEqual(result, "Pauses the vault. LOW.")
        _, args, stdin, cwd, timeout = mock_run.call_args.args
        self.assertEqual(args[:3], ["/bin/codex", "exec", "--yolo"])
        self.assertEqual(_arg_after(args, "--model"), "gpt-x")
        self.assertEqual(args[-1], "-")
        self.assertNotIn("--output-schema", args)
        self.assertIn("<system_instructions>\nBe terse.\n</system_instructions>", stdin)
        self.assertTrue(stdin.endswith("Explain this tx"))
        self.assertEqual(timeout, 7)
        # Runs outside the repo so the agent doesn't pick up project instructions.
        self.assertEqual(str(cwd), _arg_after(args, "--cd"))

    @patch("utils.llm.cli_provider._run_cli")
    def test_every_configured_mcp_server_is_disabled_by_name(self, mock_run: MagicMock) -> None:
        servers = [{"name": "context7", "enabled": True}, {"name": "review_probe", "enabled": True}]
        seen: list[list[str]] = []
        mock_run.side_effect = _codex_fake("ok", servers, seen)
        provider = CodexCLIProvider(binary="codex")

        provider.complete("p")
        provider.complete("p")

        exec_calls = [a for a in seen if a[1] == "exec"]
        self.assertEqual([a for a in seen if a[1] == "mcp"], [["codex", "mcp", "list", "--json"]])  # listed once
        for args in exec_calls:
            configs = [args[i + 1] for i, a in enumerate(args) if a == "--config"]
            self.assertEqual(configs, ["mcp_servers.context7.enabled=false", "mcp_servers.review_probe.enabled=false"])
            disabled = [args[i + 1] for i, a in enumerate(args) if a == "--disable"]
            self.assertEqual(disabled, ["apps", "plugins"])
            # `mcp_servers={}` is a no-op merge in codex; it must not be relied on.
            self.assertNotIn("mcp_servers={}", args)

    @patch("utils.llm.cli_provider._run_cli")
    def test_unaddressable_mcp_server_fails_closed(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = _codex_fake("ok", [{"name": "weird.name", "enabled": True}])
        with self.assertRaises(LLMUnavailableError) as ctx:
            CodexCLIProvider(binary="codex").complete("p")
        self.assertIn("weird.name", str(ctx.exception))
        self.assertEqual(mock_run.call_count, 1)  # never reached `exec`

    @patch("utils.llm.cli_provider._run_cli")
    def test_unparseable_mcp_list_fails_closed(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = lambda label, args, *rest: _completed(args, stdout="WARNING: something\n[]")
        with self.assertRaises(LLMUnavailableError):
            CodexCLIProvider(binary="codex").complete("p")
        self.assertEqual(mock_run.call_count, 1)

    @patch("utils.llm.cli_provider._run_cli")
    def test_complete_structured_passes_schema_file(self, mock_run: MagicMock) -> None:
        seen: list[list[str]] = []
        mock_run.side_effect = _codex_fake('{"summary": "ok"}', seen=seen)
        provider = CodexCLIProvider(binary="codex")

        self.assertTrue(provider.supports_structured_output)
        self.assertEqual(provider.complete_structured("p", SCHEMA), {"summary": "ok"})
        self.assertIn(["schema", json.dumps(SCHEMA)], seen)
        self.assertNotIn("--model", mock_run.call_args.args[1])

    @patch("utils.llm.cli_provider._run_cli")
    def test_unavailable_propagates(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = LLMUnavailableError("codex: timed out after 5s")
        with self.assertRaises(LLMUnavailableError):
            CodexCLIProvider(binary="codex", timeout=5).complete("p")

    @patch("utils.llm.cli_provider._run_cli")
    def test_invalid_structured_json_is_plain_llm_error(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = _codex_fake("not json")
        with self.assertRaises(LLMError) as ctx:
            CodexCLIProvider(binary="codex").complete_structured("p", SCHEMA)
        self.assertNotIsInstance(ctx.exception, LLMUnavailableError)


@unittest.skipUnless(shutil.which("codex"), "codex CLI not installed")
class TestCodexMCPIsolationLive(unittest.TestCase):
    """Runs the real codex CLI against a synthetic config (no auth or network needed)."""

    def test_isolation_args_disable_every_server_in_real_codex(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            Path(home, "config.toml").write_text(
                '[mcp_servers.review_probe]\ncommand = "true"\n\n'
                '[mcp_servers.other-probe]\nurl = "https://example.invalid/mcp"\n'
            )
            binary = shutil.which("codex") or "codex"
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                isolation = CodexCLIProvider(binary=binary)._mcp_isolation_args()
                listed = _run_cli("codex", [binary, *isolation, "mcp", "list", "--json"], "", Path(home), 30)
        servers = {s["name"]: s["enabled"] for s in json.loads(listed.stdout)}
        self.assertEqual(servers, {"review_probe": False, "other-probe": False})


class TestClaudeCLIProvider(unittest.TestCase):
    """Tests for ClaudeCLIProvider."""

    @patch("utils.llm.cli_provider._run_cli")
    def test_complete_skips_permissions_and_uses_system_prompt_file(self, mock_run: MagicMock) -> None:
        seen_system: list[str] = []

        def fake_run(label: str, args: list[str], prompt: str, cwd: Path, timeout: int) -> MagicMock:
            seen_system.append(Path(_arg_after(args, "--system-prompt-file")).read_text())
            return _completed(args, stdout=json.dumps({"is_error": False, "result": " Pauses. LOW. "}))

        mock_run.side_effect = fake_run
        provider = ClaudeCLIProvider(binary="/bin/claude", model="opus")

        self.assertEqual(provider.complete("Explain", system_prompt="Be terse."), "Pauses. LOW.")
        _, args, stdin, _cwd, _timeout = mock_run.call_args.args
        self.assertEqual(args[:3], ["/bin/claude", "-p", "--dangerously-skip-permissions"])
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(_arg_after(args, "--setting-sources"), "")
        self.assertIn("--disable-slash-commands", args)
        self.assertEqual(_arg_after(args, "--model"), "opus")
        self.assertEqual(_arg_after(args, "--output-format"), "json")
        self.assertEqual(seen_system, ["Be terse."])
        self.assertEqual(stdin, "Explain")

    @patch("utils.llm.cli_provider._run_cli")
    def test_complete_structured_reads_structured_output(self, mock_run: MagicMock) -> None:
        envelope = {"is_error": False, "result": "", "structured_output": {"summary": "ok"}}
        mock_run.side_effect = lambda label, args, *rest: _completed(args, stdout=json.dumps(envelope))

        result = ClaudeCLIProvider(binary="claude").complete_structured("p", SCHEMA)

        self.assertEqual(result, {"summary": "ok"})
        self.assertEqual(json.loads(_arg_after(mock_run.call_args.args[1], "--json-schema")), SCHEMA)

    @patch("utils.llm.cli_provider._run_cli")
    def test_is_error_envelope_is_unavailable(self, mock_run: MagicMock) -> None:
        envelope = {"is_error": True, "result": "Credit balance is too low"}
        mock_run.side_effect = lambda label, args, *rest: _completed(args, stdout=json.dumps(envelope))
        with self.assertRaises(LLMUnavailableError) as ctx:
            ClaudeCLIProvider(binary="claude").complete("p")
        self.assertIn("Credit balance is too low", str(ctx.exception))


class _StubProvider(LLMProvider):
    """In-memory provider whose responses/errors are scripted per call."""

    def __init__(self, name: str, outcomes: list[Any], structured: bool = True) -> None:
        self._name = name
        self._outcomes = outcomes
        self._structured = structured
        self.calls = 0

    def _next(self) -> Any:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        return self._next()

    @property
    def supports_structured_output(self) -> bool:
        return self._structured

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        return self._next()

    @property
    def model_name(self) -> str:
        return self._name


class TestFallbackProvider(unittest.TestCase):
    """Tests for FallbackProvider."""

    def test_first_success_wins(self) -> None:
        first, second = _StubProvider("a", ["from a"]), _StubProvider("b", ["from b"])
        chain = FallbackProvider([first, second])
        self.assertEqual(chain.complete("p"), "from a")
        self.assertEqual(second.calls, 0)
        self.assertEqual(chain.model_name, "a")

    def test_unavailable_provider_is_disabled_for_later_calls(self) -> None:
        codex = _StubProvider("codex", [LLMUnavailableError("no credits")])
        claude = _StubProvider("claude", ["one", "two"])
        chain = FallbackProvider([codex, claude])

        self.assertEqual(chain.complete("p"), "one")
        self.assertEqual(chain.complete("p"), "two")
        self.assertEqual(codex.calls, 1)
        self.assertEqual(chain.model_name, "claude")

    def test_bad_response_falls_through_without_disabling(self) -> None:
        codex = _StubProvider("codex", [LLMError("empty"), "codex ok"])
        api = _StubProvider("api", ["api ok"])
        chain = FallbackProvider([codex, api])

        self.assertEqual(chain.complete("p"), "api ok")
        self.assertEqual(chain.complete("p"), "codex ok")

    def test_all_fail_raises_with_every_error(self) -> None:
        chain = FallbackProvider(
            [_StubProvider("codex", [LLMUnavailableError("401")]), _StubProvider("api", [LLMError("boom")])]
        )
        with self.assertRaises(LLMError) as ctx:
            chain.complete("p")
        self.assertIn("codex: 401", str(ctx.exception))
        self.assertIn("api: boom", str(ctx.exception))

    def test_structured_skips_providers_without_support(self) -> None:
        plain = _StubProvider("groq", [], structured=False)
        structured = _StubProvider("claude", [{"summary": "ok"}])
        chain = FallbackProvider([plain, structured])

        self.assertTrue(chain.supports_structured_output)
        self.assertEqual(chain.complete_structured("p", SCHEMA), {"summary": "ok"})
        self.assertEqual(plain.calls, 0)

    def test_structured_support_drops_with_disabled_providers(self) -> None:
        chain = FallbackProvider(
            [_StubProvider("codex", [LLMUnavailableError("down")]), _StubProvider("groq", ["text"], structured=False)]
        )
        self.assertTrue(chain.supports_structured_output)
        with self.assertRaises(LLMError):
            chain.complete_structured("p", SCHEMA)
        self.assertFalse(chain.supports_structured_output)

    def test_fully_disabled_chain_fails_fast_with_reason(self) -> None:
        codex = _StubProvider("codex", [LLMUnavailableError("no credits")])
        chain = FallbackProvider([codex])
        with self.assertRaises(LLMError):
            chain.complete("p")
        with self.assertRaises(LLMError) as ctx:
            chain.complete("p")
        self.assertIn("disabled after earlier failures", str(ctx.exception))
        self.assertEqual(codex.calls, 1)

    def test_empty_chain_rejected(self) -> None:
        with self.assertRaises(LLMError):
            FallbackProvider([])


class TestFactoryCLIChain(unittest.TestCase):
    """Tests for CLI-first provider selection in the factory."""

    def setUp(self) -> None:
        reset_provider()
        self.addCleanup(reset_provider)

    @staticmethod
    def _which(available: set[str]) -> Any:
        return lambda name: f"/usr/bin/{name}" if name in available else None

    @patch("openai.OpenAI")
    def test_default_order_is_codex_claude_api(self, mock_openai_cls: MagicMock) -> None:
        env = {"LLM_PROVIDER": "venice", "LLM_API_KEY": "key"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("utils.llm.factory.shutil.which", side_effect=self._which({"codex", "claude"})),
        ):
            provider = get_llm_provider()
        self.assertIsInstance(provider, FallbackProvider)
        self.assertEqual(provider.model_name, "codex-cli -> claude-cli -> deepseek-v4-flash")

    def test_lone_cli_is_still_wrapped(self) -> None:
        with (
            patch.dict(os.environ, {"LLM_CLAUDE_MODEL": "opus"}, clear=True),
            patch("utils.llm.factory.shutil.which", side_effect=self._which({"claude"})),
        ):
            provider = get_llm_provider()
        self.assertIsInstance(provider, FallbackProvider)
        self.assertEqual(provider.model_name, "claude-cli:opus")

    @patch("utils.llm.cli_provider._run_cli")
    def test_lone_unavailable_cli_is_called_once_across_summaries(self, mock_run: MagicMock) -> None:
        from utils.llm.ai_explainer import _generate_summary

        mock_run.side_effect = LLMUnavailableError("codex: timed out after 300s")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("utils.llm.factory.shutil.which", side_effect=self._which({"codex"})),
        ):
            provider = get_llm_provider()
        for _ in range(2):
            with self.assertRaises(LLMError):
                _generate_summary(provider, "prompt")
        # Structured attempt of the first summary only; the text path and the
        # second summary skip the disabled CLI instead of waiting on it again.
        self.assertEqual(mock_run.call_count, 1)

    def test_broken_api_config_does_not_block_clis(self) -> None:
        env = {"LLM_PROVIDER": "venice", "LLM_API_KEY": "key"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("utils.llm.factory.shutil.which", side_effect=self._which({"codex"})),
            patch("utils.llm.factory._create_provider", side_effect=LLMError("openai package not installed")),
        ):
            provider = get_llm_provider()
        self.assertIsInstance(provider, FallbackProvider)
        self.assertEqual(provider.model_name, "codex-cli")

    def test_broken_api_config_raises_without_clis(self) -> None:
        env = {"LLM_PROVIDER": "venice", "LLM_API_KEY": "key"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("utils.llm.factory.shutil.which", return_value=None),
            patch("utils.llm.factory._create_provider", side_effect=LLMError("openai package not installed")),
        ):
            with self.assertRaises(LLMError) as ctx:
                get_llm_provider()
        self.assertIn("openai package not installed", str(ctx.exception))

    def test_custom_order_and_timeout(self) -> None:
        env = {"LLM_CLI_PROVIDERS": "claude, codex", "LLM_CLI_TIMEOUT": "42"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("utils.llm.factory.shutil.which", side_effect=self._which({"codex", "claude"})),
        ):
            provider = get_llm_provider()
        assert isinstance(provider, FallbackProvider)
        self.assertEqual(provider.model_name, "claude-cli -> codex-cli")
        clis = [p for p in provider._providers if isinstance(p, CodexCLIProvider | ClaudeCLIProvider)]
        self.assertEqual([p._timeout for p in clis], [42, 42])

    @patch("openai.OpenAI")
    def test_cli_disabled_uses_api_only(self, mock_openai_cls: MagicMock) -> None:
        env = {"LLM_CLI_PROVIDERS": "none", "LLM_PROVIDER": "openai", "LLM_API_KEY": "key"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("utils.llm.factory.shutil.which", side_effect=self._which({"codex", "claude"})),
        ):
            provider = get_llm_provider()
        self.assertNotIsInstance(provider, FallbackProvider)
        self.assertEqual(provider.model_name, "gpt-4o-mini")

    def test_nothing_available_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("utils.llm.factory.shutil.which", return_value=None):
            with self.assertRaises(LLMError) as ctx:
                get_llm_provider()
        self.assertIn("LLM_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
