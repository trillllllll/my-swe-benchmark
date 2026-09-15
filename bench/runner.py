from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import signal
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .adapters.base import AgentRunResult, NormalizedEvent, RunSpec, TargetConfig
from .adapters.common import to_jsonable
from .adapters.registry import get_adapter
from .config import load_case
from . import __version__ as harness_version


class RunError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunLifecycle:
    """A lifecycle notification emitted by :func:`execute_case_async`.

    ``phase`` is one of ``prepared``, ``started`` or ``finished``.  The
    callback receives this object after the corresponding state has been
    established on disk.  ``result`` is populated for the ``finished``
    notification and contains the same JSON-safe payload written to
    ``run.json``.
    """

    phase: Literal["prepared", "started", "finished"]
    run_id: str
    case: str
    target: str
    run_dir: Path
    workspace: Path
    status: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


EventCallback = Callable[[dict[str, Any]], Any]
LifecycleCallback = Callable[[RunLifecycle], Any]


@dataclass(frozen=True)
class MatrixTargetResult:
    """Outcome for one target in a matrix execution.

    ``run_dir`` and ``result`` are populated after ``execute_case_async``
    writes the normal per-run artifacts.  A preparation or orchestration
    error is kept on the item so one target cannot hide the other targets'
    outcomes.
    """

    target: str
    run_dir: Path | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class MatrixExecutionResult:
    """Ordered results returned by :func:`execute_matrix`.

    The ``runs`` tuple follows the input target order.  ``run_dirs`` and
    ``results`` are convenient mappings for callers that only need the
    successful run artifacts.  Targets that fail before producing a
    ``run.json`` remain visible in ``runs`` and ``errors``.
    """

    runs: tuple[MatrixTargetResult, ...]

    @property
    def run_dirs(self) -> dict[str, Path]:
        return {
            item.target: item.run_dir
            for item in self.runs
            if item.run_dir is not None
        }

    @property
    def ordered_run_dirs(self) -> tuple[Path | None, ...]:
        """Run directories in exactly the order targets were supplied."""

        return tuple(item.run_dir for item in self.runs)

    @property
    def results(self) -> dict[str, dict[str, Any]]:
        return {
            item.target: item.result
            for item in self.runs
            if item.result is not None
        }

    @property
    def ordered_results(self) -> tuple[dict[str, Any] | None, ...]:
        """Serialized ``run.json`` values in input-target order."""

        return tuple(item.result for item in self.runs)

    @property
    def errors(self) -> dict[str, str]:
        return {
            item.target: item.error
            for item in self.runs
            if item.error is not None
        }

    def __len__(self) -> int:
        return len(self.runs)

    def __iter__(self) -> Iterator[MatrixTargetResult]:
        return iter(self.runs)

    def __getitem__(self, target: str) -> MatrixTargetResult:
        for item in self.runs:
            if item.target == target:
                return item
        raise KeyError(target)

    def items(self) -> Iterator[tuple[str, MatrixTargetResult]]:
        return ((item.target, item) for item in self.runs)

    def values(self) -> Iterator[MatrixTargetResult]:
        return iter(self.runs)

    def keys(self) -> Iterator[str]:
        return (item.target for item in self.runs)


@dataclass(frozen=True)
class SuiteTargetResult:
    """Outcome for one ``case × target`` execution in a suite.

    ``case`` is the stable case label used by the suite (the manifest's
    ``id`` when available, or a caller supplied mapping key).  ``case_path``
    is retained as a convenience for callers that need to locate the source
    fixture.  An orchestration/setup failure is represented by ``error`` and
    does not prevent the other suite items from running.
    """

    case: str
    target: str
    run_dir: Path | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    case_path: Path | None = None

    @property
    def case_id(self) -> str:
        """Alias used by report consumers that call the label a case id."""

        return self.case

    @property
    def status(self) -> str:
        """Return the serialized run status or an orchestration sentinel."""

        if self.result is not None:
            return str(self.result.get("status") or "unknown")
        return "orchestration_error" if self.error else "unknown"

    @property
    def ok(self) -> bool:
        return self.error is None and self.result is not None


