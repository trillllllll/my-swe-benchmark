import asyncio
from pathlib import Path
from types import SimpleNamespace

from bench.adapters.base import RunSpec, TargetConfig
from bench.adapters.sdk_adapters import ClaudeAgentSdkAdapter, CodexSdkAdapter


def make_spec(adapter: str, *, timeout: float = 1.0) -> RunSpec:
    target = TargetConfig(
        name="sdk-test",
        adapter=adapter,
        model="model-test",
        permission_mode="acceptEdits",
        sandbox="workspace-write",
        approval_policy="never",
        pass_env=("BENCH_TEST_TOKEN",),
    )
    return RunSpec(target, "fix it", Path("C:/workspace"), timeout, Path("C:/run"))


class FakeCodexConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeSandbox:
    workspace_write = "workspace-write"


class FakeApprovalMode:
    deny_all = "deny_all"
    auto_review = "auto_review"


class FakeCodexTurn:
    id = "turn-1"

    def __init__(self):
        self.interrupted = False

    def stream(self):
        yield SimpleNamespace(method="item/started", payload=SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(type="commandExecution", command="python -V"))))
        yield SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta="done"))
        yield SimpleNamespace(
            method="turn/completed",
            payload=SimpleNamespace(turn=SimpleNamespace(status="completed", usage={"input_tokens": 2})),
        )

    def interrupt(self):
        self.interrupted = True


class FakeCodexThread:
    id = "thread-1"

    def __init__(self):
        self.turn_handle = FakeCodexTurn()
        self.kwargs = None

    def turn(self, prompt):
        self.prompt = prompt
        return self.turn_handle


class FakeCodex:
    last = None

    def __init__(self, config=None):
        self.config = config
        self.thread = FakeCodexThread()
        FakeCodex.last = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def thread_start(self, **kwargs):
        self.thread.kwargs = kwargs
        return self.thread


class FakeCodexSdk:
    CodexConfig = FakeCodexConfig
    Codex = FakeCodex
    Sandbox = FakeSandbox
    ApprovalMode = FakeApprovalMode


def test_codex_sdk_streams_events_and_passes_execution_policy():
    events = []
    result = asyncio.run(CodexSdkAdapter(FakeCodexSdk()).run(make_spec("codex-sdk"), events.append))

    assert result.status == "completed"
    assert result.session_id == "thread-1"
    assert result.usage == {"input_tokens": 2}
    assert result.final_response == "done"
    assert [event.kind for event in events] == ["tool_call", "assistant_delta", "turn_completed"]
    assert FakeCodex.last.thread.kwargs["cwd"].replace("\\", "/") == "C:/workspace"
    assert FakeCodex.last.thread.kwargs["model"] == "model-test"
    assert FakeCodex.last.thread.kwargs["sandbox"] == "workspace-write"
    assert FakeCodex.last.thread.kwargs["approval_mode"] == "deny_all"
    assert "BENCH_WORKSPACE" in FakeCodex.last.config.kwargs["env"]


class FakeClaudeOptions:
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeClaudeOptions.last = self


async def fake_claude_query(*, prompt, options):
    fake_claude_query.prompt = prompt
    fake_claude_query.options = options
    yield type("AssistantMessage", (), {"content": [SimpleNamespace(text="hello")], "model": "model-test"})()
    yield type(
        "ResultMessage",
        (),
        {
            "result": "hello",
            "is_error": False,
            "session_id": "claude-session-1",
            "usage": {"input_tokens": 3},
            "total_cost_usd": 0.01,
            "subtype": "success",
            "terminal_reason": "success",
        },
    )()


class FakeClaudeSdk:
    ClaudeAgentOptions = FakeClaudeOptions
    query = staticmethod(fake_claude_query)


def test_claude_agent_sdk_streams_result_metadata_and_options():
    events = []
    result = asyncio.run(ClaudeAgentSdkAdapter(FakeClaudeSdk()).run(make_spec("claude-agent-sdk"), events.append))

    assert result.status == "completed"
    assert result.session_id == "claude-session-1"
    assert result.usage == {"input_tokens": 3}
    assert result.cost_usd == 0.01
    assert result.final_response == "hello"
    assert [event.kind for event in events] == ["assistant", "turn_completed"]
    assert fake_claude_query.prompt == "fix it"
    assert FakeClaudeOptions.last.kwargs["cwd"].replace("\\", "/") == "C:/workspace"
    assert FakeClaudeOptions.last.kwargs["model"] == "model-test"
    assert FakeClaudeOptions.last.kwargs["permission_mode"] == "acceptEdits"
    assert "BENCH_WORKSPACE" in FakeClaudeOptions.last.kwargs["env"]


async def slow_claude_query(*, prompt, options):
    await asyncio.sleep(0.05)
    if False:
        yield None


def test_claude_agent_sdk_enforces_timeout_without_events():
    sdk = type("SlowClaudeSdk", (), {"ClaudeAgentOptions": FakeClaudeOptions, "query": staticmethod(slow_claude_query)})
    result = asyncio.run(ClaudeAgentSdkAdapter(sdk()).run(make_spec("claude-agent-sdk", timeout=0.01), lambda _event: None))

    assert result.status == "timeout"
    assert result.timed_out is True
