import asyncio
import json
import sys
from pathlib import Path

from bench.adapters.base import AgentRunResult, NormalizedEvent, TargetConfig
from bench.cli import main as cli_main
from bench.config import load_targets
from bench.runner import (
    RunLifecycle,
    SuiteExecutionResult,
    execute_case,
    execute_matrix,
    execute_matrix_async,
    execute_suite_async,
)
import bench.runner as runner


ROOT = Path(__file__).resolve().parents[1]


def test_fake_agent_runs_case_and_grader(tmp_path):
    targets = load_targets(ROOT / "targets.example.yaml")
    run_dir = execute_case(ROOT / "cases" / "ts-bug-001", targets["codex-gpt"], tmp_path)
    result = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert result["status"] == "passed"
    assert result["grader"]["score"] == 100
    assert result["tool_calls"] == 1
    assert "return value;" in (run_dir / "patch.diff").read_text(encoding="utf-8")
    assert (run_dir / "events.jsonl").is_file()
    assert json.loads((run_dir / "grader.json").read_text(encoding="utf-8"))["score"] == 100
    assert not (run_dir / "workspace").exists()


def test_matrix_runs_targets_in_isolated_workspaces(tmp_path, capsys):
    targets = load_targets(ROOT / "targets.example.yaml")
    selected = [targets["claude-sonnet"], targets["codex-gpt"]]

    matrix = execute_matrix(
        ROOT / "cases" / "ts-bug-001",
        selected,
        tmp_path,
        max_concurrency=2,
    )

    assert [item.target for item in matrix] == ["claude-sonnet", "codex-gpt"]
    assert list(matrix.run_dirs) == ["claude-sonnet", "codex-gpt"]
    assert len(set(matrix.run_dirs.values())) == 2
    assert not matrix.errors
    for name in ("claude-sonnet", "codex-gpt"):
        assert matrix.results[name]["status"] == "passed"
        assert matrix.results[name]["target"] == name
        assert not (matrix.run_dirs[name] / "workspace").exists()
        event = json.loads(
            (matrix.run_dirs[name] / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        assert event["target"] == name

    live_output = capsys.readouterr().out
    assert "[claude-sonnet][stdout]" in live_output
    assert "[codex-gpt][stdout]" in live_output


def test_async_matrix_honours_max_concurrency(tmp_path, monkeypatch):
    active = 0
    peak = 0

    async def fake_execute_case_async(case_path, target, runs_root, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            run_dir = runs_root / target.name
            run_dir.mkdir()
            (run_dir / "run.json").write_text(
                json.dumps({"target": target.name, "status": "completed"}),
                encoding="utf-8",
            )
            return run_dir
        finally:
            active -= 1

    monkeypatch.setattr(runner, "execute_case_async", fake_execute_case_async)
    targets = [
        TargetConfig(name=f"target-{index}", adapter="fake")
        for index in range(4)
    ]

    matrix = asyncio.run(
        execute_matrix_async(
            Path("unused"),
            targets,
            tmp_path,
            max_concurrency=2,
            live=False,
        )
    )

    assert peak == 2
    assert [item.target for item in matrix] == [target.name for target in targets]
    assert all(item.error is None for item in matrix)


def test_async_suite_runs_cartesian_product_with_global_limit_and_stable_order(tmp_path, monkeypatch):
    """Suite scheduling is global across cases, not one semaphore per case."""

    case_paths = []
    for name in ("case-a", "case-b"):
        case_dir = tmp_path / name
        case_dir.mkdir()
        (case_dir / "case.yaml").write_text(
            f"id: {name}\nfixture: fixture\ninstruction: prompt.md\n",
            encoding="utf-8",
        )
        (case_dir / "prompt.md").write_text("do the task", encoding="utf-8")
        case_paths.append(case_dir)

    active = 0
    peak = 0
    calls = []

    async def fake_execute_case_async(case_path, target, runs_root, **kwargs):
        nonlocal active, peak
        calls.append((case_path.name, target.name, kwargs.get("event_prefix")))
        active += 1
        peak = max(peak, active)
        try:
            # Different delays make completion order differ from input order.
            await asyncio.sleep(0.03 if target.name == "slow" else 0.005)
            if target.name == "broken":
                raise RuntimeError("simulated target failure")
            run_dir = runs_root / f"{case_path.name}--{target.name}"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps({"case": case_path.name, "target": target.name, "status": "passed"}),
                encoding="utf-8",
            )
            return run_dir
        finally:
            active -= 1

    monkeypatch.setattr(runner, "execute_case_async", fake_execute_case_async)
    targets = [
        TargetConfig(name="slow", adapter="fake"),
        TargetConfig(name="fast", adapter="fake"),
        TargetConfig(name="broken", adapter="fake"),
    ]

    suite = asyncio.run(
        execute_suite_async(
            case_paths,
            targets,
            tmp_path / "runs",
            max_concurrency=2,
            live=False,
        )
    )

    assert isinstance(suite, SuiteExecutionResult)
    assert peak == 2
    assert [(item.case, item.target) for item in suite] == [
        ("case-a", "slow"),
        ("case-a", "fast"),
        ("case-a", "broken"),
        ("case-b", "slow"),
        ("case-b", "fast"),
        ("case-b", "broken"),
    ]
    assert set(suite.errors) == {
        ("case-a", "broken"),
        ("case-b", "broken"),
    }
    assert len(suite.run_dirs) == 4
    assert suite.summary == {
        "total": 6,
        "passed": 4,
        "failed": 0,
        "orchestration_errors": 2,
        "timed_out": 0,
    }
    assert all(prefix == f"{case}/{target}" for case, target, prefix in calls)
    # The report representation is JSON-safe and keeps both flat rows and the
    # original run payload for downstream CLI/web consumers.
    json.dumps(suite.to_dict())


def test_async_suite_isolates_invalid_case_and_accepts_mapping_labels(tmp_path, monkeypatch):
    good = tmp_path / "good"
    good.mkdir()
    (good / "case.yaml").write_text("id: manifest-id\n", encoding="utf-8")
    bad = tmp_path / "does-not-exist"

    async def fake_execute_case_async(case_path, target, runs_root, **kwargs):
        run_dir = runs_root / f"{case_path.name}-{target.name}"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"status": "completed", "target": target.name}),
            encoding="utf-8",
        )
        return run_dir

    monkeypatch.setattr(runner, "execute_case_async", fake_execute_case_async)
    target = TargetConfig(name="one", adapter="fake")
    suite = asyncio.run(
        execute_suite_async(
            {"explicit-good": good, "explicit-bad": bad},
            [target],
            tmp_path / "runs",
            live=False,
        )
    )

    assert [item.case for item in suite] == ["explicit-good", "explicit-bad"]
    assert suite["explicit-good", "one"].result["status"] == "completed"
    assert suite["explicit-bad", "one"].error.startswith("FileNotFoundError:")


