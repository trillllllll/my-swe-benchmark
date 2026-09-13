from __future__ import annotations

from .base import AgentAdapter
from .cli_adapters import ClaudeCodeAdapter, CodexAdapter, GeminiAdapter, OpenCodeAdapter
from .sdk_adapters import ClaudeAgentSdkAdapter, CodexSdkAdapter


_ADAPTERS: dict[str, type[AgentAdapter]] = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "opencode": OpenCodeAdapter,
    "gemini": GeminiAdapter,
    "codex-sdk": CodexSdkAdapter,
    "claude-agent-sdk": ClaudeAgentSdkAdapter,
    # Short aliases make local target files less noisy.
    "codex-agent-sdk": CodexSdkAdapter,
    "claude-sdk": ClaudeAgentSdkAdapter,
}


def get_adapter(name: str) -> AgentAdapter:
    try:
        return _ADAPTERS[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(_ADAPTERS))
        raise ValueError(f"Unknown adapter {name!r}; choose one of: {choices}") from exc
