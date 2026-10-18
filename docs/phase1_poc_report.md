# Phase 1 — Proof of Concept: Native Embedded VT Terminal Renderer

Status: BLOCKED at build/environment level; precise limitation documented.

## 1. Source / component investigation

- Windows Terminal repo (`microsoft/terminal`): C++ `OpenConsole.slnx` / `CascadiaPackage`. Source contains reusable DirectWrite-based text layout/rendering engine, VT parser/emitter, and `TerminalControl` WPF/WinUI component.
- Licensing: MIT (compatible with Prime Harness).
- Distribution method: Microsoft Store `.msixbundle` / GitHub releases (`wt.exe`), **not** a standalone embedded `.dll` or NuGet package for third-party hosting.
- Embedding claim: README states core can be reused as a UI control, but requires building from source.

## 2. Embeddable library comparison

| Library / Component | Redistributable? | VT fidelity | Build needed | Verdict |
|---|---|---|---|---|
| Windows Terminal `TerminalControl` (source) | No (source only) | Full | VS 2026 + WinUI + SDK 10.0.26100 | **Blocked** |
| `pywinpty` / `winpty` (installed `.venv`) | Yes | Session only (ConPTY) | None | Session verified; no renderer |
| WebView2 / xterm.js | Yes (browser) | Full | None | **Excluded** by spec (must be native) |
| Custom GDI/DirectWrite parser | Possible in C++ | Partial/Full if built | C++ compiler + SDK | Blocked by missing compiler |

## 3. Build environment verification (Windows machine)

Checked via PowerShell / Bash:
- `wt.exe`: **not found**
- `msbuild.exe`: **not found**
- `cl.exe`: **not found** in SDK 10/8.1 paths
- VS 2026 / `C:\Program Files\Microsoft Visual Studio`: **not present**
- `.venv\Scripts\python.exe`: `pywinpty` 2.0.15 confirmed; no VT library (`pkgutil` shows only `winpty`, `_testconsole`)
- Windows 11 SDK 10.0.26100: **not installed** (only SDK 10 / 8.1 present)

**Precise technical limitation:** Integration of a real embedded Windows VT terminal renderer requires either (a) building `CascadiaPackage` from `microsoft/terminal` source with VS 2026, WinUI workload, and Windows 11 SDK 10.0.26100, or (b) obtaining a validated standalone embedded binary — neither is available in this environment.

## 4. Proof of concept — single terminal

Architecture selected: **Native Win32 window (C++ concept / Python `win32gui`) + existing `TerminalManager` ConPTY session**.

Implementation: `desktop_app.py` panel (`WCLASS_PANEL`) connects to `TerminalSlot(1)` via `_PanelQueue` subscriber (`terminal_control.py`). `PtyProcess.spawn(cmd, cwd=...)` delivers real ANSI/VT chunks (verified interactively).

Interactive test (`E:\Prime-harness\.venv\Scripts\python.exe scripts/interactive_omp_primepi_test.py`):
- Slot 1: `alive=True`, chunk count `1`, output tail contains real VT sequences: `\x1b[?9001h`, `\x1b[2J`, `\x1b[H`, `\x1b]0;C:\Windows\System32\cmd.exe\x07` (ConPTY initialization + screen clear + title set).
- This proves **session fidelity** and **ordered delivery**.
- Visual rendering: `DrawText` (plain text widget) — **not** a VT emulator.

## 5. Actual OMP / PrimePi rendering results

Interactive session delivers ANSI output, but `desktop_app.py` does **not** parse or render it correctly:
- No cursor positioning
- No alternate-screen handling
- No box-drawing / Unicode layout
- No keyboard input mapping through VT
- No synchronized-output decoding

Therefore: **OMP/PrimePi visual compatibility is NOT genuinely established**.

## 6. Integration plan (blocked at Phase 1)

Next steps (require resolved blocker):
1. Build / obtain embedded `TerminalControl` (WinUI 3 / WPF) from `microsoft/terminal`.
2. Replace `DrawText` panel with embedded control.
3. Connect embedded renderer directly to `TerminalSlot` via ordered chunk stream (already working; just needs correct visual output).
4. Verify OMP menus / box drawing / full-screen redraw inside embedded control.
5. Only then integrate 4 corners + centre chat + Add Terminal.

## 7. Remaining blockers

- **Critical:** No C++ compiler (`cl.exe`, `gcc`, `clang`) and no VS 2026 / WinUI workload.
- **Critical:** No Windows Terminal SDK / embedded control binary.
- **Medium:** `desktop_app.py` needs full VT parser replacement (not just `DrawText`).
- **Medium:** Real visual/keyboard testing of OMP/PrimePi requires embedded renderer.
- **Not blocked:** Session manager (`terminal_control.py`), layout (`DesktopLayout`), tokens, cleanup (`shutdown()`), interactive verification (`interactive_omp_primepi_test.py`).

## Deliverables provided

- `docs/embedded_renderer_investigation.md`
- `docs/phase1_poc_report.md` (this file)
- `src/prime_harness/terminal_control.py` (shutdown + session layer)
- `src/prime_harness/desktop_app.py` (native shell, DrawText clearly marked non-VT)
- `tests/test_windows_cleanup_regression.py`
- `scripts/interactive_omp_primepi_test.py` (live `.venv` test — session fidelity verified, visual blocked)
- `build_exe.py` (packaging deferred)

All within Prime Harness; no OMP/PrimePi installations modified.
