"""Load and validate ``monitoring.yaml``.

``monitoring.yaml`` is the source of truth for the protocol cards shown on the
monitoring website. It describes what each protocol monitors, how often, and
which scripts implement those checks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parent.parent))
DEFAULT_MONITORING_CONFIG_PATH: Path = REPO_ROOT / "monitoring.yaml"


class MonitoringConfigError(ValueError):
    """Raised when monitoring.yaml is malformed or violates the schema."""


@dataclass(frozen=True)
class Monitor:
    """A single check shown on a protocol card."""

    name: str
    description: str
    severity: str | None = None

    def __post_init__(self) -> None:
        if self.severity is not None and self.severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            raise MonitoringConfigError(f"invalid severity {self.severity!r}")


@dataclass(frozen=True)
class ProtocolMonitoring:
    """Card metadata for one monitored protocol."""

    slug: str
    display_name: str
    description: str
    cadence: str
    tasks: tuple[str, ...]
    monitors: tuple[Monitor, ...]
    disabled: bool = False

    @property
    def monitor_count(self) -> int:
        return len(self.monitors)


@dataclass(frozen=True)
class MonitoringConfig:
    """Top-level container for monitoring card metadata."""

    version: str
    protocols: dict[str, ProtocolMonitoring]
    path: Path

    @property
    def sorted_protocols(self) -> list[ProtocolMonitoring]:
        return [self.protocols[k] for k in sorted(self.protocols)]


def _require_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise MonitoringConfigError(f"{where} must be a non-empty string")
    return value


def _parse_monitor(protocol: str, idx: int, body: Any) -> Monitor:
    if not isinstance(body, dict):
        raise MonitoringConfigError(f"protocol `{protocol}` monitor #{idx} must be a mapping")
    for key in ("name", "description"):
        if key not in body:
            raise MonitoringConfigError(f"protocol `{protocol}` monitor #{idx} missing `{key}`")
    name = _require_string(body["name"], f"protocol `{protocol}` monitor #{idx} name")
    description = _require_string(body["description"], f"protocol `{protocol}` monitor #{idx} description")
    severity = body.get("severity")
    if severity is not None:
        severity = _require_string(severity, f"protocol `{protocol}` monitor #{idx} severity")
    return Monitor(name=name, description=description, severity=severity)


def _parse_protocol(slug: str, body: Any, source: Path) -> ProtocolMonitoring:
    if not isinstance(body, dict):
        raise MonitoringConfigError(f"{source}: protocol `{slug}` must be a mapping")

    required = ("display_name", "cadence", "tasks", "monitors")
    missing = [k for k in required if k not in body]
    if missing:
        raise MonitoringConfigError(f"{source}: protocol `{slug}` missing keys: {', '.join(missing)}")

    display_name = _require_string(body["display_name"], f"protocol `{slug}` display_name")
    cadence = _require_string(body["cadence"], f"protocol `{slug}` cadence")
    description = _require_string(body.get("description", ""), f"protocol `{slug}` description")

    tasks_raw = body.get("tasks", [])
    if not isinstance(tasks_raw, list):
        raise MonitoringConfigError(f"{source}: protocol `{slug}` tasks must be a list")
    tasks = tuple(_require_string(t, f"protocol `{slug}` task") for t in tasks_raw)

    monitors_raw = body["monitors"]
    if not isinstance(monitors_raw, list) or not monitors_raw:
        raise MonitoringConfigError(f"{source}: protocol `{slug}` monitors must be a non-empty list")
    monitors = tuple(_parse_monitor(slug, idx, m) for idx, m in enumerate(monitors_raw))

    disabled = bool(body.get("disabled", False))

    return ProtocolMonitoring(
        slug=slug,
        display_name=display_name,
        description=description,
        cadence=cadence,
        tasks=tasks,
        monitors=monitors,
        disabled=disabled,
    )


def load_monitoring_config(path: Path | str | None = None) -> MonitoringConfig:
    """Load and validate ``monitoring.yaml`` from ``path``.

    Args:
        path: Path to the YAML file. Defaults to ``monitoring.yaml`` in the
            repository root.

    Returns:
        Parsed and validated monitoring configuration.
    """
    config_path = Path(path) if path is not None else DEFAULT_MONITORING_CONFIG_PATH
    if not config_path.exists():
        raise MonitoringConfigError(f"monitoring.yaml not found at {config_path}")

    with config_path.open("r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise MonitoringConfigError(f"{config_path}: top-level mapping expected")
    if "protocols" not in raw:
        raise MonitoringConfigError(f"{config_path}: top-level `protocols:` key missing")

    version = _require_string(raw.get("version", ""), "version")
    protocols_raw = raw["protocols"]
    if not isinstance(protocols_raw, dict) or not protocols_raw:
        raise MonitoringConfigError(f"{config_path}: `protocols:` must be a non-empty mapping")

    protocols: dict[str, ProtocolMonitoring] = {}
    for slug, body in protocols_raw.items():
        protocols[str(slug)] = _parse_protocol(str(slug), body, config_path)

    return MonitoringConfig(version=version, protocols=protocols, path=config_path)


def protocol_to_json(protocol: ProtocolMonitoring) -> dict[str, object]:
    """Serialize a protocol's card metadata to a JSON-friendly dict."""
    return {
        "slug": protocol.slug,
        "display_name": protocol.display_name,
        "description": protocol.description,
        "cadence": protocol.cadence,
        "monitor_count": protocol.monitor_count,
        "disabled": protocol.disabled,
        "tasks": list(protocol.tasks),
        "monitors": [
            {
                "name": m.name,
                "description": m.description,
                **({"severity": m.severity} if m.severity is not None else {}),
            }
            for m in protocol.monitors
        ],
    }


def monitoring_to_json(config: MonitoringConfig) -> dict[str, object]:
    """Serialize the full monitoring config for API consumers."""
    data = [protocol_to_json(p) for p in config.sorted_protocols]
    return {"version": config.version, "data": data, "count": len(data)}
