from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .adapters.registry import get_adapter
from .config import load_case, load_targets
from .runner import (
    MatrixExecutionResult,
    SuiteExecutionResult,
    execute_case,
    execute_matrix,
    execute_suite,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench", description="Run coding-agent adapters against benchmark cases.")
    parser.add_argument("--targets-file", type=Path, default=Path("targets.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("targets", help="List configured targets")
    sub.add_parser("adapters", help="List supported adapters")
    case = sub.add_parser("validate", help="Validate a case manifest")
    case.add_argument("--case", type=Path, required=True)
    case.add_argument("--targets-file", type=Path, default=argparse.SUPPRESS)
    run = sub.add_parser("run", help="Run one case against one target")
    run.add_argument("--case", type=Path, required=True)
    run.add_argument("--target", required=True)
    run.add_argument("--runs-dir", type=Path, default=Path("runs"))
    run.add_argument("--timeout", type=float)
    run.add_argument("--keep-workspace", action="store_true")
    run.add_argument("--no-live", action="store_true", help="Do not print agent events while running")
    run.add_argument("--targets-file", type=Path, default=argparse.SUPPRESS)
    matrix = sub.add_parser("matrix", help="Run one case against multiple targets")
    matrix.add_argument("--case", type=Path, required=True)
    matrix.add_argument("--targets", required=True, help="Comma-separated target names")
    matrix.add_argument("--runs-dir", type=Path, default=Path("runs"))
    matrix.add_argument("--timeout", type=float)
    matrix.add_argument("--max-concurrency", type=int, help="Maximum agents running at once (default: all)")
    matrix.add_argument("--keep-workspace", action="store_true")
    matrix.add_argument("--no-live", action="store_true", help="Do not print agent events while running")
    matrix.add_argument("--json", action="store_true", help="Print the matrix report as JSON")
    matrix.add_argument("--targets-file", type=Path, default=argparse.SUPPRESS)
    suite = sub.add_parser("suite", help="Run multiple cases against multiple targets")
    suite.add_argument(
        "--cases",
        required=True,
        help="Comma-separated case directories, or one YAML suite manifest containing a 'cases' list",
    )
    suite.add_argument("--targets", required=True, help="Comma-separated target names")
    suite.add_argument("--runs-dir", type=Path, default=Path("runs"))
    suite.add_argument("--timeout", type=float)
    suite.add_argument("--max-concurrency", type=int, help="Maximum case x target runs at once (default: all)")
    suite.add_argument("--keep-workspace", action="store_true")
    suite.add_argument("--no-live", action="store_true", help="Do not print agent events while running")
    suite.add_argument("--json", action="store_true", help="Print the suite report as JSON")
    suite.add_argument("--targets-file", type=Path, default=argparse.SUPPRESS)
    tui = sub.add_parser("tui", help="Open the interactive multi-agent runner")
    tui.add_argument("--cases-dir", type=Path, default=Path("cases"), help="Directory scanned for case.yaml files")
    tui.add_argument("--runs-dir", type=Path, default=Path("runs"), help="Directory for run artifacts")
    tui.add_argument("--timeout", type=float, help="Override each case's agent timeout in seconds")
    tui.add_argument("--max-concurrency", type=int, help="Initial concurrent CLI limit (default: all selected)")
    tui.add_argument(
        "--no-keep-workspace",
        action="store_true",
        help="Remove workspaces after completed runs (stopped workspaces are retained)",
    )
    tui.add_argument("--targets-file", type=Path, default=argparse.SUPPRESS, help="Target YAML (default: targets.yaml)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "adapters":
        for name in (
            "claude-code",
            "codex",
            "opencode",
            "gemini",
            "claude-agent-sdk",
            "codex-sdk",
        ):
            print(name)
        return 0
    if args.command == "targets":
        targets = load_targets(args.targets_file)
        for target in targets.values():
            print(f"{target.name}\t{target.adapter}\t{target.model or 'default'}\t{target.executable}")
        return 0
    if args.command == "validate":
        case = load_case(args.case)
        required = ("id", "instruction", "fixture")
        missing = [key for key in required if not case.get(key)]
        if missing:
            print(f"invalid: missing {', '.join(missing)}", file=sys.stderr)
            return 2
        print(json.dumps({"status": "valid", "id": case.get("id"), "path": str(case["_manifest_path"])}, ensure_ascii=False))
        return 0
    if args.command == "tui":
        try:
            from .tui import run_tui
        except ImportError as exc:
            print(
                "TUI requires Textual. Install it with `python -m pip install -e .`.",
                file=sys.stderr,
            )
            print(f"detail: {exc}", file=sys.stderr)
            return 2
        return run_tui(
            targets_file=args.targets_file,
            cases_dir=args.cases_dir,
            runs_dir=args.runs_dir,
            timeout_seconds=args.timeout,
            max_concurrency=args.max_concurrency,
            keep_workspace=not args.no_keep_workspace,
        )
    targets = load_targets(args.targets_file)
    names = [args.target] if args.command == "run" else [item.strip() for item in args.targets.split(",") if item.strip()]
    if not names:
        print("at least one target is required", file=sys.stderr)
        return 2
    for name in names:
        if name not in targets:
            print(f"unknown target: {name}", file=sys.stderr)
            return 2
    if args.command == "run":
        run_dir = execute_case(
            args.case,
            targets[names[0]],
            args.runs_dir,
            timeout_seconds=args.timeout,
            keep_workspace=args.keep_workspace,
            live=not args.no_live,
        )
        result = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        print(f"{names[0]}\t{result['status']}\t{result['grader'].get('score')}\t{result['duration_ms']}ms\t{run_dir}")
        return 0

    if args.command == "suite":
        try:
            case_paths = _parse_suite_cases(args.cases)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            print(f"invalid suite cases: {exc}", file=sys.stderr)
            return 2
        if not case_paths:
            print("invalid suite cases: at least one case is required", file=sys.stderr)
            return 2
        started = time.monotonic()
        suite_result = execute_suite(
            case_paths,
            [targets[name] for name in names],
            args.runs_dir,
            timeout_seconds=args.timeout,
            keep_workspace=args.keep_workspace,
            live=not args.no_live,
            max_concurrency=args.max_concurrency,
        )
        report = _suite_report(case_paths, names, suite_result, started)
        report_path = _write_suite_report(args.runs_dir, report)
        _print_suite_rows(suite_result)
        print(f"suite_report\t{report_path}")
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    started = time.monotonic()
    matrix_result = execute_matrix(
        args.case,
        [targets[name] for name in names],
        args.runs_dir,
        timeout_seconds=args.timeout,
        keep_workspace=args.keep_workspace,
        live=not args.no_live,
        max_concurrency=args.max_concurrency,
    )
    report = _matrix_report(args.case, names, matrix_result, started)
    report_path = _write_matrix_report(args.runs_dir, report)
    _print_matrix_rows(matrix_result)
    print(f"matrix_report\t{report_path}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _print_matrix_rows(matrix_result: MatrixExecutionResult) -> None:
    """Print a stable, scan-friendly row for each target after live events."""
    print("target\tmodel\tstatus\tscore\tduration\ttool_calls\trun_dir")
    for item in matrix_result:
        if item.result is None:
            print(f"{item.target}\t-\terror\t-\t-\t-\t{item.error or 'unknown error'}")
            continue
        result = item.result
        grader = result.get("grader") or {}
        print(
            "\t".join(
                [
                    item.target,
                    str(result.get("model") or "default"),
                    str(result.get("status") or "unknown"),
                    str(grader.get("score", "unknown")),
                    f"{result.get('duration_ms', 'unknown')}ms",
                    str(result.get("tool_calls", "unknown")),
                    str(item.run_dir or ""),
                ]
            )
        )


def _matrix_report(
    case_path: Path,
    target_names: list[str],
    matrix_result: MatrixExecutionResult,
    started: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in matrix_result:
        if item.result is None:
            rows.append({"target": item.target, "status": "orchestration_error", "error": item.error})
            continue
        result = item.result
        grader = result.get("grader") or {}
        rows.append(
            {
                "target": item.target,
                "model": result.get("model"),
                "adapter": result.get("adapter"),
                "status": result.get("status"),
                "score": grader.get("score"),
                "duration_ms": result.get("duration_ms"),
                "tool_calls": result.get("tool_calls"),
                "timed_out": result.get("timed_out", False),
                "run_dir": str(item.run_dir) if item.run_dir else None,
                "error": result.get("error"),
            }
        )
    return {
        "matrix_id": f"matrix-{uuid.uuid4().hex[:10]}",
        "case": str(case_path),
        "targets": target_names,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "runs": rows,
    }


def _write_matrix_report(runs_dir: Path, report: dict[str, Any]) -> Path:
    runs_dir = runs_dir.resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{report['matrix_id']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _parse_suite_cases(value: str) -> list[Path]:
    """Resolve a ``--cases`` selector into an ordered list of case paths.

    The short form is a comma-separated list of case directories.  When the
    selector contains one existing YAML file with a top-level ``cases`` list,
    that file is treated as a suite manifest.  Relative manifest entries are
    first resolved from the current working directory (which keeps the README
    examples convenient), then from the manifest's directory as a fallback.
    """

    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        return []
    if len(parts) == 1:
        manifest = Path(parts[0])
        if manifest.is_file() and manifest.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            if isinstance(payload, dict) and "cases" in payload:
                raw_cases = payload["cases"]
                if not isinstance(raw_cases, list):
                    raise ValueError("suite manifest 'cases' must be a list")
                resolved: list[Path] = []
                for item in raw_cases:
                    if isinstance(item, dict):
                        item = item.get("path")
                    if not isinstance(item, (str, Path)) or not str(item).strip():
                        raise ValueError("each suite case must be a non-empty path")
                    candidate = Path(str(item))
                    if candidate.is_absolute():
                        resolved.append(candidate)
                    elif candidate.exists():
                        resolved.append(candidate.resolve())
                    else:
                        resolved.append((manifest.parent / candidate).resolve())
                return resolved
    return [Path(part) for part in parts]


def _print_suite_rows(suite_result: SuiteExecutionResult) -> None:
    """Print one stable row per case×target after live events."""

    print("case\ttarget\tmodel\tstatus\tscore\tduration\ttool_calls\trun_dir")
    for item in suite_result:
        if item.result is None:
            print(
                "\t".join(
                    [
                        item.case,
                        item.target,
                        "-",
                        "orchestration_error",
                        "-",
                        "-",
                        "-",
                        item.error or "unknown error",
                    ]
                )
            )
            continue
        result = item.result
        grader = result.get("grader") or {}
        print(
            "\t".join(
                [
                    item.case,
                    item.target,
                    str(result.get("model") or "default"),
                    str(result.get("status") or "unknown"),
                    str(grader.get("score", "unknown")),
                    f"{result.get('duration_ms', 'unknown')}ms",
                    str(result.get("tool_calls", "unknown")),
                    str(item.run_dir or ""),
                ]
            )
        )


def _suite_report(
    case_paths: list[Path],
    target_names: list[str],
    suite_result: SuiteExecutionResult,
    started: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in suite_result:
        if item.result is None:
            rows.append(
                {
                    "case": item.case,
                    "case_path": str(item.case_path) if item.case_path else None,
                    "target": item.target,
                    "status": "orchestration_error",
                    "error": item.error,
                    "run_dir": str(item.run_dir) if item.run_dir else None,
                }
            )
            continue
        result = item.result
        grader = result.get("grader") or {}
        rows.append(
            {
                "case": item.case,
                "case_path": str(item.case_path) if item.case_path else None,
                "target": item.target,
                "model": result.get("model"),
                "adapter": result.get("adapter"),
                "status": result.get("status"),
                "score": grader.get("score"),
                "duration_ms": result.get("duration_ms"),
                "tool_calls": result.get("tool_calls"),
                "timed_out": result.get("timed_out", False),
                "run_dir": str(item.run_dir) if item.run_dir else None,
                "error": result.get("error"),
            }
        )
    return {
        "suite_id": suite_result.suite_id or f"suite-{uuid.uuid4().hex[:10]}",
        "cases": [str(path) for path in case_paths],
        "case_ids": list(suite_result.cases),
        "targets": target_names,
        "started_at": suite_result.started_at or datetime.now(timezone.utc).isoformat(),
        "duration_ms": suite_result.duration_ms
        if suite_result.duration_ms is not None
        else round((time.monotonic() - started) * 1000),
        "summary": suite_result.summary,
        "runs": rows,
    }


def _write_suite_report(runs_dir: Path, report: dict[str, Any]) -> Path:
    runs_dir = runs_dir.resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{report['suite_id']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    main()