def test_cli_suite_runs_manifest_cartesian_product(tmp_path, capsys):
    exit_code = cli_main(
        [
            "--targets-file",
            str(ROOT / "targets.example.yaml"),
            "suite",
            "--cases",
            str(ROOT / "cases" / "smoke.yaml"),
            "--targets",
            "claude-sonnet,codex-gpt",
            "--runs-dir",
            str(tmp_path),
            "--max-concurrency",
            "2",
            "--no-live",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "case\ttarget\tmodel\tstatus" in output
    assert "py-bug-001\tclaude-sonnet" in output
    assert "ts-bug-001\tcodex-gpt" in output
    report_line = next(line for line in output.splitlines() if line.startswith("suite_report\t"))
    report_path = Path(report_line.split("\t", 1)[1])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["total"] == 4
    assert report["summary"]["passed"] == 4
    assert [(row["case"], row["target"]) for row in report["runs"]] == [
        ("ts-bug-001", "claude-sonnet"),
        ("ts-bug-001", "codex-gpt"),
        ("py-bug-001", "claude-sonnet"),
        ("py-bug-001", "codex-gpt"),
    ]


def test_case_emits_lifecycle_and_event_callbacks_after_persisting(tmp_path):
    targets = load_targets(ROOT / "targets.example.yaml")
    events = []
    lifecycle = []

    run_dir = execute_case(
        ROOT / "cases" / "ts-bug-001",
        targets["codex-gpt"],
        tmp_path,
        keep_workspace=True,
        live=False,
        event_callback=events.append,
        lifecycle_callback=lifecycle.append,
    )

    assert [item.phase for item in lifecycle] == ["prepared", "started", "finished"]
    assert all(isinstance(item, RunLifecycle) for item in lifecycle)
    assert lifecycle[-1].run_dir == run_dir
    assert lifecycle[-1].status == "passed"
    assert lifecycle[-1].result["status"] == "passed"
    assert events
    assert events[0]["seq"] == 1
    persisted = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events == persisted


def test_cancelled_case_writes_stopped_artifacts_and_skips_grader(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    fixture = case_dir / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "file.txt").write_text("before\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(
        "id: cancel-case\nfixture: fixture\ninstruction: prompt.md\n",
        encoding="utf-8",
    )
    (case_dir / "prompt.md").write_text("wait", encoding="utf-8")

    async def exercise():
        started = asyncio.Event()

        class SlowAdapter:
            name = "fake"
            transport = "cli"

            def describe(self, spec):
                return {"adapter": self.name, "transport": self.transport}

            async def run(self, spec, emit_event):
                emit_event(NormalizedEvent(kind="started", source=self.name, summary="running"))
                started.set()
                await asyncio.sleep(60)
                return AgentRunResult(status="completed", return_code=0)

        monkeypatch.setattr(runner, "get_adapter", lambda name: SlowAdapter())
        target = TargetConfig(name="slow", adapter="fake")
        lifecycle = []
        task = asyncio.create_task(
            runner.execute_case_async(
                case_dir,
                target,
                tmp_path / "runs",
                live=False,
                lifecycle_callback=lifecycle.append,
            )
        )
        await started.wait()
        task.cancel()
        run_dir = await task
        return run_dir, lifecycle

    run_dir, lifecycle = asyncio.run(exercise())

    result = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    grader = json.loads((run_dir / "grader.json").read_text(encoding="utf-8"))
    assert result["status"] == "stopped"
    assert result["timed_out"] is False
    assert result["error"] == "stopped by user"
    assert grader == {"status": "not_run", "score": None, "reason": "user_stopped"}
    assert (run_dir / "patch.diff").is_file()
    assert (run_dir / "events.jsonl").is_file()
    assert (run_dir / "workspace").is_dir()
    assert [item.phase for item in lifecycle] == ["prepared", "started", "finished"]
    assert lifecycle[-1].status == "stopped"


def test_adapter_consumed_cancellation_still_skips_grader(tmp_path, monkeypatch):
    """Adapters may terminate their process and return stopped instead of re-raising."""
    case_dir = tmp_path / "case"
    (case_dir / "fixture").mkdir(parents=True)
    (case_dir / "fixture" / "file.txt").write_text("before\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(
        "id: adapter-stop-case\nfixture: fixture\ninstruction: prompt.md\n",
        encoding="utf-8",
    )
    (case_dir / "prompt.md").write_text("wait", encoding="utf-8")

    class SelfStoppingAdapter:
        name = "fake"
        transport = "cli"

        def describe(self, spec):
            return {"adapter": self.name, "transport": self.transport}

        async def run(self, spec, emit_event):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return AgentRunResult(
                    status="stopped",
                    stdout="partial output\n",
                    metadata={"stopped_by_user": True},
                )

    monkeypatch.setattr(runner, "get_adapter", lambda name: SelfStoppingAdapter())

    async def exercise():
        task = asyncio.create_task(
            runner.execute_case_async(
                case_dir,
                TargetConfig(name="self-stopping", adapter="fake"),
                tmp_path / "runs",
                live=False,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        return await task

    run_dir = asyncio.run(exercise())
    result = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    grader = json.loads((run_dir / "grader.json").read_text(encoding="utf-8"))
    assert result["status"] == "stopped"
    assert grader["status"] == "not_run"


def test_cancel_during_slow_grader_stops_process_tree_and_writes_artifacts(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    fixture = case_dir / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "file.txt").write_text("before\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(
        json.dumps(
            {
                "id": "cancel-grader-case",
                "fixture": "fixture",
                "instruction": "prompt.md",
                "limits": {"timeout_seconds": 8},
                "grader": {"command": [sys.executable, "grader.py"]},
            }
        ),
        encoding="utf-8",
    )
    (case_dir / "prompt.md").write_text("finish immediately", encoding="utf-8")
    (case_dir / "grader.py").write_text(
        """\
import subprocess
import sys
import time
from pathlib import Path

subprocess.Popen([
    sys.executable,
    "-c",
    "import time; from pathlib import Path; time.sleep(1); "
    "Path('orphan.txt').write_text('still running', encoding='utf-8')",
])
Path("grader-started").write_text("started", encoding="utf-8")
time.sleep(30)
""",
        encoding="utf-8",
    )

    class ImmediateAdapter:
        name = "fake"
        transport = "cli"

        def describe(self, spec):
            return {"adapter": self.name, "transport": self.transport}

        async def run(self, spec, emit_event):
            return AgentRunResult(status="completed", return_code=0)

    monkeypatch.setattr(runner, "get_adapter", lambda name: ImmediateAdapter())

    async def exercise():
        runs_dir = tmp_path / "runs"
        task = asyncio.create_task(
            runner.execute_case_async(
                case_dir,
                TargetConfig(name="slow-grader", adapter="fake"),
                runs_dir,
                keep_workspace=True,
                live=False,
            )
        )

        for _ in range(200):
            if list(runs_dir.glob("*/workspace/grader-started")):
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("grader did not start")

        loop = asyncio.get_running_loop()
        cancelled_at = loop.time()
        task.cancel()
        run_dir = await task
        cancel_duration = loop.time() - cancelled_at

        # A descendant left behind by the grader would create this file.
        await asyncio.sleep(1.25)
        return run_dir, cancel_duration

    run_dir, cancel_duration = asyncio.run(exercise())

    assert cancel_duration < 2
    result = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    grader = json.loads((run_dir / "grader.json").read_text(encoding="utf-8"))
    assert result["status"] == "stopped"
    assert result["timed_out"] is False
    assert result["error"] == "stopped by user"
    assert grader == {"status": "not_run", "score": None, "reason": "user_stopped"}
    assert (run_dir / "patch.diff").is_file()
    assert (run_dir / "events.jsonl").is_file()
    assert not (run_dir / "workspace" / "orphan.txt").exists()