@dataclass(frozen=True)
class SuiteExecutionResult:
    """Ordered outcomes for a multi-case, multi-target suite.

    The ``runs`` tuple is ordered by case input order and then target input
    order, regardless of completion order.  Flat tuple-key mappings are
    exposed for programmatic consumers, while ``by_case``/``by_target`` are
    convenient grouped views for CLI and web reports.
    """

    runs: tuple[SuiteTargetResult, ...] = ()
    cases: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    suite_id: str | None = None
    started_at: str | None = None
    duration_ms: int | None = None

    @property
    def run_dirs(self) -> dict[tuple[str, str], Path]:
        """Map ``(case, target)`` to the generated run directory."""

        return {
            (item.case, item.target): item.run_dir
            for item in self.runs
            if item.run_dir is not None
        }

    @property
    def ordered_run_dirs(self) -> tuple[Path | None, ...]:
        return tuple(item.run_dir for item in self.runs)

    @property
    def results(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Map ``(case, target)`` to a serialized ``run.json`` object."""

        return {
            (item.case, item.target): item.result
            for item in self.runs
            if item.result is not None
        }

    @property
    def ordered_results(self) -> tuple[dict[str, Any] | None, ...]:
        return tuple(item.result for item in self.runs)

    @property
    def errors(self) -> dict[tuple[str, str], str]:
        """Map failed ``(case, target)`` items to their orchestration error."""

        return {
            (item.case, item.target): item.error
            for item in self.runs
            if item.error is not None
        }

    @property
    def by_case(self) -> dict[str, tuple[SuiteTargetResult, ...]]:
        grouped: dict[str, list[SuiteTargetResult]] = {}
        for item in self.runs:
            grouped.setdefault(item.case, []).append(item)
        return {case: tuple(items) for case, items in grouped.items()}

    @property
    def by_target(self) -> dict[str, tuple[SuiteTargetResult, ...]]:
        grouped: dict[str, list[SuiteTargetResult]] = {}
        for item in self.runs:
            grouped.setdefault(item.target, []).append(item)
        return {target: tuple(items) for target, items in grouped.items()}

    @property
    def summary(self) -> dict[str, int]:
        """Small aggregate suitable for a CLI status line or API response."""

        passed = sum(item.result is not None and item.status == "passed" for item in self.runs)
        failed = sum(item.result is not None and item.status not in {"passed", "completed"} for item in self.runs)
        orchestration_errors = sum(item.error is not None for item in self.runs)
        timed_out = sum(
            item.result is not None and bool(item.result.get("timed_out"))
            for item in self.runs
        )
        return {
            "total": len(self.runs),
            "passed": passed,
            "failed": failed,
            "orchestration_errors": orchestration_errors,
            "timed_out": timed_out,
        }

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        """Flattened rows for tabular CLI/web reports.

        The nested ``result`` object remains available on each
        :class:`SuiteTargetResult`; this view mirrors the matrix report shape
        and deliberately keeps missing values as ``None``/``unknown`` rather
        than turning an orchestration failure into a fake run.
        """

        rows: list[dict[str, Any]] = []
        for item in self.runs:
            result = item.result or {}
            grader = result.get("grader") or {}
            rows.append(
                {
                    "case": item.case,
                    "case_path": str(item.case_path) if item.case_path is not None else None,
                    "target": item.target,
                    "model": result.get("model"),
                    "adapter": result.get("adapter"),
                    "status": result.get("status") if item.result is not None else "orchestration_error",
                    "score": grader.get("score") if item.result is not None else None,
                    "duration_ms": result.get("duration_ms") if item.result is not None else None,
                    "tool_calls": result.get("tool_calls") if item.result is not None else None,
                    "timed_out": bool(result.get("timed_out")) if item.result is not None else False,
                    "run_dir": str(item.run_dir) if item.run_dir is not None else None,
                    "error": item.error or (result.get("error") if item.result is not None else None),
                }
            )
        return tuple(rows)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the suite result without leaking ``Path`` objects."""

        return {
            "suite_id": self.suite_id,
            "cases": list(self.cases),
            "targets": list(self.targets),
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "runs": [
                {
                    **row,
                    "result": item.result,
                }
                for item, row in zip(self.runs, self.rows)
            ],
            "rows": list(self.rows),
        }

    # ``as_dict`` is a common spelling in report integrations.
    as_dict = to_dict

    def __len__(self) -> int:
        return len(self.runs)

    def __iter__(self) -> Iterator[SuiteTargetResult]:
        return iter(self.runs)

    def __getitem__(self, index: int | tuple[str, str]) -> SuiteTargetResult:
        if isinstance(index, int):
            return self.runs[index]
        case, target = index
        for item in self.runs:
            if item.case == case and item.target == target:
                return item
        raise KeyError(index)


def execute_case(
    case_path: Path,
    target: TargetConfig,
    runs_root: Path,
    *,
    timeout_seconds: float | None = None,
    keep_workspace: bool = False,
    live: bool = True,
    event_prefix: str | None = None,
    event_callback: EventCallback | None = None,
    lifecycle_callback: LifecycleCallback | None = None,
) -> Path:
    """Run one case while preserving the original synchronous public API."""
    return asyncio.run(
        execute_case_async(
            case_path,
            target,
            runs_root,
            timeout_seconds=timeout_seconds,
            keep_workspace=keep_workspace,
            live=live,
            event_prefix=event_prefix,
            event_callback=event_callback,
            lifecycle_callback=lifecycle_callback,
        )
    )


async def execute_case_async(
    case_path: Path,
    target: TargetConfig,
    runs_root: Path,
    *,
    timeout_seconds: float | None = None,
    keep_workspace: bool = False,
    live: bool = True,
    event_prefix: str | None = None,
    event_callback: EventCallback | None = None,
    lifecycle_callback: LifecycleCallback | None = None,
    cancel_before_agent: bool = False,
) -> Path:
    case_path = case_path.resolve()
    runs_root = runs_root.resolve()
    case = load_case(case_path)
    case_dir = case["_case_dir"]
    case_id = str(case.get("id") or case_dir.name)
    run_id = f"{case_id}__{target.name}__{uuid.uuid4().hex[:10]}"
    run_dir = runs_root / run_id
    workspace = run_dir / "workspace"
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture = case_dir / str(case.get("fixture", "fixture"))
    if not fixture.is_dir():
        raise RunError(f"Fixture directory does not exist: {fixture}")
    shutil.copytree(fixture, workspace)
    _initialize_git(workspace)
    prompt = _read_prompt(case_dir / str(case.get("instruction", "prompt.md")))
    timeout = float(timeout_seconds or (case.get("limits") or {}).get("timeout_seconds", 300))
    adapter = get_adapter(target.adapter)
    spec = RunSpec(target=target, prompt=prompt, workspace=workspace, timeout_seconds=timeout, run_dir=run_dir)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "case": case_id,
                "case_version": case.get("version", 1),
                "agent_system": target.name,
                "harness_version": harness_version,
                "event_schema_version": 2,
                "target": target.name,
                "adapter": target.adapter,
                "model": target.model,
                "cli_version": "unknown",
                "transport": getattr(adapter, "transport", "unknown"),
                **to_jsonable(adapter.describe(spec)),
                "timeout_seconds": timeout,
                "execution_mode": "local-unisolated",
                "environment_fingerprint": _environment_fingerprint(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    events_path = run_dir / "events.jsonl"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    events_path.touch()
    event_count = 0
    events: list[dict[str, Any]] = []
    started = time.monotonic()

    def notify_lifecycle(notification: RunLifecycle) -> None:
        """Deliver UI notifications without allowing observers to fail a run."""

        if lifecycle_callback is None:
            return
        try:
            lifecycle_callback(notification)
        except Exception:
            # A display layer is an observer, not part of the agent run's
            # correctness boundary.  In particular, a closed TUI queue must
            # not turn a successful agent execution into a process error.
            pass

    notify_lifecycle(
        RunLifecycle(
            phase="prepared",
            run_id=run_id,
            case=case_id,
            target=target.name,
            run_dir=run_dir,
            workspace=workspace,
            status="queued",
        )
    )

    def emit_event(event: NormalizedEvent) -> None:
        nonlocal event_count
        event_count += 1
        payload = {
            "seq": event_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "target": target.name,
            "kind": event.kind,
            "category": event.category,
            "action": event.action,
            "title": event.title,
            "detail": event.detail,
            "status": event.status,
            "command": event.command,
            "paths": list(event.paths),
            "duration_ms": event.duration_ms,
            "source": event.source,
            "channel": event.channel,
            "tool": event.tool,
            "summary": event.summary,
            "raw": to_jsonable(event.raw),
            "data": to_jsonable(event.data),
        }
        events.append(payload)
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if event_callback is not None:
            try:
                event_callback(payload)
            except Exception:
                # Event consumers are intentionally best-effort.  The JSONL
                # file remains the authoritative stream for later replay.
                pass
        if live:
            _print_live_event(payload, event_prefix)

    stopped = False
    notify_lifecycle(
        RunLifecycle(
            phase="started",
            run_id=run_id,
            case=case_id,
            target=target.name,
            run_dir=run_dir,
            workspace=workspace,
            status="running",
        )
    )
    try:
        if cancel_before_agent:
            # A queued TUI item can be stopped before an agent process is
            # created. Still run the normal artifact finalization path so the
            # attempt has a workspace, logs, diff, and an explicit not_run
            # grader record just like a process that was stopped in-flight.
            stopped = True
            result = AgentRunResult(
                status="stopped",
                error="stopped by user",
                metadata={"stopped_by_user": True, "before_agent_start": True},
            )
        else:
            # Call the adapter once, then inspect the returned value. This
            # works for native async methods and callable wrappers alike.
            if inspect.iscoroutinefunction(adapter.run):
                maybe_result = adapter.run(spec, emit_event)
            else:
                # Keep blocking adapters off the runner loop so live events
                # remain consumable by a UI or matrix coordinator.
                maybe_result = await asyncio.to_thread(adapter.run, spec, emit_event)
            result = await maybe_result if inspect.isawaitable(maybe_result) else maybe_result
            if not isinstance(result, AgentRunResult):
                result = AgentRunResult(
                    status="process_error",
                    error=f"Adapter returned {type(result).__name__}, expected AgentRunResult",
                )
            # CLI adapters may consume task cancellation after terminating
            # their process tree and return an explicit stopped result. Treat
            # that the same as runner-level cancellation so partial runs are
            # never graded.
            stopped = result.status == "stopped" or bool((result.metadata or {}).get("stopped_by_user"))
    except asyncio.CancelledError:
        # Adapter implementations terminate their process/session in their
        # own cancellation handlers.  Continue through artifact finalization
        # so a user-initiated stop is inspectable instead of losing run.json.
        stopped = True
        result = AgentRunResult(
            status="stopped",
            error="stopped by user",
            metadata={"stopped_by_user": True},
        )
    except Exception as exc:  # SDK import/auth/process failures are run data.
        result = AgentRunResult(status="process_error", error=f"{type(exc).__name__}: {exc}")

    duration_ms = round((time.monotonic() - started) * 1000)
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    # Both operations invoke subprocesses.  Keep them off the matrix event
    # loop so another target can continue consuming its live agent output.
    patch_task = asyncio.create_task(asyncio.to_thread(_git_diff, workspace))
    try:
        patch = await asyncio.shield(patch_task)
    except asyncio.CancelledError:
        stopped = True
        try:
            patch = await patch_task
        except Exception:
            patch = ""
    (run_dir / "patch.diff").write_text(patch, encoding="utf-8")
    if stopped:
        grader = {"status": "not_run", "score": None, "reason": "user_stopped"}
    else:
        grader_task = asyncio.create_task(_run_grader(case, workspace, run_dir, timeout))
        try:
            grader = await grader_task
        except asyncio.CancelledError:
            stopped = True
            # Cancellation propagates into the grader task, which terminates
            # its process group before re-raising. The run can therefore be
            # finalized immediately without leaving grader descendants alive.
            grader = {"status": "not_run", "score": None, "reason": "user_stopped"}
    (run_dir / "grader.json").write_text(json.dumps(grader, indent=2, ensure_ascii=False), encoding="utf-8")
    status = result.status
    if stopped:
        status = "stopped"
        result = AgentRunResult(
            status="stopped",
            return_code=result.return_code,
            timed_out=False,
            stdout=result.stdout,
            stderr=result.stderr,
            final_response=result.final_response,
            session_id=result.session_id,
            usage=result.usage,
            cost_usd=result.cost_usd,
            error="stopped by user",
            metadata={**(result.metadata or {}), "stopped_by_user": True},
        )
    elif status == "completed" and grader.get("status") == "passed":
        status = "passed"
    elif grader.get("status") in {"failed", "timeout", "error"} and status in {"completed", "passed"}:
        status = "grader_failed"
    output = {
        "run_id": run_id,
        "case": case_id,
        "case_version": case.get("version", 1),
        "agent_system": target.name,
        "harness_version": harness_version,
        "target": target.name,
        "model": target.model,
        "adapter": target.adapter,
        "cli_version": "unknown",
        "transport": getattr(adapter, "transport", "unknown"),
        "status": status,
        "return_code": result.return_code,
        "timed_out": result.timed_out,
        "duration_ms": duration_ms,
        "event_count": event_count,
        "tool_calls": _count_tool_calls(events),
        "final_response": result.final_response,
        "session_id": result.session_id,
        "usage": result.usage,
        "cost_usd": result.cost_usd,
        "error": result.error,
        "metadata": to_jsonable(result.metadata or {}),
        "grader": grader,
        "execution_mode": "local-unisolated",
    }
    (run_dir / "run.json").write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    notify_lifecycle(
        RunLifecycle(
            phase="finished",
            run_id=run_id,
            case=case_id,
            target=target.name,
            run_dir=run_dir,
            workspace=workspace,
            status=status,
            result=output,
            error=result.error,
        )
    )
    # A synchronous SDK may still be unwinding after a hard timeout. Keep its
    # workspace until that worker is gone instead of deleting files underneath
    # an active agent process.
    if not keep_workspace and not stopped and not (result.metadata or {}).get("cleanup_pending"):
        _remove_tree(workspace)
    return run_dir


def execute_matrix(
    case_path: Path,
    targets: Iterable[TargetConfig] | Mapping[str, TargetConfig],
    runs_root: Path,
    *,
    timeout_seconds: float | None = None,
    keep_workspace: bool = False,
    live: bool = True,
    max_concurrency: int | None = None,
) -> MatrixExecutionResult:
    """Run one case against multiple targets concurrently.

    Each target delegates to :func:`execute_case_async`, so it receives a
    fresh fixture copy and its own run directory.  ``max_concurrency`` limits
    active agent processes; by default all supplied targets may run at once.
    A target-level setup/serialization error is recorded in the returned
    result while the remaining targets continue.
    """

    return asyncio.run(
        execute_matrix_async(
            case_path,
            targets,
            runs_root,
            timeout_seconds=timeout_seconds,
            keep_workspace=keep_workspace,
            live=live,
            max_concurrency=max_concurrency,
        )
    )


async def execute_matrix_async(
    case_path: Path,
    targets: Iterable[TargetConfig] | Mapping[str, TargetConfig],
    runs_root: Path,
    *,
    timeout_seconds: float | None = None,
    keep_workspace: bool = False,
    live: bool = True,
    max_concurrency: int | None = None,
) -> MatrixExecutionResult:
    """Async counterpart to :func:`execute_matrix`.

    Results retain target input order even when processes finish in a
    different order.  Live events are printed by the existing runner with a
    target prefix (for example ``[codex-gpt][stdout] ...``).
    """

    target_list = _normalise_matrix_targets(targets)
    if max_concurrency is None:
        limit = max(len(target_list), 1)
    else:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer or None")
        limit = max_concurrency
    semaphore = asyncio.Semaphore(limit)

    async def run_one(target: TargetConfig) -> MatrixTargetResult:
        try:
            async with semaphore:
                run_dir = await execute_case_async(
                    case_path,
                    target,
                    runs_root,
                    timeout_seconds=timeout_seconds,
                    keep_workspace=keep_workspace,
                    live=live,
                    event_prefix=target.name,
                )
            result_path = run_dir / "run.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise RunError(f"run.json must contain an object: {result_path}")
            return MatrixTargetResult(target=target.name, run_dir=run_dir, result=result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return MatrixTargetResult(
                target=target.name,
                error=f"{type(exc).__name__}: {exc}",
            )

    outcomes = await asyncio.gather(*(run_one(target) for target in target_list))
    return MatrixExecutionResult(runs=tuple(outcomes))


def execute_suite(
    cases: Iterable[Path] | Mapping[str, Path] | Path | str,
    targets: Iterable[TargetConfig] | Mapping[str, TargetConfig],
    runs_root: Path,
    *,
    timeout_seconds: float | None = None,
    keep_workspace: bool = False,
    live: bool = True,
    max_concurrency: int | None = None,
) -> SuiteExecutionResult:
    """Run every case against every target in one globally bounded suite.

    Unlike calling :func:`execute_matrix` once per case, this function uses a
    single semaphore for the whole Cartesian product.  Thus
    ``max_concurrency=2`` means at most two agent processes are active across
    *all* cases, not two processes per case.  Every item still delegates to
    :func:`execute_case_async`, which creates its own fixture copy and run
    directory.
    """

    return asyncio.run(
        execute_suite_async(
            cases,
            targets,
            runs_root,
            timeout_seconds=timeout_seconds,
            keep_workspace=keep_workspace,
            live=live,
            max_concurrency=max_concurrency,
        )
    )


async def execute_suite_async(
    cases: Iterable[Path] | Mapping[str, Path] | Path | str,
    targets: Iterable[TargetConfig] | Mapping[str, TargetConfig],
    runs_root: Path,
    *,
    timeout_seconds: float | None = None,
    keep_workspace: bool = False,
    live: bool = True,
    max_concurrency: int | None = None,
) -> SuiteExecutionResult:
    """Async suite runner with stable case-major result ordering.

    A failed setup, adapter invocation, or malformed ``run.json`` is captured
    on that ``SuiteTargetResult`` only.  Other Cartesian-product items keep
    running.  ``asyncio.gather`` preserves task/input order, so callers can
    safely align rows with the supplied case and target sequences.
    """

    case_list = _normalise_suite_cases(cases)
    target_list = _normalise_matrix_targets(targets)
    limit = _validate_concurrency(max_concurrency, default=max(len(case_list) * len(target_list), 1))
    semaphore = asyncio.Semaphore(limit)
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    suite_id = f"suite-{uuid.uuid4().hex[:10]}"

    async def run_one(case_path: Path, case_label: str, target: TargetConfig) -> SuiteTargetResult:
        run_dir: Path | None = None
        try:
            # The semaphore spans the complete agent run (including fixture
            # setup and grader in execute_case_async), giving a predictable
            # global resource bound for a suite.
            async with semaphore:
                if not case_path.exists():
                    raise FileNotFoundError(f"Case path does not exist: {case_path}")
                maybe_run_dir = await execute_case_async(
                    case_path,
                    target,
                    runs_root,
                    timeout_seconds=timeout_seconds,
                    keep_workspace=keep_workspace,
                    live=live,
                    event_prefix=f"{case_label}/{target.name}",
                )
            run_dir = Path(maybe_run_dir)
            result_path = run_dir / "run.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise RunError(f"run.json must contain an object: {result_path}")
            return SuiteTargetResult(
                case=case_label,
                target=target.name,
                run_dir=run_dir,
                result=result,
                case_path=case_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return SuiteTargetResult(
                case=case_label,
                target=target.name,
                run_dir=run_dir,
                error=f"{type(exc).__name__}: {exc}",
                case_path=case_path,
            )

    # Build tasks in case-major/target-minor order.  gather returns in this
    # same order even if a later, faster target finishes first.
    tasks = [
        run_one(case_path, case_label, target)
        for case_path, case_label in case_list
        for target in target_list
    ]
    outcomes = await asyncio.gather(*tasks)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return SuiteExecutionResult(
        runs=tuple(outcomes),
        cases=tuple(case_label for _, case_label in case_list),
        targets=tuple(target.name for target in target_list),
        suite_id=suite_id,
        started_at=started_at,
        duration_ms=elapsed_ms,
    )


def _normalise_suite_cases(
    cases: Iterable[Path] | Mapping[str, Path] | Path | str,
) -> list[tuple[Path, str]]:
    """Normalize suite cases while retaining caller order and useful labels.

    A mapping's keys are treated as explicit labels.  For a plain path list,
    the case manifest ``id`` is preferred and the directory name is used as a
    safe fallback (including for missing/malformed manifests).  We resolve
    paths here only for stable metadata; fixture validation remains inside the
    per-item runner so one bad case is isolated from the rest.
    """

    if isinstance(cases, (str, Path)):
        raw_cases: list[Any] = [cases]
        explicit_labels: list[str | None] = [None]
    elif isinstance(cases, Mapping):
        raw_cases = list(cases.values())
        explicit_labels = [str(label) for label in cases.keys()]
    else:
        raw_cases = list(cases)
        explicit_labels = [None] * len(raw_cases)

    normalized: list[tuple[Path, str]] = []
    for raw_case, explicit_label in zip(raw_cases, explicit_labels):
        if not isinstance(raw_case, (str, Path)):
            raise TypeError("suite cases must be paths (or a mapping of labels to paths)")
        case_path = Path(raw_case)
        label = explicit_label if explicit_label is not None else _case_label(case_path)
        normalized.append((case_path, label))
    return normalized


def _case_label(case_path: Path) -> str:
    """Return a human-readable case id without making normalization fatal."""

    try:
        case = load_case(case_path)
        value = case.get("id")
        if value is not None and str(value).strip():
            return str(value)
    except Exception:
        # The actual run records the precise parse/fixture error.  A fallback
        # label lets sibling cases continue and keeps that error addressable.
        pass
    name = case_path.name
    return name or str(case_path)


def _validate_concurrency(value: int | None, *, default: int) -> int:
    if value is None:
        return max(default, 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_concurrency must be a positive integer or None")
    return value


def _normalise_matrix_targets(
    targets: Iterable[TargetConfig] | Mapping[str, TargetConfig],
) -> list[TargetConfig]:
    if isinstance(targets, Mapping):
        target_list = list(targets.values())
    else:
        target_list = list(targets)
    names: set[str] = set()
    for target in target_list:
        if not isinstance(target, TargetConfig):
            raise TypeError("matrix targets must be TargetConfig instances")
        if target.name in names:
            raise ValueError(f"duplicate matrix target: {target.name}")
        names.add(target.name)
    return target_list


def _print_live_event(event: dict[str, Any], target: str | None = None) -> None:
    summary = event.get("title") or event.get("summary") or event.get("tool") or event.get("kind")
    detail = event.get("detail") or event.get("command")
    channel = event.get("channel") or "event"
    if summary:
        prefix = f"[{target}]" if target else ""
        suffix = f" - {detail}" if detail and detail != summary else ""
        print(f"{prefix}[{channel}] {summary}{suffix}", flush=True)


def _read_prompt(path: Path) -> str:
    if not path.is_file():
        raise RunError(f"Instruction file does not exist: {path}")
    return path.read_text(encoding="utf-8")


def _remove_tree(path: Path) -> None:
    def onerror(function: Any, failed_path: str, exc_info: Any) -> None:
        try:
            Path(failed_path).chmod(stat.S_IWRITE | stat.S_IREAD)
            function(failed_path)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror, ignore_errors=False)


def _git_diff(workspace: Path) -> str:
    try:
        roots = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.splitlines()
        baseline = roots[0] if roots else "HEAD"
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", baseline, "--"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        diff = result.stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.splitlines()
        for relative in untracked:
            path = workspace / relative
            if path.is_file():
                diff += _untracked_diff(relative, path.read_bytes())
        return diff
    except OSError:
        return ""


def _untracked_diff(relative: str, content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return f"\n# Untracked binary file: {relative}\n"
    lines = text.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    if text and not text.endswith("\n"):
        body += "\\ No newline at end of file\n"
    return (
        f"diff --git a/{relative} b/{relative}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{relative}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        + body
    )


def _initialize_git(workspace: Path) -> None:
    commands = [
        ["git", "init", "-q"],
        ["git", "add", "--all"],
        ["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost", "commit", "-qm", "baseline"],
    ]
    for command in commands:
        completed = subprocess.run(command, cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            raise RunError(f"Could not initialize fixture git baseline: {' '.join(command)}\n{completed.stderr}")


async def _run_grader(case: dict[str, Any], workspace: Path, run_dir: Path, timeout: float) -> dict[str, Any]:
    grader = case.get("grader") or {}
    command = grader.get("command") if isinstance(grader, dict) else None
    if not command:
        return {"status": "not_configured", "score": None}
    if isinstance(command, str):
        command = [command]
    command = _resolve_grader_command(case["_case_dir"], [str(item) for item in command])
    process: asyncio.subprocess.Process | None = None
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
    try:
        kwargs: dict[str, Any] = {
            "cwd": str(workspace),
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*command, **kwargs)
        communicate_task = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await _terminate_grader_process_tree(process)
            stdout, stderr = await _drain_grader_output(communicate_task)
            _write_grader_logs(run_dir, stdout, stderr)
            return {"status": "timeout", "score": 0}

        _write_grader_logs(run_dir, stdout, stderr)
        return {
            "status": "passed" if process.returncode == 0 else "failed",
            "score": 100 if process.returncode == 0 else 0,
            "return_code": process.returncode,
        }
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_grader_process_tree(process)
        if communicate_task is not None:
            stdout, stderr = await _drain_grader_output(communicate_task)
            _write_grader_logs(run_dir, stdout, stderr)
        raise
    except asyncio.TimeoutError:
        return {"status": "timeout", "score": 0}
    except OSError as exc:
        if process is not None:
            await _terminate_grader_process_tree(process)
        return {"status": "error", "score": 0, "error": str(exc)}


def _write_grader_logs(run_dir: Path, stdout: bytes, stderr: bytes) -> None:
    (run_dir / "grader.stdout.log").write_text(
        stdout.decode("utf-8", errors="replace"),
        encoding="utf-8",
    )
    (run_dir / "grader.stderr.log").write_text(
        stderr.decode("utf-8", errors="replace"),
        encoding="utf-8",
    )


async def _drain_grader_output(
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
    *,
    timeout: float = 2.0,
) -> tuple[bytes, bytes]:
    """Collect captured grader output without making cancellation unbounded.

    A grader can accidentally leave a descendant holding stdout/stderr open
    after its own process has exited.  Waiting on ``communicate()`` forever in
    that situation would defeat the runner's stop guarantee, so cleanup has a
    short independent deadline and falls back to whatever output is available.
    """

    try:
        return await asyncio.wait_for(asyncio.shield(communicate_task), timeout=timeout)
    except asyncio.TimeoutError:
        if not communicate_task.done():
            communicate_task.cancel()
        try:
            result = await communicate_task
        except asyncio.CancelledError:
            return b"", b""
        except Exception:
            return b"", b""
        return result
    except asyncio.CancelledError:
        return b"", b""
    except Exception:
        # The process may have been reaped by a platform-specific tree-kill
        # before the pipe task observes EOF.  The primary run artifacts are
        # still written; missing partial grader output is preferable to
        # keeping the TUI blocked on a broken pipe forever.
        return b"", b""


async def _terminate_grader_process_tree(process: asyncio.subprocess.Process) -> None:
    pid = process.pid
    if pid is None:
        return

    terminated = False
    if os.name == "nt":
        try:
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(taskkill.wait(), timeout=5)
            terminated = taskkill.returncode == 0
        except (OSError, asyncio.TimeoutError):
            terminated = False
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
            terminated = True
        except (OSError, ProcessLookupError):
            terminated = False

    if not terminated and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass


def _resolve_grader_command(case_dir: Path, command: list[str]) -> list[str]:
    resolved: list[str] = []
    for index, token in enumerate(command):
        if index > 0 and token.endswith((".py", ".ps1", ".cmd", ".sh")):
            candidate = Path(token)
            if not candidate.is_absolute():
                resolved.append(str((case_dir / candidate).resolve()))
                continue
        resolved.append(token)
    return resolved


def _count_tool_calls(events: list[dict[str, Any]]) -> int | str:
    if not events:
        return "unknown"
    request_kinds = {"tool_call", "function_call", "command", "tool_use", "item/started"}
    return sum(1 for event in events if str(event["kind"]).lower() in request_kinds)


def _environment_fingerprint() -> str:
    values = [os.name, os.environ.get("COMPUTERNAME", ""), os.environ.get("PROCESSOR_ARCHITECTURE", "")]
    return hashlib.sha256("|".join(values).encode()).hexdigest()[:16]
