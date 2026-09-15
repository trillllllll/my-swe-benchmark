from pathlib import Path

from bench.adapters.base import RunSpec, TargetConfig
from bench.adapters.common import command_executable
from bench.adapters.registry import get_adapter


def make_spec(adapter: str) -> RunSpec:
    target = TargetConfig(
        name="test",
        adapter=adapter,
        executable="fake-agent",
        command_prefix=("python",),
        model="model-x",
        mode="json",
        permission_mode="yolo",
        sandbox="workspace-write",
        approval_policy="never",
        agent="build",
    )
    return RunSpec(target, "fix it", Path("C:/workspace"), 10, Path("C:/run"))


def test_all_adapters_build_commands():
    commands = {
        name: get_adapter(name).build_command(make_spec(name))
        for name in ("claude-code", "codex", "opencode", "gemini")
    }
    assert "--model" in commands["claude-code"]
    assert "exec" in commands["codex"]
    assert "--format" in commands["opencode"]
    assert "--prompt" in commands["gemini"]


def test_codex_approval_policy_is_before_exec_subcommand():
    command = get_adapter("codex").build_command(make_spec("codex"))
    assert command.index("--ask-for-approval") < command.index("exec")
    assert command[command.index("--ask-for-approval") + 1] == "never"


def test_json_and_text_events_are_normalized():
    adapter = get_adapter("codex")
    assert adapter.parse_event('{"type":"tool_call"}').kind == "tool_call"
    assert adapter.parse_event("plain output").kind == "text"


def test_cli_events_have_semantic_fields_and_unknown_fallback():
    adapter = get_adapter("codex")
    command = adapter.parse_event('{"type":"item.started","item":{"type":"command_execution","command":"pytest -q"}}')
    assert command is not None
    assert command.category == "command"
    assert command.title == "执行命令"
    assert command.command == "pytest -q"
    unknown = adapter.parse_event('{"type":"vendor.future_event","payload":{"x":1}}')
    assert unknown is not None
    assert unknown.category == "unknown"
    assert unknown.raw["type"] == "vendor.future_event"


def test_bare_real_cli_names_resolve_to_windows_wrappers(monkeypatch):
    monkeypatch.setattr(
        "bench.adapters.common.shutil.which",
        lambda value: "C:\\Program Files\\nodejs\\" + value + ".cmd",
    )
    assert command_executable("claude").endswith("claude.cmd")
    # Explicit paths (including the fake-agent fixture) must not be rewritten.
    assert command_executable("tests/fake_agent.py") == "tests/fake_agent.py"
