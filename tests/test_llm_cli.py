"""Tests for the CLI-backed LLM providers, the fallback chain, and CLI-first factory wiring."""

import json
import os
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from utils.llm.base import LLMError, LLMProvider, LLMUnavailableError
from utils.llm.cli_provider import ClaudeCLIProvider, CodexCLIProvider
from utils.llm.factory import get_llm_provider, reset_provider
from utils.llm.fallback import FallbackProvider

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"summary": {"type": "string"}}}


def _completed(args: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.args, proc.stdout, proc.stderr, proc.returncode = args, stdout, stderr, returncode
    return proc


def _arg_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


class TestCodexCLIProvider(unittest.TestCase):
    """Tests for CodexCLIProvider."""

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_complete_reads_last_message_and_runs_yolo(self, mock_run: MagicMock) -> None:
        def fake_run(args: list[str], **kwargs: Any) -> MagicMock:
            Path(_arg_after(args, "--output-last-message")).write_text("  Pauses the vault. LOW.\n")
            return _completed(args)

        mock_run.side_effect = fake_run
        provider = CodexCLIProvider(binary="/bin/codex", model="gpt-x")

        result = provider.complete("Explain this tx", system_prompt="Be terse.")

        self.assertEqual(result, "Pauses the vault. LOW.")
        args = mock_run.call_args.args[0]
        self.assertEqual(args[:3], ["/bin/codex", "exec", "--yolo"])
        self.assertEqual(_arg_after(args, "--model"), "gpt-x")
        self.assertEqual(args[-1], "-")
        self.assertNotIn("--output-schema", args)
        stdin = mock_run.call_args.kwargs["input"]
        self.assertIn("<system_instructions>\nBe terse.\n</system_instructions>", stdin)
        self.assertTrue(stdin.endswith("Explain this tx"))
        # Runs outside the repo so the agent doesn't pick up project instructions.
        self.assertEqual(str(mock_run.call_args.kwargs["cwd"]), _arg_after(args, "--cd"))

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_complete_structured_passes_schema_file(self, mock_run: MagicMock) -> None:
        seen_schema: dict[str, Any] = {}

        def fake_run(args: list[str], **kwargs: Any) -> MagicMock:
            seen_schema.update(json.loads(Path(_arg_after(args, "--output-schema")).read_text()))
            Path(_arg_after(args, "--output-last-message")).write_text('{"summary": "ok"}')
            return _completed(args)

        mock_run.side_effect = fake_run
        provider = CodexCLIProvider(binary="codex")

        self.assertTrue(provider.supports_structured_output)
        self.assertEqual(provider.complete_structured("p", SCHEMA), {"summary": "ok"})
        self.assertEqual(seen_schema, SCHEMA)
        self.assertNotIn("--model", mock_run.call_args.args[0])

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_nonzero_exit_is_unavailable(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = lambda args, **kw: _completed(args, stderr="ERROR: 401 Unauthorized", returncode=1)
        with self.assertRaises(LLMUnavailableError) as ctx:
            CodexCLIProvider(binary="codex").complete("p")
        self.assertIn("401 Unauthorized", str(ctx.exception))

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_timeout_is_unavailable(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=5)
        with self.assertRaises(LLMUnavailableError):
            CodexCLIProvider(binary="codex", timeout=5).complete("p")

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_missing_binary_is_unavailable(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("codex")
        with self.assertRaises(LLMUnavailableError):
            CodexCLIProvider(binary="codex").complete("p")

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_invalid_structured_json_is_plain_llm_error(self, mock_run: MagicMock) -> None:
        def fake_run(args: list[str], **kwargs: Any) -> MagicMock:
            Path(_arg_after(args, "--output-last-message")).write_text("not json")
            return _completed(args)

        mock_run.side_effect = fake_run
        with self.assertRaises(LLMError) as ctx:
            CodexCLIProvider(binary="codex").complete_structured("p", SCHEMA)
        self.assertNotIsInstance(ctx.exception, LLMUnavailableError)


class TestClaudeCLIProvider(unittest.TestCase):
    """Tests for ClaudeCLIProvider."""

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_complete_skips_permissions_and_uses_system_prompt_file(self, mock_run: MagicMock) -> None:
        seen_system: list[str] = []

        def fake_run(args: list[str], **kwargs: Any) -> MagicMock:
            seen_system.append(Path(_arg_after(args, "--system-prompt-file")).read_text())
            return _completed(args, stdout=json.dumps({"is_error": False, "result": " Pauses. LOW. "}))

        mock_run.side_effect = fake_run
        provider = ClaudeCLIProvider(binary="/bin/claude", model="opus")

        self.assertEqual(provider.complete("Explain", system_prompt="Be terse."), "Pauses. LOW.")
        args = mock_run.call_args.args[0]
        self.assertEqual(args[:3], ["/bin/claude", "-p", "--dangerously-skip-permissions"])
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(_arg_after(args, "--model"), "opus")
        self.assertEqual(_arg_after(args, "--output-format"), "json")
        self.assertEqual(seen_system, ["Be terse."])
        self.assertEqual(mock_run.call_args.kwargs["input"], "Explain")

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_complete_structured_reads_structured_output(self, mock_run: MagicMock) -> None:
        envelope = {"is_error": False, "result": "", "structured_output": {"summary": "ok"}}
        mock_run.side_effect = lambda args, **kw: _completed(args, stdout=json.dumps(envelope))

        result = ClaudeCLIProvider(binary="claude").complete_structured("p", SCHEMA)

        self.assertEqual(result, {"summary": "ok"})
        self.assertEqual(json.loads(_arg_after(mock_run.call_args.args[0], "--json-schema")), SCHEMA)

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_is_error_envelope_is_unavailable(self, mock_run: MagicMock) -> None:
        envelope = {"is_error": True, "result": "Credit balance is too low"}
        mock_run.side_effect = lambda args, **kw: _completed(args, stdout=json.dumps(envelope))
        with self.assertRaises(LLMUnavailableError) as ctx:
            ClaudeCLIProvider(binary="claude").complete("p")
        self.assertIn("Credit balance is too low", str(ctx.exception))

    @patch("utils.llm.cli_provider.subprocess.run")
    def test_nonzero_exit_is_unavailable(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = lambda args, **kw: _completed(args, stdout='{"is_error": true}', returncode=1)
        with self.assertRaises(LLMUnavailableError):
            ClaudeCLIProvider(binary="claude").complete("p")


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

    def test_cli_only_without_api_key(self) -> None:
        with (
            patch.dict(os.environ, {"LLM_CLAUDE_MODEL": "opus"}, clear=True),
            patch("utils.llm.factory.shutil.which", side_effect=self._which({"claude"})),
        ):
            provider = get_llm_provider()
        self.assertIsInstance(provider, ClaudeCLIProvider)
        self.assertEqual(provider.model_name, "claude-cli:opus")

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
        self.assertEqual(provider.model_name, "gpt-4o-mini")

    def test_nothing_available_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("utils.llm.factory.shutil.which", return_value=None):
            with self.assertRaises(LLMError) as ctx:
                get_llm_provider()
        self.assertIn("LLM_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
