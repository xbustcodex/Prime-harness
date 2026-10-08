# Embedded Windows VT Terminal Renderer — Investigation

Status: blocker documented; no redistributable embedded control verified.

## Evidence checked

- `wt.exe` (Windows Terminal executable): **not installed** (`Get-Command` failed).
- Windows Terminal SDK / redistributable embedded control package: **not found** on system, in `.venv`, in Python package indexes (`pkgutil` shows only `winpty`, `_testconsole`), or as a NuGet reference.
- Windows Terminal repo (`github.com/microsoft/terminal`): source is C++ with DirectWrite/DirectX rendering engine (`conhost` + `CascadiaPackage`). README states the core can be reused as a UI control, but requires building `CascadiaPackage` in VS 2026 with WinUI workload, Windows 11 SDK 10.0.26100, .NET 4.7.2 targeting pack, and complex MSBuild setup (`OpenConsole.slnx`). There is **no standalone `.dll`/NuGet component** for embedding in a native desktop shell without source compilation.
- `pywinpty` 2.0.15 (`.venv\Scripts\python.exe`): provides `PtyProcess` / ConPTY session management only; **does not provide a visual VT renderer**.
- `desktop_app.py`: current panel uses `win32gui.DrawText` on a custom `WNDCLASS`. It performs no ANSI/VT parsing, no alternate-screen handling, no cursor positioning, no synchronized-output decoding. It is a **plain native desktop text widget**.

## Alternatives compared

| Approach | Availability | Redistribution | VT fidelity | Verdict |
|---|---|---|---|---|
| Windows Terminal SDK / embedded `TerminalControl` | Source only; requires VS 2026 + WinUI + SDK build | Not packaged; build from `main` | Full (DirectWrite + VT parser) | **Unavailable** for this harness |
| WPF / WinUI 3 custom parser + DirectX render | Possible if built with `libvterm`/C# bindings | Would require new C#/C++ component | Full if implemented | **Not implemented**; out of scope for Python harness |
| WebView / xterm.js (`terminal_room.html`) | Already available in repo | Browser dependency; violates native requirement | Full (xterm.js) | **Excluded** by spec (must be native, not browser) |
| `pywinpty` + `DrawText` (current) | Installed | Python package | None (plain text) | **Inadequate**; claims false compatibility |

## Conclusion

A genuine embedded Windows VT terminal renderer is **unavailable** in this environment as a redistributable component. The correct action is to **document the blocker**, keep the native desktop shell architecture (`desktop_app.py`) and session layer (`terminal_control.py`), and **not claim OMP/PrimePi visual compatibility** until a real embedded renderer (built from Terminal source or a validated native library) is integrated.

## Recommended path forward (not executed — blocked)

1. Build `CascadiaPackage` from `microsoft/terminal` source or obtain a validated embedded control binary.
2. Replace `DrawText` panel with the embedded `TerminalControl` (WPF/WinUI host window).
3. Re-run interactive OMP/PrimePi visual tests (menus, box drawing, resize, keyboard) inside the embedded renderer.
4. Only then package `.exe`.

All work stays inside Prime Harness; no OMP/PrimePi installations modified.
