from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from prime_harness.api import create_app
from prime_harness.services import HarnessService
from prime_harness.stores import SqliteStore


def main() -> None:
    database = Path(os.environ.get("PRIME_HARNESS_DB", ".prime-harness.sqlite3")).resolve()
    workspace = Path(os.environ.get("PRIME_HARNESS_WORKSPACE", Path.cwd())).resolve()
    host = os.environ.get("PRIME_HARNESS_HOST", "127.0.0.1")
    port = int(os.environ.get("PRIME_HARNESS_PORT", "8000"))
    service = HarnessService(
        SqliteStore(database),
        workspace,
        default_agent_provider=os.environ.get("PRIME_HARNESS_AGENT_PROVIDER", "shell"),
        omp_executable=os.environ.get("PRIME_HARNESS_OMP_EXECUTABLE", "omp"),
    )
    uvicorn.run(
        create_app(service),
        host=host,
        port=port,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
