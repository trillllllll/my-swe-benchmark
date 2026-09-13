from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .base import NormalizedEvent, RunSpec


def command_executable(executable: str) -> str:
    """Resolve a command name to a runnable file when one is available.

    Windows installs for Node-based CLIs commonly expose a Unix shim plus a
    ``.cmd`` wrapper.  ``where.exe`` and an interactive shell resolve the
    wrapper automatically, but ``asyncio.create_subprocess_exec`` does not
    perform PATHEXT lookup for a bare name.  Resolving here keeps configured
    targets portable while preserving an unresolved token so the eventual
    process error still names the user's configuration.
    """

    if not executable or Path(executable).is_absolute() or any(sep in executable for sep in ("/", "\\")):
        return executable
    return shutil.which(executable) or executable


def command_base(spec: RunSpec) -> list[str]:
    return [*spec.target.command_prefix, command_executable(spec.target.executable)]


def base_environment(spec: RunSpec) -> dict[str, str]:
    # Keep ordinary process settings needed to find runtimes, but do not leak
    # credentials into an agent unless the target explicitly allowlists them.
    secret_name = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE)
    env = {
        key: value
        for key, value in os.environ.items()
        if not secret_name.search(key) or key in spec.target.pass_env
    }
    for key in spec.target.pass_env:
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(spec.target.env)
    env["BENCH_RUN_DIR"] = str(spec.run_dir)
    env["BENCH_WORKSPACE"] = str(spec.workspace)
    env["BENCH_TASK_PROMPT"] = spec.prompt
    return env


def parse_json_event(line: str, source: str) -> NormalizedEvent | None:
    text = line.strip()
    if not text:
        return None
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        return NormalizedEvent(
            kind="text",
            source=source,
            raw=line,
            summary=line.rstrip("\r\n"),
            data={"text": line.rstrip("\r\n")},
        )
    if isinstance(value, dict):
        event_type = str(value.get("type") or value.get("event") or value.get("kind") or "json")
        tool = _event_tool(value)
        return NormalizedEvent(
            kind=event_type,
            source=source,
            raw=value,
            summary=_event_summary(value),
            tool=tool,
            data={"payload": value},
        )
    return NormalizedEvent(kind="json", source=source, raw=value, data={"payload": value})


def classify_default(return_code: int | None, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if return_code is None:
        return "process_error"
    return "completed" if return_code == 0 else "agent_error"


def to_jsonable(value: Any) -> Any:
    """Convert SDK dataclasses and Pydantic models without importing either SDK."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json", by_alias=True))
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return to_jsonable(vars(value))
    return repr(value)


def _event_tool(value: dict[str, Any]) -> str | None:
    for key in ("tool", "tool_name", "name"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _event_summary(value: dict[str, Any]) -> str | None:
    for key in ("summary", "text", "message", "command"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None
