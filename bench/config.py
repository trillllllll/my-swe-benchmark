from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .adapters.base import TargetConfig


def load_targets(path: Path) -> dict[str, TargetConfig]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_targets = payload.get("targets", {})
    if not isinstance(raw_targets, dict):
        raise ValueError("targets.yaml must contain a mapping named 'targets'")
    targets: dict[str, TargetConfig] = {}
    for name, raw in raw_targets.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Target {name!r} must be a mapping")
        executable = str(raw.get("executable", ""))
        executable_path = Path(executable)
        # Resolve local helper scripts relative to the target file.  Bare
        # command names (for example ``claude``) remain bare so PATH lookup
        # and PATHEXT handling continue to work on each host.
        if not executable_path.is_absolute() and (path.parent / executable_path).is_file():
            executable = str((path.parent / executable_path).resolve())
        command_prefix = tuple(str(item) for item in raw.get("command_prefix", []))
        if command_prefix:
            prefix_path = Path(command_prefix[0])
            if not prefix_path.is_absolute() and (path.parent / prefix_path).is_file():
                command_prefix = (str((path.parent / prefix_path).resolve()), *command_prefix[1:])
        targets[str(name)] = TargetConfig(
            name=str(name),
            adapter=str(raw.get("adapter", "")),
            executable=executable,
            model=_optional_string(raw.get("model")),
            mode=_optional_string(raw.get("mode")),
            permission_mode=_optional_string(raw.get("permission_mode")),
            sandbox=_optional_string(raw.get("sandbox")),
            approval_policy=_optional_string(raw.get("approval_policy")),
            agent=_optional_string(raw.get("agent")),
            command_prefix=command_prefix,
            extra_args=tuple(str(item) for item in raw.get("extra_args", [])),
            env={str(key): str(value) for key, value in (raw.get("env", {}) or {}).items()},
            pass_env=tuple(str(item) for item in raw.get("pass_env", [])),
        )
    return targets


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def load_case(path: Path) -> dict[str, Any]:
    manifest_path = path / "case.yaml" if path.is_dir() else path
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Case manifest must be a mapping: {manifest_path}")
    payload["_manifest_path"] = manifest_path
    payload["_case_dir"] = manifest_path.parent
    return payload
