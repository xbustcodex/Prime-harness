from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AgentConfig:
    agent_id: str
    provider: str
    command: str
    workspace_path: Path
    timeout_seconds: int = 120
    environment: dict[str, str] | None = None
    model: str = "local-shell"


@dataclass(slots=True)
class ReviewerConfig:
    reviewer_id: str
    provider: str
    model: str
    endpoint: str | None = None
    timeout_seconds: int = 120


@dataclass(slots=True)
class HarnessSettings:
    data_path: Path = Path(".prime-harness.sqlite3")
    workspace_root: Path = Path.cwd()
    api_key: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    agent_provider: str = "shell"
    omp_executable: str = "omp"
