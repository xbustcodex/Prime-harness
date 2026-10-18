#!/usr/bin/env python3
"""Package the Prime Harness native Windows desktop application.

Build (in the project virtual environment, on Windows x64):

    python -m pip install -e ".[windows,desktop,packaging]"
    python build_exe.py

Produces: dist/PrimeHarnessControlRoom.exe (single file, windowed, no console).

Runtime requirements on the target machine: Windows 10 1809+ or Windows 11
(ConPTY). Visual Studio is not required. Installed OMP / PrimePi are launched,
never modified.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
APP_NAME = "PrimeHarnessControlRoom"
ENTRY = PROJECT / "src" / "prime_harness" / "desktop_app.py"
REQUIRED_MODULES = ("PySide6", "pyte", "winpty")


def main() -> int:
    if sys.platform != "win32":
        print("Build the Windows executable on Windows (ConPTY is Windows-only).")
        return 1
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        print(f"Missing build prerequisites: {', '.join(missing)}")
        print('Install with: python -m pip install -e ".[windows,desktop,packaging]"')
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        f"--name={APP_NAME}",
        "--onefile",
        "--windowed",
        f"--paths={PROJECT / 'src'}",
        "--hidden-import=winpty",
        str(ENTRY),
    ]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=PROJECT, check=False)
    if result.returncode != 0:
        print(f"PyInstaller failed with exit code {result.returncode}")
        return result.returncode

    executable = PROJECT / "dist" / f"{APP_NAME}.exe"
    if not executable.is_file():
        print(f"Expected executable not produced: {executable}")
        return 1
    print(f"Build complete: {executable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
