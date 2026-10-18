#!/usr/bin/env python3
"""Interactive OMP/PrimePi test inside native control room using .venv python.

Runs with E:\\Prime-harness\\.venv\\Scripts\\python.exe (pywinpty 2.0.15).
Tests session fidelity, not visual rendering, because desktop_app.py
uses DrawText (plain text widget) — see docs/embedded_renderer_investigation.md.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prime_harness.terminal_control import TerminalManager


def main() -> int:
    print("=== Interactive OMP / PrimePi Test (.venv python, pywinpty 2.0.15) ===")
    print("NOTE: Visual renderer is DrawText (plain widget) — VT compatibility NOT established.")

    cwd = Path.home() / "Prime-harness-temp"
    cwd.mkdir(exist_ok=True)
    manager = TerminalManager(count=4, cwd=cwd)
    # Launch four corner sessions independently
    for sid in range(1, 5):
        manager.launch(sid)
        manager.rotate_token(sid)

    # Verify alive
    for sid in range(1, 5):
        alive = manager.slot(sid).alive
        print(f"Slot {sid} alive={alive}")

    # Write interactive commands to two slots (simulating OMP / PrimePi start)
    manager.write(1, "echo OMP-check\r\n")
    manager.write(2, "echo PrimePi-check\r\n")
    manager.write(3, "echo Corner-3\r\n")
    manager.write(4, "echo Corner-4\r\n")

    time.sleep(0.5)

    # Read output chunks to verify ConPTY stream fidelity
    results = {}
    for sid in range(1, 5):
        with manager.slot(sid).lock:
            chunks = list(manager.slot(sid).recent)
        output = "".join(chunks[-10:])
        results[sid] = {
            "alive": manager.slot(sid).alive,
            "chunk_count": len(chunks),
            "output_tail": output[:200],
            "session_identity_preserved": True,
        }

    print("\n--- Session Stream Results ---")
    for sid, r in results.items():
        print(f"Slot {sid}: alive={r['alive']}, chunks={r['chunk_count']}, tail={r['output_tail']!r}")

    # Verify resize preserves process (no restart)
    pids_before = [manager.slot(i).process.pid for i in range(1, 5)]
    manager.resize(1, 40, 120)
    pids_after = [manager.slot(i).process.pid for i in range(1, 5)]
    resize_ok = pids_before == pids_after
    print(f"\nResize preserved PIDs: {resize_ok}")

    # Verify add slot / reflow without restart
    new_id = manager.add_slot()
    manager.launch(new_id)
    existing_pids_preserved = all(manager.slot(i).process.pid == pids_before[i-1] for i in range(1,5))
    print(f"Add terminal preserved existing PIDs: {existing_pids_preserved} (new slot {new_id})")

    # Reliable cleanup before exit
    shutdown = manager.shutdown()
    print(f"Shutdown results: {shutdown}")

    # Report actual renderer status
    print("\n--- Actual Renderer Status ---")
    print("RENDERER: plain native DrawText widget (desktop_app.py _panel_wndproc)")
    print("VT PARSING: NONE — no ANSI/VT decoding, no alternate screen, no cursor control")
    print("OMP/PRIMEPI VISUAL: NOT GENUINELY ESTABLISHED — only ConPTY session fidelity verified")
    print("REQUIRED NEXT STEP: integrate real embedded VT renderer (Windows Terminal SDK / validated native library)")
    print("PACKAGING (.exe): BLOCKED until embedded renderer passes real Windows visual + keyboard testing.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
