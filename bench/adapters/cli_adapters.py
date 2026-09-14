from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from dataclasses import replace
from typing import Any

from .base import AgentRunResult, EventSink, NormalizedEvent, RunSpec
from .common import base_environment, classify_default, command_base, parse_json_event


def _decode_process_line(chunk: bytes) -> str:
    """Decode CLI output across UTF-8 and Windows' active code page."""
    try:
        return chunk.decode("utf-8")
    except UnicodeDecodeError:
        # Windows tools such as taskkill commonly emit the system code page
        # (cp936 on Chinese installations), while agent CLIs usually emit
        # UTF-8.  Try the active Windows codec before replacing characters.
        for encoding in ("mbcs", "cp936") if os.name == "nt" else ("latin-1",):
            try:
                return chunk.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return chunk.decode("utf-8", errors="replace")


class _CliAdapter:
    """Shared streaming process runner for vendor CLIs."""

    name = "cli"
    transport = "cli"
    stdin_prompt = False

    def build_command(self, spec: RunSpec) -> list[str]:
        raise NotImplementedError

    def build_environment(self, spec: RunSpec) -> dict[str, str]:
        return base_environment(spec)

    def parse_event(self, line: str) -> NormalizedEvent | None:
        return parse_json_event(line, self.name)

    def classify_exit(self, return_code: int | None, timed_out: bool) -> str:
        return classify_default(return_code, timed_out)

    def describe(self, spec: RunSpec) -> dict[str, object]:
        return {
            "adapter": self.name,
            "transport": self.transport,
            "command": self.build_command(spec),
        }

    async def run(self, spec: RunSpec, emit_event: EventSink) -> AgentRunResult:
        command = self.build_command(spec)
        environment = self.build_environment(spec)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        return_code: int | None = None
        timed_out = False
        error: str | None = None
        process: asyncio.subprocess.Process | None = None
        reader_tasks: list[asyncio.Task[None]] = []

        try:
            kwargs: dict[str, Any] = {
                "cwd": str(spec.workspace),
                "env": environment,
                "stdin": asyncio.subprocess.PIPE if self.stdin_prompt else asyncio.subprocess.DEVNULL,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            process = await asyncio.create_subprocess_exec(*command, **kwargs)

            async def consume(
                stream: asyncio.StreamReader | None,
                channel: str,
                sink: list[str],
            ) -> None:
                if stream is None:
                    return
                while True:
                    chunk = await stream.readline()
                    if not chunk:
                        break
                    line = _decode_process_line(chunk)
                    sink.append(line)
                    event = self.parse_event(line)
                    if event is not None:
                        emit_event(replace(event, channel=channel))

            reader_tasks = [
                asyncio.create_task(consume(process.stdout, "stdout", stdout_parts)),
                asyncio.create_task(consume(process.stderr, "stderr", stderr_parts)),
            ]
            if self.stdin_prompt and process.stdin is not None:
                # Start readers before writing.  Very short-lived wrappers
                # (and Windows .cmd shims) can close stdin as soon as they
                # have spawned their child; that must not discard output that
                # is still available on stdout/stderr.
                try:
                    process.stdin.write(spec.prompt.encode("utf-8"))
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionError, OSError):
                    # The process result and captured streams remain the
                    # source of truth.  A closed stdin is common for CLIs
                    # that accept the prompt through an environment or have
                    # already exited, so continue collecting their result.
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except (BrokenPipeError, ConnectionError, OSError):
                        pass
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout=spec.timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                await _terminate_process_tree(process)
                return_code = await process.wait()
            await asyncio.gather(*reader_tasks)
        except OSError as exc:
            error = str(exc)
            if process is not None and process.returncode is None:
                await _terminate_process_tree(process)
            if reader_tasks:
                await asyncio.gather(*reader_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                await _terminate_process_tree(process)
            if reader_tasks:
                await asyncio.gather(*reader_tasks, return_exceptions=True)
            # A user stop is a normal, inspectable run outcome.  Returning
            # the streams collected so far lets the runner persist partial
            # stdout/stderr instead of losing them when the task is
            # cancelled.  The runner will skip grading and mark the final
            # run status as ``stopped``.
            return AgentRunResult(
                status="stopped",
                return_code=process.returncode if process is not None else return_code,
                timed_out=False,
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                error="stopped by user",
                metadata={
                    "command": command,
                    "stopped_by_user": True,
                },
            )

        status = self.classify_exit(return_code, timed_out)
        if error:
            status = "process_error"
        return AgentRunResult(
            status=status,
            return_code=return_code,
            timed_out=timed_out,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            error=error,
            metadata={"command": command},
        )


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Stop the adapter and descendants when a turn exceeds its deadline."""
    pid = process.pid
    if pid is None:
        return
    if os.name == "nt":
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            if completed.returncode == 0:
                return
        except OSError:
            pass
        process.kill()
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


class ClaudeCodeAdapter(_CliAdapter):
    name = "claude-code"

    def build_command(self, spec: RunSpec) -> list[str]:
        target = spec.target
        command = [*command_base(spec), "--print"]
        if target.mode:
            command += ["--output-format", target.mode]
            if target.mode == "stream-json":
                # Claude Code requires --verbose when stream-json is used
                # together with --print.
                command += ["--verbose"]
        if target.model:
            command += ["--model", target.model]
        if target.permission_mode:
            command += ["--permission-mode", target.permission_mode]
        command += ["--no-session-persistence", "--", spec.prompt]
        return command + list(target.extra_args)


class CodexAdapter(_CliAdapter):
    name = "codex"
    stdin_prompt = True

    def build_command(self, spec: RunSpec) -> list[str]:
        target = spec.target
        # Codex exposes approval policy as a top-level option (unlike the
        # other exec options).  Keep it before the ``exec`` subcommand so the
        # command works with current Codex CLI releases as well as wrappers
        # used in offline tests.
        command = command_base(spec)
        if target.approval_policy:
            command += ["--ask-for-approval", target.approval_policy]
        command += ["exec"]
        if target.model:
            command += ["--model", target.model]
        if target.sandbox:
            command += ["--sandbox", target.sandbox]
        command += ["--json", "--ephemeral", "--cd", str(spec.workspace), "-"]
        return command + list(target.extra_args)


class OpenCodeAdapter(_CliAdapter):
    name = "opencode"

    def build_command(self, spec: RunSpec) -> list[str]:
        target = spec.target
        command = [*command_base(spec), "run", "--format", "json", "--dir", str(spec.workspace)]
        if target.model:
            command += ["--model", target.model]
        if target.agent:
            command += ["--agent", target.agent]
        command += [spec.prompt]
        return command + list(target.extra_args)


class GeminiAdapter(_CliAdapter):
    name = "gemini"

    def build_command(self, spec: RunSpec) -> list[str]:
        target = spec.target
        command = [*command_base(spec), "--prompt", spec.prompt]
        if target.model:
            command += ["--model", target.model]
        if target.mode:
            command += ["--output-format", target.mode]
        if target.permission_mode:
            command += ["--approval-mode", target.permission_mode]
        return command + list(target.extra_args)
