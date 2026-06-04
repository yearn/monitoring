"""OpenAI Codex Python SDK LLM provider.

Uses the native ``openai-codex`` SDK rather than the OpenAI HTTP API. Codex is
an agent runtime, so this adapter constrains it to direct text completion:
read-only sandbox, denied approvals, and one ephemeral thread per request.
"""

import json
from typing import Any

from utils.llm.base import LLMError, LLMProvider
from utils.logging import get_logger

logger = get_logger("utils.llm.codex_provider")

_COMPLETION_INSTRUCTIONS = """Act as a direct LLM completion backend for this application.
Use only the prompt content supplied in the turn. Do not inspect the workspace,
run commands, edit files, or describe tool limitations."""


class CodexProvider(LLMProvider):
    """LLM provider backed by the OpenAI Codex Python SDK."""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        structured_output: bool = True,
        model_provider: str | None = None,
        cwd: str | None = None,
    ) -> None:
        """Initialize the provider.

        Args:
            api_key: Optional OpenAI API key. If omitted, Codex reuses existing
                Codex authentication (for example from ``codex login``).
            model: Codex model identifier.
            structured_output: Whether to advertise Codex ``output_schema``.
            model_provider: Optional Codex model provider override.
            cwd: Optional runtime working directory for the Codex app-server.
        """
        try:
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
        except ImportError:
            raise LLMError(
                "openai-codex package not installed. Install with: uv pip install 'monitoring-scripts-py[ai]'"
            )

        self._model = model
        self._model_provider = model_provider
        self._structured_output = structured_output
        self._approval_mode = ApprovalMode.deny_all
        self._sandbox = Sandbox.read_only
        config = CodexConfig(cwd=cwd) if cwd else None
        client = Codex(config)
        try:
            if api_key:
                client.login_api_key(api_key)
        except Exception:
            client.close()
            raise
        self._client = client
        logger.info(
            "Initialized Codex provider: model=%s model_provider=%s structured=%s cwd=%s",
            model,
            model_provider,
            structured_output,
            cwd,
        )

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Generate a completion using a fresh Codex thread."""
        try:
            return self._run(prompt, system_prompt).strip()
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"Codex SDK call failed: {e}") from e

    @property
    def supports_structured_output(self) -> bool:
        """Return whether Codex ``output_schema`` is enabled."""
        return self._structured_output

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        """Request a schema-constrained Codex response and return it parsed."""
        try:
            content = self._run(prompt, system_prompt, output_schema=schema)
            parsed: dict[str, Any] = json.loads(content)
            return parsed
        except LLMError:
            raise
        except json.JSONDecodeError as e:
            raise LLMError(f"Structured Codex response was not valid JSON: {e}") from e
        except Exception as e:
            raise LLMError(f"Codex structured call failed: {e}") from e

    def close(self) -> None:
        """Close the Codex runtime process."""
        self._client.close()

    def _run(self, prompt: str, system_prompt: str, output_schema: dict[str, Any] | None = None) -> str:
        """Run one stateless Codex turn and return the final response text."""
        thread = self._client.thread_start(
            approval_mode=self._approval_mode,
            developer_instructions=self._build_instructions(system_prompt),
            ephemeral=True,
            model=self._model,
            model_provider=self._model_provider,
            sandbox=self._sandbox,
        )
        result = thread.run(
            prompt,
            approval_mode=self._approval_mode,
            model=self._model,
            output_schema=output_schema,
            sandbox=self._sandbox,
        )
        if not result.final_response:
            raise LLMError("Empty response from Codex")
        return result.final_response.strip()

    def _build_instructions(self, system_prompt: str) -> str:
        """Combine adapter-level constraints with the caller's system prompt."""
        if not system_prompt:
            return _COMPLETION_INSTRUCTIONS
        return f"{_COMPLETION_INSTRUCTIONS}\n\n{system_prompt}"

    @property
    def model_name(self) -> str:
        """Return the model identifier."""
        return self._model
