from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def _sleep_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _run_blocking_mode(workspace: Path, *, spawn_child: bool) -> int:
    child: subprocess.Popen[str] | None = None
    if spawn_child:
        orphan_path = workspace / "orphan.txt"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys, time; from pathlib import Path; "
                    "time.sleep(float(sys.argv[2])); "
                    "Path(sys.argv[1]).write_text('child survived', encoding='utf-8')"
                ),
                str(orphan_path),
                str(_sleep_seconds("BENCH_FAKE_CHILD_DELAY_SECONDS", 1.0)),
            ]
        )
        (workspace / "agent-child.pid").write_text(str(child.pid), encoding="utf-8")
    (workspace / "agent-started").write_text(str(os.getpid()), encoding="utf-8")
    print(json.dumps({"type": "agent.blocking", "child_pid": child.pid if child else None}), flush=True)
    try:
        time.sleep(_sleep_seconds("BENCH_FAKE_SLEEP_SECONDS", 60.0))
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
    return 0


def main() -> int:
    workspace = Path(os.environ["BENCH_WORKSPACE"])
    prompt = os.environ.get("BENCH_TASK_PROMPT", "")
    if not prompt:
        prompt = sys.stdin.read()
    mode = os.environ.get("BENCH_FAKE_MODE", "normal")
    print(json.dumps({"type": "session.started", "prompt_length": len(prompt), "mode": mode}), flush=True)
    if mode == "exit-nonzero":
        print(json.dumps({"type": "error", "message": "requested non-zero exit"}), flush=True)
        return 7
    if mode == "sleep":
        return _run_blocking_mode(workspace, spawn_child=False)
    if mode == "spawn-child":
        return _run_blocking_mode(workspace, spawn_child=True)
    if mode != "normal":
        print(json.dumps({"type": "error", "message": f"unknown fake mode: {mode}"}), flush=True)
        return 2
    value_source = workspace / "src" / "value.ts"
    calculator_source = workspace / "src" / "calculator.py"
    if value_source.is_file():
        value_source.write_text(
            "export function normalizeValue(value: number | null | undefined): number {\n"
            "  return value;\n"
            "}\n",
            encoding="utf-8",
        )
        changed_path = "src/value.ts"
    elif calculator_source.is_file():
        calculator_source.write_text(
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n",
            encoding="utf-8",
        )
        changed_path = "src/calculator.py"
    else:
        print(json.dumps({"type": "error", "message": "unsupported fixture"}), flush=True)
        return 1
    print(json.dumps({"type": "tool_call", "tool": "write", "path": changed_path}), flush=True)
    print(json.dumps({"type": "tool_result", "ok": True}), flush=True)
    print(json.dumps({"type": "turn.completed", "status": "completed"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
