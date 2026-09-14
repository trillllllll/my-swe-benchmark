from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import AsyncMock

from bench.tui import (
    BenchTuiApp,
    CaseInfo,
    RunItem,
    RunScreen,
    TuiRunController,
    PreviewScreen,
    check_target_availability,
    discover_cases,
    load_cli_targets,
)
from bench.runner import RunLifecycle


ROOT = Path(__file__).resolve().parents[1]


def fake_target(name: str, mode: str, **environment: str):
    base = load_cli_targets(ROOT / "targets.example.yaml")["codex-gpt"]
    return replace(
        base,
        name=name,
        env={"BENCH_FAKE_MODE": mode, **environment},
    )


def test_discover_cases_only_reads_case_manifests() -> None:
    cases = discover_cases(ROOT / "cases")
    assert [case.case_id for case in cases] == ["py-bug-001", "ts-bug-001"]


def test_cli_target_filter_and_preflight() -> None:
    targets = load_cli_targets(ROOT / "targets.example.yaml")
    assert set(targets) == {"claude-sonnet", "codex-gpt", "opencode-qwen", "gemini-pro"}
    assert all(check_target_availability(target).available for target in targets.values())


def test_missing_executable_is_disabled() -> None:
    targets = load_cli_targets(ROOT / "targets.example.yaml")
    target = targets["claude-sonnet"]
    missing = target.__class__(
        name=target.name,
        adapter=target.adapter,
        executable="definitely-not-a-real-agent-command",
    )
    result = check_target_availability(missing)
    assert not result.available
    assert "not found" in (result.reason or "")


def test_missing_command_prefix_is_disabled_before_script_check() -> None:
    target = load_cli_targets(ROOT / "targets.example.yaml")["claude-sonnet"]
    result = check_target_availability(
        replace(target, command_prefix=("definitely-not-a-real-python-command",))
    )
    assert not result.available
    assert result.reason == "command prefix not found: definitely-not-a-real-python-command"


def test_controller_runs_targets_in_parallel_and_keeps_artifacts(tmp_path: Path) -> None:
    targets = load_cli_targets(ROOT / "targets.example.yaml")
    selected = [targets["claude-sonnet"], targets["codex-gpt"]]

    async def run() -> TuiRunController:
        controller = TuiRunController(
            ROOT / "cases" / "py-bug-001",
            selected,
            tmp_path / "runs",
            keep_workspace=True,
        )
        controller.start_background()
        await controller.wait()
        return controller

    controller = asyncio.run(run())
    assert controller.done
    assert all(item.status == "passed" for item in controller.items.values())
    assert all(item.run_dir and (item.run_dir / "workspace").is_dir() for item in controller.items.values())
    assert len({item.run_dir for item in controller.items.values()}) == 2


def test_controller_stop_cancels_only_selected_target(monkeypatch, tmp_path: Path) -> None:
    targets = load_cli_targets(ROOT / "targets.example.yaml")
    selected = [targets["claude-sonnet"], targets["codex-gpt"]]
    calls: list[str] = []

    async def fake_execute(case_path, target, runs_dir, **kwargs):
        calls.append(target.name)
        await asyncio.sleep(60)

    monkeypatch.setattr("bench.tui.execute_case_async", fake_execute)

    async def run() -> tuple[TuiRunController, str]:
        controller = TuiRunController(ROOT / "cases" / "py-bug-001", selected, tmp_path / "runs")
        controller.start_background()
        await asyncio.sleep(0)
        await controller.stop("claude-sonnet")
        first_status = controller.get("claude-sonnet").status
        assert controller.get("codex-gpt").status == "running"
        await controller.stop_all()
        return controller, first_status

    controller, first_status = asyncio.run(run())
    assert first_status == "stopped"
    assert controller.get("codex-gpt").status == "stopped"
    assert "codex-gpt" in calls


def test_controller_rerun_creates_new_attempt(monkeypatch, tmp_path: Path) -> None:
    target = load_cli_targets(ROOT / "targets.example.yaml")["claude-sonnet"]
    calls = 0

    async def fake_execute(case_path, target, runs_dir, **kwargs):
        nonlocal calls
        calls += 1
        run_dir = Path(runs_dir) / f"attempt-{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        return run_dir

    monkeypatch.setattr("bench.tui.execute_case_async", fake_execute)

    async def run() -> tuple[TuiRunController, object]:
        controller = TuiRunController(ROOT / "cases" / "py-bug-001", [target], tmp_path / "runs")
        controller.start_background()
        await controller.wait()
        item = await controller.rerun(target.name)
        assert item is not None and item.task is not None
        await item.task
        return controller, item

    controller, item = asyncio.run(run())
    assert item.attempt == 2
    assert item.status == "completed"
    assert calls == 2
    assert controller.get(target.name).run_dir == tmp_path / "runs" / "attempt-2"
    assert (tmp_path / "runs" / "attempt-1").is_dir()
    assert (tmp_path / "runs" / "attempt-2").is_dir()


