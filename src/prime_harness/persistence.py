"""Persisted application state: profiles, panel configuration, supervisor history.

Restoring a panel means restoring its saved configuration (profile, folder).
It never implies that an earlier process is still running: child processes do
not survive application exit, so they are relaunched only on request.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from prime_harness.profiles import LaunchProfile, default_profiles

STATE_ENV = "PRIME_HARNESS_STATE"
SCHEMA_VERSION = 1


@dataclass
class PanelConfig:
    panel_id: str
    profile_id: str
    cwd: str = ""


@dataclass
class AppState:
    profiles: list[LaunchProfile] = field(default_factory=default_profiles)
    panels: list[PanelConfig] = field(default_factory=list)
    supervisor_history: list[str] = field(default_factory=list)
    default_cwd: str = ""


def default_state_path() -> Path:
    override = os.environ.get(STATE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "PrimeHarness" / "state.json"


def load_state(path: Path) -> AppState:
    """Load state; a missing file gives defaults and a corrupt file is set aside."""
    if not path.exists():
        return AppState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles = [LaunchProfile.from_dict(p) for p in data.get("profiles", [])]
        if not any(p.profile_id == "omp" for p in profiles):
            profiles = default_profiles() + profiles
        return AppState(
            profiles=profiles,
            panels=[PanelConfig(**p) for p in data.get("panels", [])],
            supervisor_history=[str(line) for line in data.get("supervisor_history", [])][-500:],
            default_cwd=str(data.get("default_cwd", "")),
        )
    except (ValueError, KeyError, TypeError):
        backup = path.with_suffix(".corrupt.json")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        return AppState()


def save_state(path: Path, state: AppState) -> None:
    """Atomic write: temp file in the same folder, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "profiles": [p.to_dict() for p in state.profiles],
        "panels": [vars(p) for p in state.panels],
        "supervisor_history": state.supervisor_history[-500:],
        "default_cwd": state.default_cwd,
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp, path)
