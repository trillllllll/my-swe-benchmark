"""Adapters for the first-party Claude Agent and Codex Python SDKs.

The CLI adapters in :mod:`cli_adapters` start a subprocess and parse its
stdout.  These adapters use the SDKs directly instead.  This matters for a
live UI: SDK notifications/messages are handed to ``emit_event`` immediately
as the agent produces them, while the complete native object is retained in
the event payload for later inspection.

Both SDKs are optional dependencies.  Importing this module never imports an
SDK, so the rest of the benchmark can still run with only the CLI adapters
installed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import importlib.util
import json
import math
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .base import AgentRunResult, NormalizedEvent, RunSpec
from .common import base_environment, parse_json_event


class AdapterUnavailableError(ImportError):
    """Raised when an optional first-party SDK is not installed."""


EmitEvent = Callable[[NormalizedEvent], None]


class _ClosableContext:
    """Turn a non-context-manager test double into a managed resource."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def __enter__(self) -> Any:
        return self.value

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        close = getattr(self.value, "close", None)
        if callable(close):
            close()


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        # A test can inject a module into sys.modules without a spec.  The
        # actual loader below will still be able to use that module.
        return module_name in __import__("sys").modules


def _load_module(module_name: str, package_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise AdapterUnavailableError(
            f"The {package_hint} is not installed. Install it with "
            f"`python -m pip install {package_hint}` before using this adapter."
        ) from exc


def _json_safe(value: Any) -> Any:
    """Convert SDK dataclasses/models/enums to JSON-safe values.

    SDK versions evolve their event classes.  This deliberately prefers
    public serialization methods and only falls back to public attributes and
    finally ``repr``; no private SDK fields are required for the adapter to
    keep an event.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if dataclasses.is_dataclass(value):
        try:
            return _json_safe(dataclasses.asdict(value))
        except Exception:
            return {
                key: _json_safe(getattr(value, key))
                for key in getattr(value, "__dataclass_fields__", {})
                if not key.startswith("_")
            }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except TypeError:
            try:
                return _json_safe(model_dump())
            except Exception:
                pass
        except Exception:
            pass
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    # Some SDK models expose a root value (for example an untagged union).
    if hasattr(value, "root"):
        try:
            return _json_safe(value.root)
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            public = {
                str(key): _json_safe(item)
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }
            if public:
                return public
        except Exception:
            pass
    return repr(value)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(value)


def _coalesce_text(primary: Iterable[str], fallback: Iterable[str]) -> str | None:
    """Choose completed message text over duplicated streaming deltas."""

    first = "".join(part for part in primary if part)
    if first:
        return first
    second = "".join(part for part in fallback if part)
    return second or None


def _value(value: Any, *names: str, default: Any = None) -> Any:
    """Read a field from either a dataclass/model or a mapping."""

    for name in names:
        if isinstance(value, Mapping) and name in value:
            candidate = value[name]
            if candidate is not None:
                return candidate
            continue
        try:
            candidate = getattr(value, name)
        except (AttributeError, TypeError):
            continue
        if candidate is not None:
            return candidate
    return default


def _string_value(value: Any, *names: str, default: str | None = None) -> str | None:
    candidate = _value(value, *names, default=default)
    if candidate is None:
        return default
    if isinstance(candidate, Enum):
        candidate = candidate.value
    return str(candidate)


def _number_value(value: Any) -> float | None:
    """Return a finite numeric value without coercing missing statistics."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _consume_task_exception(task: Any) -> None:
    """Read a detached task's exception so asyncio does not log it later."""

    try:
        task.exception()
    except BaseException:
        pass


def _interrupt_sdk_control(control: Mapping[str, Any]) -> None:
    """Interrupt and close a synchronous SDK session during cancellation."""

    turn = control.get("turn")
    if turn is not None:
        try:
            interrupt = getattr(turn, "interrupt", None)
            if callable(interrupt):
                interrupt()
        except Exception:
            pass
    codex = control.get("codex")
    if codex is not None:
        try:
            close = getattr(codex, "close", None)
            if callable(close):
                close()
        except Exception:
            pass


def _emit(emit_event: EmitEvent, *, kind: str, source: str, native: Any, data: Mapping[str, Any] | None = None) -> None:
    payload = _json_safe(native)
    event_data: dict[str, Any] = {"payload": payload}
    if data:
        event_data.update({str(key): _json_safe(value) for key, value in data.items()})
    summary = event_data.get("summary")
    if not isinstance(summary, str):
        summary = event_data.get("text") or event_data.get("delta") or event_data.get("command")
    tool = event_data.get("tool")
    if not isinstance(tool, str):
        tool = None
    emit_event(
        NormalizedEvent(
            kind=kind,
            source=source,
            raw=payload,
            summary=summary if isinstance(summary, str) else None,
            tool=tool,
            data=event_data,
        )
    )


def _run_coroutine(coro: Any) -> Any:
    """Run a coroutine from sync code, including a caller with a live loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # ``asyncio.run`` cannot be nested.  A small helper thread keeps the
    # public adapter API synchronous without imposing a loop policy on callers.
    result: list[Any] = []
    error: list[BaseException] = []

    def worker() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # propagate the original exception
            error.append(exc)

    thread = threading.Thread(target=worker, name="bench-sdk-run", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


def _normalise_sandbox(value: str | None, sandbox_type: Any) -> Any:
    if not value:
        return None
    key = value.strip().lower().replace("-", "_")
    aliases = {
        "readonly": "read_only",
        "read_only": "read_only",
        "workspacewrite": "workspace_write",
        "workspace_write": "workspace_write",
        "workspace": "workspace_write",
        "fullaccess": "full_access",
        "full_access": "full_access",
        "danger_full_access": "full_access",
    }
    member = aliases.get(key, key)
    try:
        return getattr(sandbox_type, member)
    except AttributeError:
        # An older/newer SDK may expose a string-compatible enum but no exact
        # member.  Passing the string keeps mock SDKs and future aliases usable.
        return member


def _normalise_approval(value: str | None, approval_type: Any) -> Any:
    # Codex calls its two high-level modes deny_all and auto_review.  CLI
    # values such as "never" and "on-request" are accepted in target files.
    key = (value or "auto_review").strip().lower().replace("-", "_")
    member = "deny_all" if key in {"never", "deny", "deny_all", "reject"} else "auto_review"
    try:
        return getattr(approval_type, member)
    except AttributeError:
        return member


def _claude_permission(value: str | None) -> str | None:
    if value is None:
        return None
    # Keep official Claude values intact and translate the CLI shorthand used
    # by the initial target examples.
    aliases = {
        "yolo": "bypassPermissions",
        "bypass": "bypassPermissions",
        "bypass_permissions": "bypassPermissions",
        "accept_edits": "acceptEdits",
        "accept-edits": "acceptEdits",
        "dont_ask": "dontAsk",
        "dont-ask": "dontAsk",
    }
    return aliases.get(value, aliases.get(value.lower(), value))


class _SdkAdapterMixin:
    """Protocol-compatible helpers shared by SDK adapters."""

    name: str
    transport = "sdk"

    def build_command(self, spec: RunSpec) -> list[str]:
        raise NotImplementedError(f"{self.name} is SDK-backed and has no CLI command")

    def build_environment(self, spec: RunSpec) -> dict[str, str]:
        return base_environment(spec)

    def parse_event(self, line: str) -> NormalizedEvent | None:
        return parse_json_event(line, self.name)

    def classify_exit(self, return_code: int | None, timed_out: bool) -> str:
        if timed_out:
            return "timeout"
        if return_code is None:
            return "process_error"
        return "completed" if return_code == 0 else "agent_error"

    def describe(self, spec: RunSpec) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "transport": self.transport,
            "sdk": self.module_name,
            "workspace": str(spec.workspace),
        }


class CodexSdkAdapter(_SdkAdapterMixin):
    """Run a task through the official ``openai-codex`` Python SDK."""

    name = "codex-sdk"
    module_name = "openai_codex"

    def __init__(self, sdk_module: Any | None = None) -> None:
        self._sdk_module = sdk_module

    @classmethod
    def is_available(cls) -> bool:
        return _module_available(cls.module_name)

    def _sdk(self) -> Any:
        if self._sdk_module is None:
            self._sdk_module = _load_module(self.module_name, "openai-codex")
        return self._sdk_module

    async def run(self, spec: RunSpec, emit_event: EmitEvent) -> AgentRunResult:
        """Run asynchronously as required by the runner protocol."""

        # The synchronous Codex SDK waits on a blocking notification queue.
        # Keep a shielded worker so cancellation at the deadline does not
        # leave an unobserved task, while the watchdog in ``run_sync`` asks
        # the active turn to interrupt itself.
        cancelled = threading.Event()
        control: dict[str, Any] = {}

        def forward_event(event: NormalizedEvent) -> None:
            if not cancelled.is_set():
                emit_event(event)

        worker = asyncio.create_task(asyncio.to_thread(self.run_sync, spec, forward_event, control))
        try:
            return await asyncio.wait_for(asyncio.shield(worker), timeout=max(0.001, spec.timeout_seconds))
        except asyncio.TimeoutError:
            cancelled.set()
            # Most SDK versions finish promptly after interrupt/close.  Wait
            # for that cleanup before the runner snapshots or removes the
            # workspace, but retain a hard bound for broken runtimes.
            grace = min(2.0, max(0.1, spec.timeout_seconds * 0.1))
            try:
                await asyncio.wait_for(asyncio.to_thread(_interrupt_sdk_control, control), timeout=grace)
            except asyncio.TimeoutError:
                pass
            try:
                completed = await asyncio.wait_for(asyncio.shield(worker), timeout=grace)
                if completed.status == "timeout":
                    return completed
            except Exception:
                worker.add_done_callback(_consume_task_exception)
                return AgentRunResult(
                    status="timeout",
                    timed_out=True,
                    error=f"Codex SDK timed out after {spec.timeout_seconds:g}s",
                    metadata={"timeout_seconds": spec.timeout_seconds, "cleanup_pending": True},
                )
            # ``run_sync`` has its own interrupt watchdog.  If a particular
            # SDK build does not unblock promptly, return a bounded result to
            # the runner and consume the eventual worker outcome quietly.
            worker.add_done_callback(_consume_task_exception)
            return AgentRunResult(
                status="timeout",
                timed_out=True,
                error=f"Codex SDK timed out after {spec.timeout_seconds:g}s",
                metadata={"timeout_seconds": spec.timeout_seconds},
            )
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.to_thread(_interrupt_sdk_control, control)
            worker.add_done_callback(_consume_task_exception)
            raise

    def run_sync(
        self,
        spec: RunSpec,
        emit_event: EmitEvent,
        _control: dict[str, Any] | None = None,
    ) -> AgentRunResult:
        sdk = self._sdk()
        started = time.monotonic()
        final_response: str | None = None
        terminal_status: str | None = None
        terminal_error: str | None = None
        session_id: str | None = None
        usage: dict[str, Any] | None = None
        cost_usd: float | None = None
        metadata: dict[str, Any] = {}
        saw_terminal = False
        delta_text: list[str] = []
        completed_text: list[str] = []
        tool_calls = 0
        timeout_triggered = threading.Event()
        interrupt_timer: threading.Timer | None = None

        try:
            codex_kwargs: dict[str, Any] = {}
            config_type = getattr(sdk, "CodexConfig", None)
            executable = spec.target.executable.strip()
            # CodexConfig validates custom binaries as filesystem paths.  The
            # default target uses the SDK's pinned runtime, so only pass an
            # executable that looks like an explicit path and exists.
            if config_type:
                config_kwargs: dict[str, Any] = {"env": self.build_environment(spec)}
                if executable and executable.lower() not in {"codex", "codex.exe"}:
                    executable_path = Path(executable)
                    if executable_path.exists():
                        config_kwargs["codex_bin"] = str(executable_path)
                try:
                    codex_kwargs["config"] = config_type(**config_kwargs)
                except TypeError:
                    # Keep simple injected fakes (which often model only
                    # ``codex_bin``) compatible with the production adapter.
                    config_kwargs.pop("env", None)
                    try:
                        codex_kwargs["config"] = config_type(**config_kwargs)
                    except TypeError:
                        codex_kwargs.pop("codex_bin", None)
                        codex_kwargs["config"] = config_type()

            codex_type = getattr(sdk, "Codex", None)
            sandbox_type = getattr(sdk, "Sandbox", None)
            approval_type = getattr(sdk, "ApprovalMode", None)
            if codex_type is None or sandbox_type is None or approval_type is None:
                raise AdapterUnavailableError(
                    "The installed openai-codex package does not expose the public "
                    "Codex/Sandbox/ApprovalMode API required by this adapter."
                )

            codex_instance = codex_type(**codex_kwargs)
            codex_context = (
                codex_instance
                if hasattr(codex_instance, "__enter__") and hasattr(codex_instance, "__exit__")
                else _ClosableContext(codex_instance)
            )
            with codex_context as codex:
                if _control is not None:
                    _control["codex"] = codex
                thread_kwargs: dict[str, Any] = {
                    "cwd": str(spec.workspace),
                    "ephemeral": True,
                    "approval_mode": _normalise_approval(
                        spec.target.approval_policy or spec.target.permission_mode,
                        approval_type,
                    ),
                }
                sandbox = _normalise_sandbox(spec.target.sandbox, sandbox_type)
                if sandbox is not None:
                    thread_kwargs["sandbox"] = sandbox
                if spec.target.model:
                    thread_kwargs["model"] = spec.target.model
                thread = codex.thread_start(**thread_kwargs)
                thread_id = _string_value(thread, "id", default=None)
                if thread_id:
                    metadata["thread_id"] = thread_id
                    session_id = thread_id
                turn = thread.turn(spec.prompt)
                if _control is not None:
                    _control["turn"] = turn
                turn_id = _string_value(turn, "id", default=None)
                if turn_id:
                    metadata["turn_id"] = turn_id

                def request_interrupt() -> None:
                    timeout_triggered.set()
                    try:
                        turn.interrupt()
                    except Exception:
                        # The outer async wrapper still enforces the deadline
                        # if an SDK version cannot interrupt a blocked stream.
                        pass

                remaining = max(0.0, spec.timeout_seconds - (time.monotonic() - started))
                interrupt_timer = threading.Timer(remaining, request_interrupt)
                interrupt_timer.daemon = True
                interrupt_timer.start()

                try:
                    for native_event in turn.stream():
                        if timeout_triggered.is_set() or time.monotonic() - started > spec.timeout_seconds:
                            timeout_triggered.set()
                            try:
                                turn.interrupt()
                            except Exception:
                                pass
                            return AgentRunResult(
                                status="timeout",
                                timed_out=True,
                                final_response=_coalesce_text(completed_text, delta_text),
                                session_id=session_id,
                                usage=usage,
                                cost_usd=cost_usd,
                                metadata={
                                    **metadata,
                                    "duration_ms": round((time.monotonic() - started) * 1000),
                                    "tool_calls": tool_calls,
                                },
                            )

                        kind, event_data = self._event_kind(native_event)
                        if kind == "tool_call":
                            tool_calls += 1
                        if kind == "assistant":
                            text = event_data.get("text")
                            if isinstance(text, str) and text:
                                completed_text.append(text)
                        elif kind == "assistant_delta":
                            text = event_data.get("delta")
                            if isinstance(text, str) and text:
                                delta_text.append(text)
                        if kind == "usage":
                            candidate = event_data.get("usage")
                            if isinstance(candidate, Mapping):
                                usage = dict(candidate)
                                metadata["usage"] = usage
                        if kind == "turn_completed":
                            saw_terminal = True
                            terminal_status = event_data.get("status")
                            error_value = event_data.get("error")
                            if error_value is not None:
                                terminal_error = str(error_value)
                            if isinstance(event_data.get("usage"), Mapping):
                                usage = dict(event_data["usage"])
                            candidate_cost = _number_value(event_data.get("cost_usd"))
                            if candidate_cost is not None:
                                cost_usd = candidate_cost
                            metadata.update({key: value for key, value in event_data.items() if key != "status"})
                        _emit(emit_event, kind=kind, source=self.name, native=native_event, data=event_data)
                finally:
                    if interrupt_timer is not None:
                        interrupt_timer.cancel()
                    if _control is not None:
                        _control.pop("turn", None)
                if _control is not None:
                    _control.pop("codex", None)

            final_response = _coalesce_text(completed_text, delta_text)
            if timeout_triggered.is_set() or time.monotonic() - started > spec.timeout_seconds:
                return AgentRunResult(
                    status="timeout",
                    timed_out=True,
                    final_response=final_response,
                    session_id=session_id,
                    usage=usage,
                    cost_usd=cost_usd,
                    metadata={
                        **metadata,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "tool_calls": tool_calls,
                    },
                )
            if terminal_status in {"failed", "error", "cancelled", "canceled", "interrupted"}:
                status = "agent_error"
            elif saw_terminal:
                status = "completed"
            else:
                status = "agent_error"
                metadata["missing_terminal_event"] = True
            metadata.update({"duration_ms": round((time.monotonic() - started) * 1000), "tool_calls": tool_calls})
            return AgentRunResult(
                status=status,
                return_code=0 if status == "completed" else 1,
                final_response=final_response,
                session_id=session_id,
                usage=usage,
                cost_usd=cost_usd,
                error=terminal_error,
                metadata=metadata,
            )
        except AdapterUnavailableError:
            raise
        except TimeoutError as exc:
            return AgentRunResult(
                status="timeout",
                timed_out=True,
                final_response=_coalesce_text(completed_text, delta_text),
                session_id=session_id,
                usage=usage,
                cost_usd=cost_usd,
                error=str(exc) or "Codex SDK timed out",
                metadata={"duration_ms": round((time.monotonic() - started) * 1000), "tool_calls": tool_calls},
            )
        except Exception as exc:
            if timeout_triggered.is_set() or time.monotonic() - started > spec.timeout_seconds:
                return AgentRunResult(
                    status="timeout",
                    timed_out=True,
                    final_response=_coalesce_text(completed_text, delta_text),
                    session_id=session_id,
                    usage=usage,
                    cost_usd=cost_usd,
                    error="Codex SDK timed out",
                    metadata={"duration_ms": round((time.monotonic() - started) * 1000), "tool_calls": tool_calls},
                )
            return AgentRunResult(
                status="agent_error",
                final_response=_coalesce_text(completed_text, delta_text),
                session_id=session_id,
                usage=usage,
                cost_usd=cost_usd,
                error=f"{type(exc).__name__}: {exc}",
                metadata={"duration_ms": round((time.monotonic() - started) * 1000), "tool_calls": tool_calls},
            )

    async def run_async(self, spec: RunSpec, emit_event: EmitEvent) -> AgentRunResult:
        """Async convenience wrapper for applications already using asyncio."""

        return await self.run(spec, emit_event)

    @staticmethod
    def _event_kind(event: Any) -> tuple[str, dict[str, Any]]:
        method = _string_value(event, "method", default="") or ""
        payload = _value(event, "payload", default=None)
        data: dict[str, Any] = {"method": method}
        if method == "item/agentMessage/delta":
            data["delta"] = _string_value(payload, "delta", default="") or ""
            return "assistant_delta", data
        if method in {"thread/started", "turn/started"}:
            return method.replace("/", "_"), data
        if method == "turn/completed":
            turn = _value(payload, "turn", default=payload)
            status = _string_value(turn, "status", default=None)
            if status:
                data["status"] = status
            error = _value(turn, "error", default=None)
            if error is not None:
                data["error"] = _json_safe(error)
            turn_usage = _value(turn, "usage", "token_usage", "tokenUsage", default=None)
            if turn_usage is not None:
                data["usage"] = _json_safe(turn_usage)
            turn_cost = _number_value(_value(turn, "cost_usd", "costUsd", "total_cost_usd", "totalCostUsd", default=None))
            if turn_cost is not None:
                data["cost_usd"] = turn_cost
            return "turn_completed", data
        if method in {"thread/tokenUsage/updated", "thread/token_usage/updated"}:
            token_usage = _value(payload, "token_usage", "tokenUsage", default=None)
            if token_usage is not None:
                data["usage"] = _json_safe(token_usage)
            turn_id = _string_value(payload, "turn_id", "turnId", default=None)
            if turn_id:
                data["turn_id"] = turn_id
            return "usage", data
        if method == "item/completed" or method == "item/started":
            item = _value(payload, "item", default=None)
            root = _value(item, "root", default=item)
            item_type = (_string_value(root, "type", default="") or "").lower()
            text = _string_value(root, "text", default=None)
            if text:
                data["text"] = text
            command = _string_value(root, "command", default=None)
            if command:
                data["command"] = command
            name = _string_value(root, "name", default=None)
            if name:
                data["tool"] = name
            tool_item_types = {
                "commandexecution",
                "command_execution",
                "mcptoolcall",
                "mcp_tool_call",
                "mcpserverrequest",
                "mcp_server_request",
                "filechange",
                "file_change",
                "websearch",
                "web_search",
                "toolcall",
                "tool_call",
            }
            if item_type in tool_item_types:
                return ("tool_call" if method == "item/started" else "tool_result"), data
            if item_type in {"agentmessage", "agent_message"}:
                return "assistant", data
            return "item", data
        if method == "turn/failed":
            data["status"] = "failed"
            return "turn_completed", data
        if method == "error":
            return "error", data
        if method:
            return method.replace("/", "_"), data
        return "sdk_event", data


class ClaudeAgentSdkAdapter(_SdkAdapterMixin):
    """Run a task through the official ``claude-agent-sdk`` Python package."""

    name = "claude-agent-sdk"
    module_name = "claude_agent_sdk"

    def __init__(self, sdk_module: Any | None = None) -> None:
        self._sdk_module = sdk_module

    @classmethod
    def is_available(cls) -> bool:
        return _module_available(cls.module_name)

    def _sdk(self) -> Any:
        if self._sdk_module is None:
            self._sdk_module = _load_module(self.module_name, "claude-agent-sdk")
        return self._sdk_module

    async def run(self, spec: RunSpec, emit_event: EmitEvent) -> AgentRunResult:
        """Run asynchronously using Claude's native async message stream."""

        self._sdk()
        return await self._run_async(spec, emit_event)

    def run_sync(self, spec: RunSpec, emit_event: EmitEvent) -> AgentRunResult:
        # Resolve the optional package before entering asyncio so callers get
        # the useful install hint instead of a generic coroutine error.
        self._sdk()
        return _run_coroutine(self._run_async(spec, emit_event))

    async def run_async(self, spec: RunSpec, emit_event: EmitEvent) -> AgentRunResult:
        """Run the Claude query without creating a nested event loop."""

        return await self._run_async(spec, emit_event)

    async def _run_async(self, spec: RunSpec, emit_event: EmitEvent) -> AgentRunResult:
        sdk = self._sdk()
        started = time.monotonic()
        final_response: str | None = None
        session_id: str | None = None
        usage: dict[str, Any] | None = None
        cost_usd: float | None = None
        metadata: dict[str, Any] = {}
        collected_text: list[str] = []
        delta_text: list[str] = []
        tool_calls = 0
        last_result: Any = None

        async def consume() -> None:
            nonlocal final_response, last_result, tool_calls, session_id, usage, cost_usd
            options_type = getattr(sdk, "ClaudeAgentOptions", None)
            query = getattr(sdk, "query", None)
            if options_type is None or query is None:
                raise AdapterUnavailableError(
                    "The installed claude-agent-sdk package does not expose "
                    "query and ClaudeAgentOptions."
                )
            option_kwargs: dict[str, Any] = {
                "cwd": str(spec.workspace),
                "setting_sources": [],
                "env": self.build_environment(spec),
            }
            if spec.target.model:
                option_kwargs["model"] = spec.target.model
            permission_mode = _claude_permission(spec.target.permission_mode)
            if permission_mode:
                option_kwargs["permission_mode"] = permission_mode
            # Keep compatibility with older SDKs that do not know one of the
            # isolation-related options.  Drop only optional fields, in order,
            # and retain the core cwd/model/permission configuration.
            options = None
            for optional in ("setting_sources", "env"):
                try:
                    options = options_type(**option_kwargs)
                    break
                except TypeError:
                    option_kwargs.pop(optional, None)
            if options is None:
                options = options_type(**option_kwargs)

            async for message in query(prompt=spec.prompt, options=options):
                if time.monotonic() - started > spec.timeout_seconds:
                    raise asyncio.TimeoutError
                kind, event_data = self._event_kind(message, sdk)
                if kind == "tool_call":
                    tool_calls += 1
                text = event_data.get("text") or event_data.get("result") or event_data.get("delta")
                if isinstance(text, str) and text:
                    if kind == "assistant_delta":
                        delta_text.append(text)
                    elif kind == "assistant":
                        collected_text.append(text)
                if kind == "turn_completed":
                    last_result = message
                    result_text = event_data.get("result")
                    if isinstance(result_text, str):
                        final_response = result_text
                    candidate_session = event_data.get("session_id")
                    if candidate_session is not None:
                        session_id = str(candidate_session)
                    candidate_usage = event_data.get("usage")
                    if isinstance(candidate_usage, Mapping):
                        usage = dict(candidate_usage)
                    candidate_cost = _number_value(event_data.get("total_cost_usd"))
                    if candidate_cost is not None:
                        cost_usd = candidate_cost
                    metadata.update({key: value for key, value in event_data.items() if key not in {"result", "is_error"}})
                _emit(emit_event, kind=kind, source=self.name, native=message, data=event_data)

        try:
            await asyncio.wait_for(consume(), timeout=max(0.001, spec.timeout_seconds))
        except AdapterUnavailableError:
            raise
        except asyncio.TimeoutError:
            return AgentRunResult(
                status="timeout",
                timed_out=True,
                final_response=final_response or _coalesce_text(collected_text, delta_text),
                session_id=session_id,
                usage=usage,
                cost_usd=cost_usd,
                metadata={
                    **metadata,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "tool_calls": tool_calls,
                },
            )
        except Exception as exc:
            return AgentRunResult(
                status="agent_error",
                final_response=final_response or _coalesce_text(collected_text, delta_text),
                session_id=session_id,
                usage=usage,
                cost_usd=cost_usd,
                error=f"{type(exc).__name__}: {exc}",
                metadata={
                    **metadata,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "tool_calls": tool_calls,
                },
            )

        if final_response is None:
            final_response = _coalesce_text(collected_text, delta_text)
        is_error = bool(_value(last_result, "is_error", default=False)) if last_result is not None else False
        terminal_reason = _string_value(last_result, "terminal_reason", default=None) if last_result is not None else None
        aborted_reasons = {"aborted_streaming", "aborted_tools", "interrupted", "cancelled", "canceled"}
        status = "agent_error" if is_error or terminal_reason in aborted_reasons else "completed"
        metadata.update({"duration_ms": round((time.monotonic() - started) * 1000), "tool_calls": tool_calls})
        return AgentRunResult(
            status=status,
            return_code=0 if status == "completed" else 1,
            final_response=final_response,
            session_id=session_id,
            usage=usage,
            cost_usd=cost_usd,
            metadata=metadata,
        )

    @staticmethod
    def _event_kind(message: Any, sdk: Any) -> tuple[str, dict[str, Any]]:
        class_name = type(message).__name__
        data: dict[str, Any] = {"message_type": class_name}
        if class_name == "ResultMessage":
            result = _string_value(message, "result", default=None)
            if result is not None:
                data["result"] = result
            is_error = bool(_value(message, "is_error", default=False))
            data["is_error"] = is_error
            for field_name in ("subtype", "stop_reason", "terminal_reason", "session_id", "duration_ms", "duration_api_ms", "num_turns", "total_cost_usd", "usage", "model_usage", "errors"):
                value = _value(message, field_name, default=None)
                if value is not None:
                    data[field_name] = _json_safe(value)
            return "turn_completed", data
        if class_name == "AssistantMessage":
            content = _value(message, "content", default=[]) or []
            blocks = list(content) if isinstance(content, Iterable) and not isinstance(content, (str, bytes, Mapping)) else [content]
            names: list[str] = []
            texts: list[str] = []
            for block in blocks:
                block_name = _string_value(block, "name", default=None)
                if block_name:
                    names.append(block_name)
                block_text = _string_value(block, "text", default=None)
                if block_text:
                    texts.append(block_text)
            if names:
                data["tool"] = names[0]
                data["tools"] = names
                return "tool_call", data
            if texts:
                data["text"] = "".join(texts)
            return "assistant", data
        if class_name == "StreamEvent":
            raw_event = _value(message, "event", default={})
            event_type = _string_value(raw_event, "type", default="stream") or "stream"
            data["event_type"] = event_type
            delta = _value(_value(raw_event, "delta", default={}), "text", default=None)
            if isinstance(delta, str):
                data["delta"] = delta
            return "assistant_delta" if "delta" in event_type else "stream_event", data
        if class_name in {"UserMessage", "ToolResultMessage"}:
            return "tool_result", data
        if class_name == "SystemMessage":
            subtype = _string_value(message, "subtype", default=None)
            if subtype:
                data["subtype"] = subtype
            return "system", data
        # Keep support for future SDK message classes by using their explicit
        # ``type`` field when available, otherwise a stable lower-case name.
        explicit = _string_value(message, "type", default=None)
        return (explicit or class_name).lower(), data


__all__ = [
    "AdapterUnavailableError",
    "AgentRunResult",
    "ClaudeAgentSdkAdapter",
    "CodexSdkAdapter",
]
