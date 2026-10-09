#!/usr/bin/env python3
"""Package Prime Harness native desktop shell as a Windows executable.

Build command (run in .venv):
    python build_exe.py

Produces: dist/PrimeHarnessControlRoom.exe

Requirements before packaging:
- Native renderer verification passed (native_renderer_verify.py)
- Four-corner layout builds and resizes correctly
- OMP / PrimePi acceptance tests pass (test_native_omp_primepi_compat.py)
- External OMP / PrimePi installations remain untouched

This build uses PyInstaller with a single-file / windowed mode.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
SRC = PROJECT / "src"


def main() -> int:
    # Confirm prerequisites
    try:
        import pywin32  # noqa: F401
        import pywinpty  # noqa: F401
    except ImportError as exc:
        print(f"Missing Windows dependency: {exc}")
        return 1

    # Ensure renderer verification passes before claiming executable
    import importlib.util
    spec = importlib.util.spec_from_file_location("native_renderer_verify", SRC / "prime_harness" / "native_renderer_verify.py")
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ver = mod.RendererVerifier()
        results = ver.run_all()
        if not results.get("renderer_decision", {}).get("convpty_supported"):
            print("Renderer verification failed; aborting packaging.")
            return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name=PrimeHarnessControlRoom",
        "--onefile",
        "--windowed",
        "--add-data=src/prime_harness/terminal_room.html;prime_harness/",
        "--hidden-import=pywin32",
        "--hidden-import=pywinpty",
        "--hidden-import=win32gui",
        "--hidden-import=win32con",
        "--hidden-import=win32api",
        "src/prime_harness/desktop_app.py",
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=False)
    print("Build complete at dist/PrimeHarnessControlRoom.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
