"""Interactive Textual front-end for running one case against CLI targets.

The UI deliberately keeps orchestration in :class:`TuiRunController`.  The
controller is usable without Textual, which makes the scheduling, cancellation
and target discovery behaviour straightforward to test.  Textual is imported
at module import time because ``bench tui`` is an explicitly interactive
command; the rest of the runner remains usable without the UI dependency.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from rich.markup import escape as escape_markup
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from .adapters.base import RunSpec, TargetConfig
from .adapters.registry import get_adapter
from .config import load_case, load_targets
from .runner import execute_case_async


CLI_ADAPTERS = frozenset({"claude-code", "codex", "opencode", "gemini"})


@dataclass(frozen=True)
class CaseInfo:
    """A case manifest discovered below the configured cases directory."""

    path: Path
    case_id: str
    title: str


@dataclass(frozen=True)
class TargetCheck:
    """Availability and command preview information for one target."""

    target: TargetConfig
    available: bool
    reason: str | None = None
    command: tuple[str, ...] = ()


def discover_cases(cases_dir: Path) -> list[CaseInfo]:
    """Return valid ``case.yaml`` manifests below *cases_dir*.

    Invalid manifests are skipped instead of preventing the selection screen
    from opening.  The manifest path is retained so the runner can use the
    exact case selected by the user.
    """

    root = Path(cases_dir).expanduser()
    if not root.exists():
        return []
    found: list[CaseInfo] = []
    for manifest in sorted(root.rglob("case.yaml")):
        if not manifest.is_file():
            continue
        try:
            payload = load_case(manifest)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            continue
        case_id = str(payload.get("id") or manifest.parent.name)
        title = str(payload.get("title") or payload.get("name") or case_id)
        found.append(CaseInfo(path=manifest, case_id=case_id, title=title))
    return found


def load_cli_targets(path: Path) -> dict[str, TargetConfig]:
    """Load only the four first-party CLI adapter targets.

    SDK targets remain valid in ``targets.yaml`` but are intentionally hidden
    from this first TUI because this screen is a process/harness monitor.
    """

    return {
        name: target
        for name, target in load_targets(Path(path).expanduser()).items()
        if target.adapter in CLI_ADAPTERS
    }


def _command_is_available(command: str) -> bool:
    """Check a command without invoking vendor-specific discovery tools."""

    if not command:
        return False
    path = Path(command).expanduser()
    if path.is_file():
        return True
    return shutil.which(command) is not None


def check_target_availability(target: TargetConfig) -> TargetCheck:
    """Check the executable and command prefix used by a CLI target."""

    # command_prefix is argv, not a list of executables. Only argv[0] needs
    # PATH resolution; later values may legitimately be flags such as ``-u``.
    if target.command_prefix and not _command_is_available(target.command_prefix[0]):
        return TargetCheck(target, False, f"command prefix not found: {target.command_prefix[0]}")
    if not target.executable:
        return TargetCheck(target, False, "target has no executable")
    if not _command_is_available(target.executable):
        return TargetCheck(target, False, f"executable not found: {target.executable}")

    # Build a command using a synthetic workspace.  This catches malformed
    # target options early and gives the preview screen the exact argv.
    try:
        adapter = get_adapter(target.adapter)
        spec = RunSpec(
            target=target,
            prompt="<task prompt>",
            workspace=Path("<workspace>"),
            timeout_seconds=300,
            run_dir=Path("<run>"),
        )
        command = tuple(str(part) for part in adapter.build_command(spec))
    except Exception as exc:  # malformed target should be selectable as disabled
        return TargetCheck(target, False, f"cannot build command: {exc}")
    return TargetCheck(target, True, command=command)


def format_command(command: Iterable[str]) -> str:
    """Render argv in a shell-friendly way for the preview screen."""

    values = [str(part) for part in command]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


@dataclass
class RunItem:
    """Mutable UI state for one target attempt."""

    target: TargetConfig
    attempt: int = 1
    status: str = "queued"
    task: asyncio.Task[Any] | None = None
    run_dir: Path | None = None
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    stop_requested: bool = False

    @property
    def key(self) -> str:
        return self.target.name

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.monotonic()
        return round((end - self.started_at) * 1000)

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running", "stopping"}

    def log_text(self, channel: str) -> str:
        filename = {"stdout": "stdout.log", "stderr": "stderr.log"}.get(channel)
        if filename is None:
            return ""
        # The runner flushes complete logs at finalization.  During a live run
        # the normalized event stream is still available, so use its raw line
        # as an immediate fallback for the detail pane.
        if self.run_dir is not None:
            try:
                text = (self.run_dir / filename).read_text(encoding="utf-8", errors="replace")
                if text:
                    return text
            except OSError:
                pass
        lines: list[str] = []
        for event in self.events:
            if event.get("channel") != channel:
                continue
            raw = event.get("raw")
            if isinstance(raw, str):
                lines.append(raw.rstrip("\r\n"))
            elif raw is not None:
                lines.append(json.dumps(raw, ensure_ascii=False, default=str))
            elif event.get("summary"):
                lines.append(str(event["summary"]))
        return "\n".join(lines)


UpdateListener = Callable[[RunItem], Any]


class TuiRunController:
    """Run selected targets concurrently and expose thread-safe state updates."""

    def __init__(
        self,
        case_path: Path,
        targets: Iterable[TargetConfig],
        runs_dir: Path,
        *,
        timeout_seconds: float | None = None,
        max_concurrency: int | None = None,
        keep_workspace: bool = True,
    ) -> None:
        self.case_path = Path(case_path)
        self.runs_dir = Path(runs_dir)
        self.timeout_seconds = timeout_seconds
        self.keep_workspace = keep_workspace
        target_list = list(targets)
        names = [target.name for target in target_list]
        if len(set(names)) != len(names):
            raise ValueError("target names must be unique")
        if max_concurrency is None:
            concurrency = len(target_list) or 1
        elif isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer or None")
        else:
            concurrency = max_concurrency
        self.max_concurrency = concurrency
        self.items: dict[str, RunItem] = {target.name: RunItem(target=target) for target in target_list}
        self.listeners: list[UpdateListener] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._group_task: asyncio.Task[Any] | None = None
        self._tasks: dict[tuple[str, int], asyncio.Task[Any]] = {}
        self._task_items: dict[tuple[str, int], RunItem] = {}
        # Keep attempt numbers independent from the currently visible item.
        # A completed attempt is replaced in ``items`` on rerun, so deriving
        # the number only from that mapping can otherwise repeat numbers after
        # callers retain an older RunItem reference.
        self._attempt_counters = {target.name: 1 for target in target_list}
        self._rerun_flights: dict[str, asyncio.Task[RunItem | None]] = {}
        self._rerun_locks = {target.name: asyncio.Lock() for target in target_list}
        # A stop invalidates any rerun transition that was already queued.  A
        # generation token closes the race where ``stop`` observes no flight,
        # then a concurrently scheduled rerun creates its replacement after
        # the stop lock is released.
        self._stop_generations = {target.name: 0 for target in target_list}
        self._stopping_all = False
        self._semaphore: asyncio.Semaphore | None = None
        self.group_id = f"tui-{uuid.uuid4().hex[:10]}"

    def subscribe(self, listener: UpdateListener) -> None:
        self.listeners.append(listener)

    def get(self, target_name: str) -> RunItem | None:
        return self.items.get(target_name)

    @property
    def running(self) -> bool:
        return any(item.active for item in self.items.values()) or any(
            not flight.done() for flight in self._rerun_flights.values()
        )

    @property
    def done(self) -> bool:
        return bool(self.items) and all(not item.active for item in self.items.values()) and not any(
            not flight.done() for flight in self._rerun_flights.values()
        )

    def summary(self) -> dict[str, int]:
        statuses = [item.status for item in self.items.values()]
        return {
            "total": len(statuses),
            "queued": statuses.count("queued"),
            "running": statuses.count("running"),
            "passed": statuses.count("passed"),
            "completed": statuses.count("completed"),
            "stopped": statuses.count("stopped"),
            "timeout": statuses.count("timeout"),
            "failed": sum(status in {"agent_error", "process_error", "grader_failed", "error"} for status in statuses),
        }

    def start_background(self) -> asyncio.Task[Any]:
        if self._group_task is not None:
            return self._group_task
        self._loop = asyncio.get_running_loop()
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        tasks = [self._start_item(item) for item in self.items.values()]
        self._group_task = asyncio.create_task(self._wait_for_tasks(tasks), name=self.group_id)
        return self._group_task

    async def wait(self) -> None:
        # Reruns are dynamically added after the initial group task has
        # completed.  Keep observing the task registry until no transition or
        # attempt remains active, so callers get a true quiescence guarantee.
        if self._group_task is None and not self._tasks:
            return
        while True:
            if self._group_task is not None:
                await asyncio.shield(self._group_task)
            flights = [flight for flight in self._rerun_flights.values() if not flight.done()]
            tasks = [task for task in self._tasks.values() if not task.done()]
            if not flights and not tasks and not any(item.active for item in self.items.values()):
                return
            pending = [*flights, *tasks]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _wait_for_tasks(self, tasks: list[asyncio.Task[Any]]) -> None:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _start_item(self, item: RunItem) -> asyncio.Task[Any]:
        key = (item.key, item.attempt)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            raise RuntimeError(f"attempt already running: {item.key} #{item.attempt}")
        task = asyncio.create_task(self._run_item(item), name=f"bench:{item.key}:{item.attempt}")
        item.task = task
        self._tasks[key] = task
        self._task_items[key] = item
        return task

    async def _run_item(self, item: RunItem) -> None:
        assert self._semaphore is not None
        try:
            async with self._semaphore:
                stopped_before_start = item.stop_requested
                item.status = "stopping" if stopped_before_start else "running"
                item.started_at = time.monotonic()
                self._notify(item)

                def event_callback(event: Any) -> None:
                    payload = _event_payload(event)
                    self._dispatch_threadsafe(self._record_event, item, payload)

                def lifecycle_callback(event: Any) -> None:
                    self._dispatch_threadsafe(self._apply_lifecycle, item, event)

                kwargs: dict[str, Any] = {
                    "timeout_seconds": self.timeout_seconds,
                    "keep_workspace": self.keep_workspace,
                    "live": False,
                }
                parameters = inspect.signature(execute_case_async).parameters
                accepts_arbitrary_keywords = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                if "event_callback" in parameters or accepts_arbitrary_keywords:
                    kwargs["event_callback"] = event_callback
                if "lifecycle_callback" in parameters or accepts_arbitrary_keywords:
                    kwargs["lifecycle_callback"] = lifecycle_callback
                if "cancel_before_agent" in parameters or accepts_arbitrary_keywords:
                    kwargs["cancel_before_agent"] = stopped_before_start
                try:
                    run_dir = await execute_case_async(self.case_path, item.target, self.runs_dir, **kwargs)
                except TypeError as exc:
                    # A third-party runner wrapper may expose **kwargs but
                    # reject the lifecycle hooks.  Keep the UI compatible
                    # with the pre-hook runner instead of failing the run.
                    if not any(
                        name in str(exc)
                        for name in ("event_callback", "lifecycle_callback", "cancel_before_agent")
                    ):
                        raise
                    kwargs.pop("event_callback", None)
                    kwargs.pop("lifecycle_callback", None)
                    kwargs.pop("cancel_before_agent", None)
                    run_dir = await execute_case_async(self.case_path, item.target, self.runs_dir, **kwargs)
                item.run_dir = Path(run_dir)
                result_path = item.run_dir / "run.json"
                if result_path.is_file():
                    try:
                        item.result = json.loads(result_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as exc:
                        item.error = f"invalid run.json: {exc}"
                result_error = (item.result or {}).get("error")
                if result_error:
                    item.error = str(result_error)
                result_status = str((item.result or {}).get("status") or "completed")
                item.status = "stopped" if item.stop_requested else result_status
        except asyncio.CancelledError:
            item.status = "stopped" if item.stop_requested else "cancelled"
            raise
        except Exception as exc:
            item.status = "process_error"
            item.error = f"{type(exc).__name__}: {exc}"
        finally:
            item.finished_at = time.monotonic()
            self._notify(item)

    def _apply_lifecycle(self, item: RunItem, event: Any) -> None:
        phase = _event_field(event, "phase", "kind", "status")
        run_dir = _event_field(event, "run_dir", "run_path")
        if run_dir:
            item.run_dir = Path(str(run_dir))
        result = _event_field(event, "result")
        if isinstance(result, Mapping):
            item.result = {str(key): value for key, value in result.items()}
        error = _event_field(event, "error") or (item.result or {}).get("error")
        if error:
            item.error = str(error)
        if phase in {"prepared", "started", "running"} and item.status == "queued":
            item.status = "running"
            item.started_at = item.started_at or time.monotonic()
        self._notify(item)

    def _record_event(self, item: RunItem, payload: dict[str, Any]) -> None:
        item.events.append(payload)
        self._notify(item)

    def _dispatch_threadsafe(self, callback: Callable[..., Any], *args: Any) -> None:
        if self._loop is None:
            callback(*args)
            return
        # Once the Textual loop is shutting down, a late SDK/reader callback
        # must be dropped rather than mutating controller/UI state from a
        # worker thread.
        if self._loop.is_closed() or not self._loop.is_running():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            callback(*args)
        else:
            try:
                self._loop.call_soon_threadsafe(callback, *args)
            except RuntimeError:
                # The loop may close between the checks above and this call.
                pass

    def _notify(self, item: RunItem) -> None:
        for listener in tuple(self.listeners):
            try:
                result = listener(item)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception:
                # UI listeners are observers; one broken panel must not abort
                # an agent run or prevent its artifacts from being written.
                continue

    async def stop_all(self) -> None:
        # A rerun is a short-lived orchestration task which may be between
        # cancelling the previous attempt and creating the replacement. Sweep
        # every attempt first (to release semaphore slots), then drain those
        # flights. The second sweep closes the race where a replacement is
        # created just as the first sweep starts.
        self._stopping_all = True
        try:
            for name in self._rerun_locks:
                self._stop_generations[name] += 1
            for _ in range(2):
                active_items = {
                    id(item): item
                    for key, item in self._task_items.items()
                    if not self._tasks[key].done()
                }
                active_items.update({id(item): item for item in self.items.values() if item.active})
                if not active_items:
                    break
                await asyncio.gather(
                    *(self._stop_item(item) for item in active_items.values()), return_exceptions=True
                )
                # A rerun flight can itself be waiting for a queued previous
                # attempt.  We stop attempts first (which releases the
                # semaphore), then wait for those orchestration flights.  If
                # we awaited flights before this sweep, a queued rerun could
                # wait forever while the running attempt holding the only
                # semaphore slot remained active.
                flights = [flight for flight in self._rerun_flights.values() if not flight.done()]
                if flights:
                    await asyncio.gather(*flights, return_exceptions=True)
                    flights = []
            # In the no-active-items case, still drain any flight that was
            # created just before the sweep. Generation invalidation prevents
            # it from creating a replacement attempt.
            flights = [flight for flight in self._rerun_flights.values() if not flight.done()]
            if flights:
                await asyncio.gather(*flights, return_exceptions=True)
        finally:
            self._stopping_all = False

    async def rerun(self, target_name: str) -> RunItem | None:
        if self._stopping_all:
            return None
        existing = self._rerun_flights.get(target_name)
        if existing is not None:
            try:
                return await asyncio.shield(existing)
            except asyncio.CancelledError:
                # ``stop``/``stop_all`` may deliberately cancel a queued
                # rerun flight.  Treat that as a declined rerun for the UI;
                # propagate cancellation only when the caller itself was
                # cancelled (the shielded flight is still alive then).
                if existing.cancelled():
                    return None
                raise
        if target_name not in self.items:
            return None
        generation = self._stop_generations.get(target_name, 0)
        flight = asyncio.create_task(
            self._perform_rerun(target_name, generation),
            name=f"bench:rerun:{target_name}",
        )
        self._rerun_flights[target_name] = flight
        # Keep the flight registered until its newly-created attempt finishes.
        # A burst of identical key presses therefore observes the same attempt
        # instead of repeatedly cancelling and replacing live CLI processes.
        flight.add_done_callback(lambda done, name=target_name: self._hold_rerun_until_attempt_done(name, done))
        try:
            return await asyncio.shield(flight)
        except asyncio.CancelledError:
            if flight.cancelled():
                return None
            raise

    async def _perform_rerun(self, target_name: str, generation: int) -> RunItem | None:
        async with self._rerun_locks[target_name]:
            if self._stopping_all or generation != self._stop_generations.get(target_name, 0):
                return None
            previous = self.items.get(target_name)
            if previous is None:
                return None
            if previous.active:
                await self._stop_item(previous)
            if self._stopping_all or generation != self._stop_generations.get(target_name, 0):
                return None
            attempt = max(self._attempt_counters.get(target_name, 0), previous.attempt) + 1
            self._attempt_counters[target_name] = attempt
            item = RunItem(target=previous.target, attempt=attempt)
            self.items[target_name] = item
            if self._semaphore is None:
                self._loop = asyncio.get_running_loop()
                self._semaphore = asyncio.Semaphore(self.max_concurrency)
            self._start_item(item)
            return item

    def _hold_rerun_until_attempt_done(
        self, target_name: str, flight: asyncio.Task[RunItem | None]
    ) -> None:
        if self._rerun_flights.get(target_name) is not flight:
            return
        try:
            item = flight.result()
        except BaseException:
            self._rerun_flights.pop(target_name, None)
            return
        if item is None or item.task is None:
            self._rerun_flights.pop(target_name, None)
            return

        def release(_done: asyncio.Task[Any]) -> None:
            if self._rerun_flights.get(target_name) is flight:
                self._rerun_flights.pop(target_name, None)

        item.task.add_done_callback(release)

    async def _stop_item(self, item: RunItem) -> None:
        if not item.active and (item.task is None or item.task.done()):
            return
        was_queued = item.started_at is None and item.status in {"queued", "stopping"}
        item.stop_requested = True
        item.status = "stopping"
        self._notify(item)
        task = item.task
        if task is not None and not task.done():
            # Let a queued task acquire the semaphore and execute the runner's
            # pre-start cancellation path. That path materializes the normal
            # stopped artifacts without ever launching the CLI. Running tasks
            # are cancelled so their adapter can terminate the process tree.
            if not was_queued:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if item.status == "stopping":
            item.status = "stopped"
            item.finished_at = time.monotonic()
            self._notify(item)

    async def stop(self, target_name: str) -> None:
        lock = self._rerun_locks.get(target_name)
        if lock is None:
            return
        # Invalidate a rerun transition before acquiring its lock.  The rerun
        # checks this generation after cancelling the old attempt and will
        # skip creating a replacement when this stop won the race.
        self._stop_generations[target_name] += 1
        flight = self._rerun_flights.get(target_name)
        if flight is not None and not flight.done():
            # Let the transition finish its serialized state change.  Once it
            # returns, the current item is either the old stopped attempt or
            # the new replacement, both of which are owned by ``items``.
            await asyncio.gather(flight, return_exceptions=True)
        # Serialize stop with the rerun state transition.  Without this lock,
        # a stop arriving between ``_perform_rerun``'s old-item cancellation
        # and replacement creation could stop the old item while leaving the
        # new CLI task running with no owner.
        async with lock:
            item = self.items.get(target_name)
            if item is None:
                return
            await self._stop_item(item)


def _event_field(event: Any, *names: str) -> Any:
    if isinstance(event, Mapping):
        for name in names:
            if name in event:
                return event[name]
    for name in names:
        value = getattr(event, name, None)
        if value is not None:
            return value
    return None


def _event_payload(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        return {str(key): value for key, value in event.items()}
    names = ("seq", "timestamp", "target", "kind", "source", "channel", "tool", "summary", "raw", "data")
    return {name: value for name in names if (value := getattr(event, name, None)) is not None}


class ConfigSelectScreen(Screen[None]):
    """Choose target/case/run directories before loading the selection view."""

    CSS = """
    ConfigSelectScreen { align: center middle; }
    #config-panel { width: 72; height: auto; border: round $accent; padding: 1 2; }
    #config-panel Input { margin: 1 0; }
    #config-error { color: $error; height: auto; }
    """

    def __init__(self, app: "BenchTuiApp") -> None:
        super().__init__()
        self.bench_app = app

    def compose(self) -> ComposeResult:
        with Vertical(id="config-panel"):
            yield Label("Agent benchmark", id="config-title", markup=False)
            yield Label("Target configuration", markup=False)
            yield Input(str(self.bench_app.targets_file), id="targets-path")
            yield Static(self._target_file_hint(), id="targets-hint", markup=False)
            yield Label("Cases directory", markup=False)
            yield Input(str(self.bench_app.cases_dir), id="cases-path")
            yield Label("Runs directory", markup=False)
            yield Input(str(self.bench_app.runs_dir), id="runs-path")
            yield Static("", id="config-error", markup=False)
            yield Button("Continue", id="config-continue", variant="primary")

    def _target_file_hint(self) -> str:
        if self.bench_app.targets_file.exists():
            return ""
        candidates = sorted(
            path.name
            for path in self.bench_app.targets_file.parent.glob("targets*.y*ml")
            if path.is_file()
        )
        if candidates:
            return "Not found. Available files: " + ", ".join(candidates)
        return "File not found. Enter a target YAML path."

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "config-continue":
            return
        try:
            targets_file = Path(self.query_one("#targets-path", Input).value).expanduser()
            cases_dir = Path(self.query_one("#cases-path", Input).value).expanduser()
            runs_dir = Path(self.query_one("#runs-path", Input).value).expanduser()
            targets = load_cli_targets(targets_file)
            cases = discover_cases(cases_dir)
            if not targets:
                raise ValueError("no CLI targets found (supported adapters: claude-code, codex, opencode, gemini)")
            if not cases:
                raise ValueError(f"no case.yaml found below {cases_dir}")
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            self.query_one("#config-error", Static).update(str(exc))
            return
        self.bench_app.targets_file = targets_file
        self.bench_app.cases_dir = cases_dir
        self.bench_app.runs_dir = runs_dir
        self.bench_app.targets = targets
        self.bench_app.case_infos = cases
        self.app.push_screen(SelectionScreen(self.bench_app))


class SelectionScreen(Screen[None]):
    CSS = """
    SelectionScreen { layout: vertical; }
    #selection-body { height: 1fr; }
    #case-panel, #target-panel { width: 1fr; border: round $panel; padding: 1; overflow: auto; }
    .section-title { text-style: bold; margin-bottom: 1; }
    #selection-error { color: $error; height: auto; }
    #selection-actions { height: auto; align: right middle; }
    """

    def __init__(self, app: "BenchTuiApp") -> None:
        super().__init__()
        self.bench_app = app
        self.checks: dict[str, TargetCheck] = {}
        self.target_widget_ids: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="selection-body"):
            with Vertical(id="case-panel"):
                yield Label("Choose one case", classes="section-title", markup=False)
                with ListView(id="case-list"):
                    for index, case in enumerate(self.bench_app.case_infos):
                        yield ListItem(
                            Label(f"{case.case_id}  {case.title}", markup=False),
                            id=f"case-{index}",
                        )
            with Vertical(id="target-panel"):
                yield Label("Choose one or more CLI targets", classes="section-title", markup=False)
                used_ids: set[str] = set()
                for name, target in self.bench_app.targets.items():
                    check = check_target_availability(target)
                    self.checks[name] = check
                    base_id = f"target-{_slug(name)}"
                    widget_id = base_id
                    suffix = 2
                    while widget_id in used_ids:
                        widget_id = f"{base_id}-{suffix}"
                        suffix += 1
                    used_ids.add(widget_id)
                    self.target_widget_ids[name] = widget_id
                    label = f"{name} | {target.adapter} | {target.model or 'default'}"
                    if not check.available:
                        label += f" (disabled: {check.reason})"
                    # Textual's Checkbox constructor has no ``markup`` flag
                    # (including the 0.47 compatibility floor).  Escape
                    # user/config supplied text before Rich parses it.
                    yield Checkbox(escape_markup(label), id=widget_id, disabled=not check.available)
        yield Static("", id="selection-error", markup=False)
        with Horizontal(id="selection-actions"):
            yield Button("Back", id="selection-back")
            yield Button("Preview", id="selection-preview", variant="primary")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # ListView is used as a keyboard-friendly case radio list.  Remove
        # selection styling from sibling items so exactly one case is active.
        for item in self.query("#case-list ListItem"):
            item.remove_class("selected")
        event.item.add_class("selected")

    def selected_case(self) -> CaseInfo | None:
        for index, case in enumerate(self.bench_app.case_infos):
            item = self.query_one(f"#case-{index}", ListItem)
            if item.has_class("selected"):
                return case
        return self.bench_app.case_infos[0] if len(self.bench_app.case_infos) == 1 else None

    def selected_targets(self) -> list[TargetConfig]:
        selected: list[TargetConfig] = []
        for name, target in self.bench_app.targets.items():
            checkbox = self.query_one(f"#{self.target_widget_ids[name]}", Checkbox)
            if checkbox.value and self.checks[name].available:
                selected.append(target)
        return selected

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "selection-back":
            self.app.pop_screen()
            return
        if event.button.id != "selection-preview":
            return
        case = self.selected_case()
        targets = self.selected_targets()
        if case is None:
            self.query_one("#selection-error", Static).update("Select one case first")
            return
        if not targets:
            self.query_one("#selection-error", Static).update("Select at least one available CLI target")
            return
        self.app.push_screen(PreviewScreen(self.bench_app, case, targets))


class PreviewScreen(Screen[None]):
    CSS = """
    PreviewScreen { align: center middle; }
    #preview-panel { width: 90%; height: 90%; border: round $accent; padding: 1 2; }
    #preview-text { height: 1fr; overflow: auto; }
    #preview-actions { height: auto; align: right middle; }
    #preview-error { color: $error; height: auto; }
    """

    BINDINGS = [Binding("enter", "start_run", "Start"), Binding("escape", "go_back", "Back")]

    def __init__(self, app: "BenchTuiApp", case: CaseInfo, targets: list[TargetConfig]) -> None:
        super().__init__()
        self.bench_app = app
        self.case = case
        self.targets = targets

    def compose(self) -> ComposeResult:
        with Vertical(id="preview-panel"):
            yield Label("Run preview", classes="section-title", markup=False)
            yield Static(self._preview(), id="preview-text", markup=False)
            yield Label("Maximum concurrent CLIs (1..selected count)", markup=False)
            yield Input(str(self._initial_concurrency()), id="preview-concurrency", type="integer")
            yield Static("", id="preview-error", markup=False)
            with Horizontal(id="preview-actions"):
                yield Button("Back", id="preview-back")
                yield Button("Start", id="preview-start", variant="primary")

    def _preview(self) -> str:
        lines = [
            f"Case: {self.case.case_id} ({self.case.path})",
            f"Targets: {len(self.targets)}",
            f"Workspace: independent copy per target; keep={self.bench_app.keep_workspace}",
            f"Timeout: {self.bench_app.timeout_seconds or 'case default'} seconds",
            "",
        ]
        for target in self.targets:
            check = check_target_availability(target)
            command = format_command(check.command) if check.command else f"{target.executable} ..."
            lines.append(f"[{target.name}] model={target.model or 'default'} adapter={target.adapter}")
            lines.append(f"  {command}")
        lines += ["", "Press Enter or Start to confirm. Real CLI processes begin only after confirmation."]
        return "\n".join(lines)

    def _initial_concurrency(self) -> int:
        return max(1, min(self.bench_app.max_concurrency or len(self.targets), len(self.targets)))

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_start_run(self) -> None:
        self._start()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "preview-concurrency":
            self._start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "preview-back":
            self.app.pop_screen()
        elif event.button.id == "preview-start":
            self._start()

    def _start(self) -> None:
        if self.app.screen is not self:
            return
        try:
            concurrency = int(self.query_one("#preview-concurrency", Input).value.strip())
        except (TypeError, ValueError):
            self.query_one("#preview-error", Static).update("Concurrency must be a positive integer")
            return
        if not 1 <= concurrency <= len(self.targets):
            self.query_one("#preview-error", Static).update(
                f"Concurrency must be between 1 and {len(self.targets)}"
            )
            return
        self.bench_app.max_concurrency = concurrency
        controller = TuiRunController(
            self.case.path,
            self.targets,
            self.bench_app.runs_dir,
            timeout_seconds=self.bench_app.timeout_seconds,
            max_concurrency=self.bench_app.max_concurrency or len(self.targets),
            keep_workspace=self.bench_app.keep_workspace,
        )
        self.bench_app.controller = controller
        self.app.push_screen(RunScreen(self.bench_app, controller, self.case))


class ConfirmExitScreen(ModalScreen[str]):
    CSS = """
    ConfirmExitScreen { align: center middle; }
    #confirm-box { width: 60; height: auto; border: round $warning; padding: 1 2; }
    #confirm-box Button { margin: 1 1 0 0; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("Agents are still running. Stop all and exit?", markup=False)
            with Horizontal():
                yield Button("Stop all and exit", id="confirm-stop", variant="error")
                yield Button("Continue running", id="confirm-continue")
                yield Button("Cancel", id="confirm-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choice = {
            "confirm-stop": "stop",
            "confirm-continue": "continue",
            "confirm-cancel": "cancel",
        }.get(event.button.id, "cancel")
        self.dismiss(choice)


