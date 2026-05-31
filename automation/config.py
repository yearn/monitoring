"""Parse `automation/jobs.yaml` into typed profile/task dataclasses.

`jobs.yaml` is the single source of truth for what runs on the Hetzner box —
each profile owns a cron expression and an ordered list of tasks. Consumed by
`automation.__main__` (CLI) and `automation.runner` (subprocess executor).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parent.parent))
DEFAULT_JOBS_CONFIG_PATH: Path = REPO_ROOT / "automation" / "jobs.yaml"


class JobsConfigError(ValueError):
    """Raised when jobs.yaml is malformed or violates the schema."""


@dataclass
class Task:
    name: str
    script: str
    args: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class Profile:
    name: str
    cron: str
    tasks: list[Task]
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    @property
    def enabled_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.enabled]


@dataclass
class JobsConfig:
    profiles: dict[str, Profile]
    path: Path

    @property
    def enabled_profiles(self) -> list[Profile]:
        return [p for p in self.profiles.values() if p.enabled]


_TASK_REQUIRED = ("name", "script")
_TASK_ALLOWED = (*_TASK_REQUIRED, "args", "enabled")
_PROFILE_REQUIRED = ("cron",)
_PROFILE_ALLOWED = (*_PROFILE_REQUIRED, "tasks", "env", "enabled", "description")


def load_jobs_config(path: Path | str | None = None) -> JobsConfig:
    """Load and validate jobs.yaml from `path` (defaults to `automation/jobs.yaml`)."""
    config_path = Path(path) if path is not None else DEFAULT_JOBS_CONFIG_PATH
    if not config_path.exists():
        raise JobsConfigError(f"jobs.yaml not found at {config_path}")

    with config_path.open("r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "profiles" not in raw:
        raise JobsConfigError(f"{config_path}: top-level `profiles:` key missing")
    profiles_raw = raw["profiles"]
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise JobsConfigError(f"{config_path}: `profiles:` must be a non-empty mapping")

    profiles: dict[str, Profile] = {}
    for name, body in profiles_raw.items():
        profiles[name] = _parse_profile(name, body, config_path)

    return JobsConfig(profiles=profiles, path=config_path)


def _parse_profile(name: str, body: Any, source: Path) -> Profile:
    if not isinstance(body, dict):
        raise JobsConfigError(f"{source}: profile `{name}` must be a mapping")
    _check_keys(body, _PROFILE_REQUIRED, _PROFILE_ALLOWED, f"profile `{name}`", source)

    env_raw = body.get("env", {}) or {}
    if not isinstance(env_raw, dict):
        raise JobsConfigError(f"{source}: profile `{name}`.env must be a mapping")
    env = {str(k): str(v) for k, v in env_raw.items()}

    tasks_raw = body.get("tasks", []) or []
    if not isinstance(tasks_raw, list):
        raise JobsConfigError(f"{source}: profile `{name}`.tasks must be a list")
    tasks = [_parse_task(name, idx, t, source) for idx, t in enumerate(tasks_raw)]

    return Profile(
        name=name,
        cron=str(body["cron"]),
        tasks=tasks,
        env=env,
        enabled=bool(body.get("enabled", True)),
    )


def _parse_task(profile: str, idx: int, body: Any, source: Path) -> Task:
    if not isinstance(body, dict):
        raise JobsConfigError(f"{source}: profile `{profile}` task #{idx} must be a mapping")
    _check_keys(body, _TASK_REQUIRED, _TASK_ALLOWED, f"profile `{profile}` task #{idx}", source)

    args_raw = body.get("args", {}) or {}
    if not isinstance(args_raw, dict):
        raise JobsConfigError(f"{source}: profile `{profile}` task #{idx}.args must be a mapping")
    args = {str(k): str(v) for k, v in args_raw.items()}

    return Task(
        name=str(body["name"]),
        script=str(body["script"]),
        args=args,
        enabled=bool(body.get("enabled", True)),
    )


def _check_keys(body: dict, required: tuple[str, ...], allowed: tuple[str, ...], where: str, source: Path) -> None:
    missing = [k for k in required if k not in body]
    if missing:
        raise JobsConfigError(f"{source}: {where} missing required keys: {', '.join(missing)}")
    unknown = [k for k in body if k not in allowed]
    if unknown:
        raise JobsConfigError(f"{source}: {where} has unknown keys: {', '.join(unknown)}")