def test_tui_selection_preview_and_run_pilot(tmp_path: Path) -> None:
    async def run() -> None:
        app = BenchTuiApp(
            targets_file=ROOT / "targets.example.yaml",
            cases_dir=ROOT / "cases",
            runs_dir=tmp_path / "runs",
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#config-continue")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "SelectionScreen"
            await pilot.click("#case-1")
            await pilot.pause()
            await pilot.click("#case-0")
            await pilot.pause()
            assert len(app.screen.query("#case-list ListItem.selected")) == 1
            await pilot.click("#target-claude-sonnet")
            await pilot.pause()
            await pilot.click("#target-codex-gpt")
            await pilot.pause()
            await pilot.click("#selection-preview")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "PreviewScreen"
            assert app.screen.query_one("#preview-concurrency").value == "2"
            preview = str(app.screen.query_one("#preview-text").render())
            assert "claude-sonnet-4-6" in preview
            assert "--permission-mode" in preview
            assert "gpt-5" in preview
            assert "--ask-for-approval" in preview
            assert app.controller is None
            assert not (tmp_path / "runs").exists()
            # Enter is the primary confirmation path; the focused input's
            # Submitted event must not swallow the screen binding.
            await pilot.click("#preview-concurrency")
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "RunScreen"
            for _ in range(50):
                if app.controller is not None and app.controller.done:
                    break
                await pilot.pause(0.1)
            assert app.controller is not None and app.controller.done
            assert app.controller.summary()["passed"] == 2
            assert len(app.screen.query("#run-list ListItem")) == 3
            overview = str(app.screen.query_one("#run-content").render())
            assert "claude-sonnet" in overview
            assert "codex-gpt" in overview
            assert "session.started" in overview

            await pilot.click("#side-overview")
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            detail = str(app.screen.query_one("#run-content").render())
            assert '"target": "claude-sonnet"' in detail
            assert '"channel": "stdout"' in detail
            await pilot.press("f")
            await pilot.pause()
            assert "view stdout" in str(app.screen.query_one("#run-content").render())
            await pilot.press("f")
            await pilot.pause()
            assert "view stderr" in str(app.screen.query_one("#run-content").render())
            await pilot.press("f")
            await pilot.pause()
            assert '"target": "claude-sonnet"' in str(app.screen.query_one("#run-content").render())

            selected = app.controller.get("claude-sonnet")
            assert selected is not None and selected.run_dir is not None
            opened: list[Path] = []
            app.screen._open_path = opened.append
            await pilot.press("o")
            await pilot.press("d")
            assert opened == [selected.run_dir / "workspace", selected.run_dir / "patch.diff"]

    asyncio.run(run())


def test_tui_preview_accepts_default_highlighted_case(tmp_path: Path) -> None:
    """The initially highlighted case is selectable without an extra Enter."""

    async def run() -> None:
        app = BenchTuiApp(
            targets_file=ROOT / "targets.example.yaml",
            cases_dir=ROOT / "cases",
            runs_dir=tmp_path / "runs",
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#config-continue")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "SelectionScreen"
            assert app.screen.selected_case() == app.case_infos[0]
            await pilot.click("#target-codex-gpt")
            await pilot.click("#selection-preview")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "PreviewScreen"

    asyncio.run(run())


def test_discover_cases_ignores_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "case.yaml").write_text("id: [unterminated", encoding="utf-8")
    assert discover_cases(tmp_path) == []


def test_config_screen_displays_target_yaml_parse_error(tmp_path: Path) -> None:
    bad_targets = tmp_path / "targets.yaml"
    bad_targets.write_text("targets: [unterminated", encoding="utf-8")

    async def run() -> None:
        app = BenchTuiApp(
            targets_file=bad_targets,
            cases_dir=ROOT / "cases",
            runs_dir=tmp_path / "runs",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click("#config-continue")
            error = app.screen.query_one("#config-error").render()
            assert "expected" in str(error).lower() or "yaml" in str(error).lower()

    asyncio.run(run())


def test_config_screen_lists_and_accepts_alternate_target_yaml(tmp_path: Path) -> None:
    candidate = tmp_path / "targets.local.yaml"
    candidate.write_text(
        "targets:\n"
        "  local-cli:\n"
        "    adapter: codex\n"
        "    executable: definitely-not-a-real-agent-command\n",
        encoding="utf-8",
    )

    async def run() -> None:
        app = BenchTuiApp(
            targets_file=tmp_path / "targets.yaml",
            cases_dir=ROOT / "cases",
            runs_dir=tmp_path / "runs",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            assert "targets.local.yaml" in str(app.screen.query_one("#targets-hint").render())
            app.screen.query_one("#targets-path").value = str(candidate)
            await pilot.click("#config-continue")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "SelectionScreen"
            assert "local-cli" in str(app.screen.query_one("#target-local-cli").render())

    asyncio.run(run())


def test_selection_disables_missing_target_and_blocks_preview(tmp_path: Path) -> None:
    targets_file = tmp_path / "targets.yaml"
    targets_file.write_text(
        "targets:\n"
        "  missing-cli:\n"
        "    adapter: codex\n"
        "    executable: definitely-not-a-real-agent-command\n",
        encoding="utf-8",
    )

    async def run() -> None:
        app = BenchTuiApp(
            targets_file=targets_file,
            cases_dir=ROOT / "cases",
            runs_dir=tmp_path / "runs",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click("#config-continue")
            await pilot.pause()
            checkbox = app.screen.query_one("#target-missing-cli")
            assert checkbox.disabled
            assert "disabled:" in str(checkbox.render())
            await pilot.click("#case-0")
            await pilot.pause()
            await pilot.click("#selection-preview")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "SelectionScreen"
            assert "Select at least one available CLI target" in str(
                app.screen.query_one("#selection-error").render()
            )
            assert app.controller is None
            assert not (tmp_path / "runs").exists()

    asyncio.run(run())


def test_running_quit_offers_all_choices_and_stop_exits_cleanly(tmp_path: Path) -> None:
    target = fake_target("slow-quit-target", "sleep", BENCH_FAKE_SLEEP_SECONDS="30")
    case = CaseInfo(ROOT / "cases" / "py-bug-001" / "case.yaml", "py-bug-001", "")
    app = BenchTuiApp()
    controller = TuiRunController(case.path, [target], tmp_path / "runs", keep_workspace=True)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(RunScreen(app, controller, case))
            for _ in range(100):
                item = controller.get(target.name)
                if item and item.status == "running":
                    break
                await pilot.pause(0.025)
            else:
                raise AssertionError("slow fake target did not start")

            await pilot.press("q")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "ConfirmExitScreen"
            assert app.screen.query_one("#confirm-stop")
            assert app.screen.query_one("#confirm-continue")
            assert app.screen.query_one("#confirm-cancel")
            await pilot.click("#confirm-continue")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "RunScreen"
            assert controller.running

            await pilot.press("q")
            await pilot.pause()
            await pilot.click("#confirm-cancel")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "RunScreen"
            assert controller.running

            await pilot.press("q")
            await pilot.pause()
            await pilot.click("#confirm-stop")
            for _ in range(200):
                if controller.done:
                    break
                await asyncio.sleep(0.025)
            assert controller.done
            item = controller.get(target.name)
            assert item is not None and item.status == "stopped"

    asyncio.run(run())


def test_controller_rapid_reruns_share_one_attempt(monkeypatch, tmp_path: Path) -> None:
    target = load_cli_targets(ROOT / "targets.example.yaml")["claude-sonnet"]
    calls = 0

    async def fake_execute(case_path, target, runs_dir, **kwargs):
        nonlocal calls
        calls += 1
        run_dir = Path(runs_dir) / f"attempt-{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.sleep(0.02)
        (run_dir / "run.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        return run_dir

    monkeypatch.setattr("bench.tui.execute_case_async", fake_execute)

    async def run() -> tuple[TuiRunController, list[RunItem | None]]:
        controller = TuiRunController(ROOT / "cases" / "py-bug-001", [target], tmp_path / "runs")
        controller.start_background()
        await controller.wait()
        results = list(await asyncio.gather(*(controller.rerun(target.name) for _ in range(4))))
        item = results[0]
        assert item is not None and item.task is not None
        await item.task
        await asyncio.sleep(0)
        return controller, results

    controller, results = asyncio.run(run())
    assert all(item is results[0] for item in results)
    assert results[0] is not None and results[0].attempt == 2
    assert calls == 2
    assert set(controller._tasks) == {(target.name, 1), (target.name, 2)}


def test_stop_race_with_rerun_does_not_leave_replacement_running(monkeypatch, tmp_path: Path) -> None:
    target = load_cli_targets(ROOT / "targets.example.yaml")["claude-sonnet"]
    calls = 0

    async def fake_execute(case_path, target, runs_dir, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(60)

    monkeypatch.setattr("bench.tui.execute_case_async", fake_execute)

    async def run() -> TuiRunController:
        controller = TuiRunController(ROOT / "cases" / "py-bug-001", [target], tmp_path / "runs")
        controller.start_background()
        await asyncio.sleep(0)
        rerun_task = asyncio.create_task(controller.rerun(target.name))
        stop_task = asyncio.create_task(controller.stop(target.name))
        rerun_result, _ = await asyncio.gather(rerun_task, stop_task)
        assert rerun_result is None or rerun_result.status == "stopped"
        await controller.stop_all()
        return controller

    controller = asyncio.run(run())
    assert controller.get(target.name) is not None
    assert controller.get(target.name).status == "stopped"
    assert all(task.done() for task in controller._tasks.values())


def test_overview_lowercase_s_does_not_stop_all(tmp_path: Path) -> None:
    target = load_cli_targets(ROOT / "targets.example.yaml")["claude-sonnet"]
    app = BenchTuiApp()
    case = CaseInfo(ROOT / "cases" / "py-bug-001" / "case.yaml", "py-bug-001", "")
    controller = TuiRunController(case.path, [target], tmp_path / "runs")
    screen = RunScreen(app, controller, case)
    screen.selected_key = "__overview__"
    screen.run_worker = AsyncMock()  # type: ignore[method-assign]
    screen.action_stop_selected()
    screen.run_worker.assert_not_awaited()


def test_run_screen_renders_markup_like_output_and_errors(tmp_path: Path) -> None:
    target = load_cli_targets(ROOT / "targets.example.yaml")["claude-sonnet"]

    async def run() -> None:
        app = BenchTuiApp()
        case = CaseInfo(ROOT / "cases" / "py-bug-001" / "case.yaml", "py-bug-001", "")
        controller = TuiRunController(case.path, [target], tmp_path / "runs")
        item = controller.get(target.name)
        assert item is not None
        item.status = "process_error"
        item.error = "bad [/oops] [bold]markup"
        item.events.append({"channel": "stdout", "raw": "literal [/oops] [bold] text"})

        async with app.run_test(size=(100, 30)) as pilot:
            # Push a mounted RunScreen directly to isolate rendering from the
            # process runner; no agent invocation is needed for this check.
            app.push_screen(RunScreen(app, controller, case))
            await pilot.pause()
            await pilot.click("#side-0")
            await pilot.pause()
            rendered = str(app.screen.query_one("#run-content").render())
            assert "literal [/oops] [bold] text" in rendered
            assert "bad [/oops] [bold]markup" in rendered

    asyncio.run(run())


def test_controller_enforces_concurrency_cap(monkeypatch, tmp_path: Path) -> None:
    targets = load_cli_targets(ROOT / "targets.example.yaml")
    selected = [targets["claude-sonnet"], targets["codex-gpt"], targets["opencode-qwen"]]
    active = 0
    peak = 0

    async def fake_execute(case_path, target, runs_dir, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        run_dir = Path(runs_dir) / target.name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        return run_dir

    monkeypatch.setattr("bench.tui.execute_case_async", fake_execute)

    async def run() -> TuiRunController:
        controller = TuiRunController(
            ROOT / "cases" / "py-bug-001", selected, tmp_path / "runs", max_concurrency=1
        )
        controller.start_background()
        await controller.wait()
        return controller

    controller = asyncio.run(run())
    assert peak == 1
    assert all(item.status == "completed" for item in controller.items.values())


def test_stop_all_materializes_artifacts_for_queued_items(monkeypatch, tmp_path: Path) -> None:
    targets = load_cli_targets(ROOT / "targets.example.yaml")
    selected = [targets["claude-sonnet"], targets["codex-gpt"]]
    started = asyncio.Event()

    async def fake_execute(case_path, target, runs_dir, **kwargs):
        run_dir = Path(runs_dir) / f"{target.name}-stopped"
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        lifecycle_callback = kwargs.get("lifecycle_callback")

        def notify(phase: str, *, result: dict | None = None) -> None:
            if lifecycle_callback is not None:
                lifecycle_callback(
                    RunLifecycle(
                        phase=phase,
                        run_id=run_dir.name,
                        case="py-bug-001",
                        target=target.name,
                        run_dir=run_dir,
                        workspace=workspace,
                        status=(result or {}).get("status", "running"),
                        result=result,
                        error=(result or {}).get("error"),
                    )
                )

        notify("prepared")
        notify("started")

        async def finish_stopped() -> Path:
            result = {
                "status": "stopped",
                "timed_out": False,
                "error": "stopped by user",
            }
            (run_dir / "run.json").write_text(json.dumps(result), encoding="utf-8")
            (run_dir / "grader.json").write_text(
                json.dumps({"status": "not_run", "score": None, "reason": "user_stopped"}),
                encoding="utf-8",
            )
            (run_dir / "events.jsonl").touch()
            (run_dir / "patch.diff").write_text("", encoding="utf-8")
            (run_dir / "stdout.log").write_text("", encoding="utf-8")
            (run_dir / "stderr.log").write_text("", encoding="utf-8")
            notify("finished", result=result)
            return run_dir

        if kwargs.get("cancel_before_agent"):
            return await finish_stopped()
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return await finish_stopped()

    monkeypatch.setattr("bench.tui.execute_case_async", fake_execute)

    async def run() -> TuiRunController:
        controller = TuiRunController(
            ROOT / "cases" / "py-bug-001",
            selected,
            tmp_path / "runs",
            max_concurrency=1,
        )
        controller.start_background()
        await started.wait()
        await controller.stop_all()
        return controller

    controller = asyncio.run(run())
    for item in controller.items.values():
        assert item.status == "stopped"
        assert item.run_dir is not None
        result = json.loads((item.run_dir / "run.json").read_text(encoding="utf-8"))
        grader = json.loads((item.run_dir / "grader.json").read_text(encoding="utf-8"))
        assert result == {"status": "stopped", "timed_out": False, "error": "stopped by user"}
        assert grader == {"status": "not_run", "score": None, "reason": "user_stopped"}
        assert (item.run_dir / "events.jsonl").is_file()
        assert (item.run_dir / "patch.diff").is_file()
        assert (item.run_dir / "workspace").is_dir()


def test_controller_distinguishes_timeout_agent_and_process_errors(tmp_path: Path) -> None:
    targets = [
        fake_target("timeout-target", "sleep", BENCH_FAKE_SLEEP_SECONDS="30"),
        fake_target("agent-error-target", "exit-nonzero"),
        replace(
            fake_target("process-error-target", "normal"),
            command_prefix=(),
            executable=str(tmp_path / "missing-agent-command"),
        ),
    ]

    async def run() -> TuiRunController:
        controller = TuiRunController(
            ROOT / "cases" / "py-bug-001",
            targets,
            tmp_path / "runs",
            timeout_seconds=0.15,
        )
        controller.start_background()
        await controller.wait()
        return controller

    controller = asyncio.run(run())
    assert {name: item.status for name, item in controller.items.items()} == {
        "timeout-target": "timeout",
        "agent-error-target": "agent_error",
        "process-error-target": "process_error",
    }


def test_stop_all_terminates_cli_process_tree_and_finalizes_run(tmp_path: Path) -> None:
    target = fake_target(
        "process-tree-target",
        "spawn-child",
        BENCH_FAKE_SLEEP_SECONDS="30",
        BENCH_FAKE_CHILD_DELAY_SECONDS="1",
    )

    async def run() -> tuple[TuiRunController, Path]:
        controller = TuiRunController(
            ROOT / "cases" / "py-bug-001",
            [target],
            tmp_path / "runs",
            keep_workspace=True,
        )
        controller.start_background()
        for _ in range(200):
            item = controller.get(target.name)
            if item and item.run_dir and (item.run_dir / "workspace" / "agent-started").is_file():
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("fake agent process tree did not start")

        await controller.stop_all()
        await controller.wait()
        await asyncio.sleep(1.25)
        item = controller.get(target.name)
        assert item is not None and item.run_dir is not None
        return controller, item.run_dir

    controller, run_dir = asyncio.run(run())
    item = controller.get(target.name)
    assert controller.done
    assert not controller.running
    assert item is not None and item.status == "stopped"
    result = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    grader = json.loads((run_dir / "grader.json").read_text(encoding="utf-8"))
    assert result["status"] == "stopped"
    assert result["timed_out"] is False
    assert result["error"] == "stopped by user"
    assert grader == {"status": "not_run", "score": None, "reason": "user_stopped"}
    assert (run_dir / "events.jsonl").is_file()
    assert (run_dir / "patch.diff").is_file()
    assert (run_dir / "workspace").is_dir()
    assert not (run_dir / "workspace" / "orphan.txt").exists()