class RunScreen(Screen[None]):
    BINDINGS = [
        Binding("s", "stop_selected", "Stop"),
        Binding("S", "stop_all", "Stop all"),
        Binding("r", "rerun_selected", "Rerun"),
        Binding("o", "open_workspace", "Workspace"),
        Binding("d", "open_diff", "Diff"),
        Binding("f", "cycle_view", "View"),
        Binding("q", "quit_run", "Quit"),
    ]
    CSS = """
    RunScreen { layout: vertical; }
    #run-body { height: 1fr; }
    #run-sidebar { width: 30; border: round $panel; }
    #run-detail { width: 1fr; border: round $panel; padding: 1; overflow: auto; }
    #run-status { height: auto; padding: 0 1; }
    """

    def __init__(self, app: "BenchTuiApp", controller: TuiRunController, case: CaseInfo) -> None:
        super().__init__()
        self.bench_app = app
        self.controller = controller
        self.case = case
        self.selected_key = "__overview__"
        self.view = "events"
        self._sidebar_keys: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="run-body"):
            with Vertical(id="run-sidebar"):
                yield Label("Run group", classes="section-title", markup=False)
                with ListView(id="run-list"):
                    yield ListItem(Label("Overview", markup=False), id="side-overview")
                    for index, target in enumerate(self.controller.items.values()):
                        self._sidebar_keys[f"side-{index}"] = target.key
                        yield ListItem(
                            Label(self._sidebar_label(target), markup=False),
                            id=f"side-{index}",
                        )
            with Vertical(id="run-detail"):
                yield Static("", id="run-status", markup=False)
                yield Static("", id="run-content", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.controller.subscribe(self._on_controller_update)
        self.controller.start_background()
        self.set_interval(0.5, self._refresh_view)
        self._refresh_view()

    def _on_controller_update(self, item: RunItem) -> None:
        # Controller notifications are scheduled onto the Textual loop even
        # when a synchronous adapter emits events from a worker thread.
        self.call_after_refresh(self._refresh_item, item)

    def _refresh_item(self, item: RunItem) -> None:
        for widget_id, key in self._sidebar_keys.items():
            if key == item.key:
                self.query_one(f"#{widget_id} Label", Label).update(self._sidebar_label(item))
                break
        self._refresh_view()

    def _sidebar_label(self, item: RunItem) -> str:
        duration = f" {item.duration_ms}ms" if item.duration_ms is not None else ""
        return f"{item.target.name}  [{item.status}]{duration}"

    def _refresh_view(self) -> None:
        summary = self.controller.summary()
        self.query_one("#run-status", Static).update(
            f"Case {self.case.case_id} | total {summary['total']} | queued {summary['queued']} "
            f"| running {summary['running']} | passed {summary['passed']} | completed {summary['completed']} "
            f"| failed {summary['failed']} | timeout {summary['timeout']} | stopped {summary['stopped']}"
        )
        content = self.query_one("#run-content", Static)
        if self.selected_key == "__overview__":
            lines = [f"Run group: {self.controller.group_id}", f"Runs: {self.bench_app.runs_dir}", "", "Timeline:"]
            for item in self.controller.items.values():
                line = f"{item.target.name}: {item.status} ({item.duration_ms or 0}ms)"
                if item.error:
                    line += f" | error: {item.error}"
                lines.append(line)
            timeline = [
                (str(event.get("timestamp") or ""), item.target.name, event)
                for item in self.controller.items.values()
                for event in item.events
            ]
            for _, target_name, event in sorted(timeline, key=lambda entry: entry[0])[-50:]:
                detail = event.get("summary") or event.get("tool") or ""
                lines.append(f"  [{target_name}] {event.get('kind', 'event')}: {detail}")
            content.update("\n".join(lines))
            return
        item = self.controller.get(self.selected_key)
        if item is None:
            content.update("No target selected")
            return
        if self.view == "events":
            lines = [f"{item.target.name} | attempt {item.attempt} | status {item.status}", ""]
            if item.error:
                lines.append(f"Error: {item.error}")
                lines.append("")
            if item.result and item.result.get("error") and not item.error:
                lines.append(f"Error: {item.result['error']}")
                lines.append("")
            lines += [json.dumps(event, ensure_ascii=False, default=str) for event in item.events[-100:]]
            content.update("\n".join(lines) or "No events yet")
        elif self.view in {"stdout", "stderr"}:
            body = item.log_text(self.view) or f"No {self.view} output yet"
            error_line = f"\nError: {item.error}" if item.error else ""
            content.update(
                f"{item.target.name} | attempt {item.attempt} | status {item.status} | view {self.view}"
                f"{error_line}\n\n{body}"
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = self._sidebar_keys.get(event.item.id or "")
        self.selected_key = key or "__overview__"
        self._refresh_view()

    def action_stop_selected(self) -> None:
        if self.selected_key == "__overview__":
            # Lowercase ``s`` is scoped to the selected CLI.  On the overview
            # there is no selected process, so leave all agents untouched.
            return
        else:
            self.run_worker(self.controller.stop(self.selected_key), exclusive=False)

    def action_stop_all(self) -> None:
        self.run_worker(self.controller.stop_all(), exclusive=False)

    def action_rerun_selected(self) -> None:
        if self.selected_key != "__overview__":
            self.run_worker(self.controller.rerun(self.selected_key), exclusive=False)

    def action_cycle_view(self) -> None:
        self.view = {"events": "stdout", "stdout": "stderr", "stderr": "events"}[self.view]
        self._refresh_view()

    def _open_path(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif shutil.which("open"):
                subprocess.Popen(["open", str(path)])
            elif shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            pass

    def action_open_workspace(self) -> None:
        item = self.controller.get(self.selected_key)
        self._open_path(item.run_dir / "workspace" if item and item.run_dir else None)

    def action_open_diff(self) -> None:
        item = self.controller.get(self.selected_key)
        self._open_path(item.run_dir / "patch.diff" if item and item.run_dir else None)

    def action_quit_run(self) -> None:
        if not self.controller.running:
            self.app.exit()
            return

        def handle(choice: str | None) -> None:
            if choice == "stop":
                self.run_worker(self._stop_and_exit(), exclusive=True)

        self.app.push_screen(ConfirmExitScreen(), handle)

    async def _stop_and_exit(self) -> None:
        await self.controller.stop_all()
        self.app.exit()


class BenchTuiApp(App[None]):
    """Textual application object used by :func:`run_tui`."""

    TITLE = "Agent Benchmark"
    CSS = """
    Screen { background: $surface; }
    .section-title { text-style: bold; }
    """

    def __init__(
        self,
        *,
        targets_file: Path = Path("targets.yaml"),
        cases_dir: Path = Path("cases"),
        runs_dir: Path = Path("runs"),
        timeout_seconds: float | None = None,
        max_concurrency: int | None = None,
        keep_workspace: bool = True,
    ) -> None:
        super().__init__()
        self.targets_file = Path(targets_file)
        self.cases_dir = Path(cases_dir)
        self.runs_dir = Path(runs_dir)
        self.timeout_seconds = timeout_seconds
        self.max_concurrency = max_concurrency
        self.keep_workspace = keep_workspace
        self.targets: dict[str, TargetConfig] = {}
        self.case_infos: list[CaseInfo] = []
        self.controller: TuiRunController | None = None

    def on_mount(self) -> None:
        self.push_screen(ConfigSelectScreen(self))


def run_tui(
    *,
    targets_file: Path = Path("targets.yaml"),
    cases_dir: Path = Path("cases"),
    runs_dir: Path = Path("runs"),
    timeout_seconds: float | None = None,
    max_concurrency: int | None = None,
    keep_workspace: bool = True,
) -> int:
    """Start the interactive app.  The function is intentionally sync like
    the rest of the ``bench`` entrypoint and returns when the app exits."""

    BenchTuiApp(
        targets_file=targets_file,
        cases_dir=cases_dir,
        runs_dir=runs_dir,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        keep_workspace=keep_workspace,
    ).run()
    return 0


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-").lower() or "target"


__all__ = [
    "BenchTuiApp",
    "CaseInfo",
    "ConfigSelectScreen",
    "PreviewScreen",
    "RunItem",
    "RunScreen",
    "SelectionScreen",
    "TargetCheck",
    "TuiRunController",
    "check_target_availability",
    "discover_cases",
    "format_command",
    "load_cli_targets",
    "run_tui",
]
