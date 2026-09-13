from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any, Protocol


@dataclass(frozen=True)
class TargetConfig:
    name: str
    adapter: str
    executable: str = ""
    model: str | None = None
    mode: str | None = None
    permission_mode: str | None = None
    sandbox: str | None = None
    approval_policy: str | None = None
    agent: str | None = None
    command_prefix: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    pass_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunSpec:
    target: TargetConfig
    prompt: str
    workspace: Path
    timeout_seconds: float
    run_dir: Path
    network: str = "inherit"


@dataclass(frozen=True)
class NormalizedEvent:
    kind: str
    source: str = "unknown"
    raw: Any = None
    summary: str | None = None
    tool: str | None = None
    channel: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunResult:
    status: str
    return_code: int | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    final_response: str | None = None
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[NormalizedEvent], None]


class AgentAdapter(Protocol):
    name: str
    transport: str

    def build_command(self, spec: RunSpec) -> list[str]: ...

    def build_environment(self, spec: RunSpec) -> dict[str, str]: ...

    def parse_event(self, line: str) -> NormalizedEvent | None: ...

    def classify_exit(self, return_code: int | None, timed_out: bool) -> str: ...

    async def run(self, spec: RunSpec, emit_event: EventSink) -> AgentRunResult: ...

    def describe(self, spec: RunSpec) -> dict[str, Any]: ...
