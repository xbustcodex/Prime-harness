# Prime Harness: native Windows control room

Status: migration specification and first backend preparation. The native desktop renderer is NOT implemented yet. The existing browser prototype remains available for regression comparison, not the target application.

## Product contract

- Ship a real Windows desktop executable; no browser, web server, or internet connection required to use local terminals.
- Start with four independent CMD/ConPTY sessions, positioned top-left, top-right, bottom-left and bottom-right.
- Place a compact supervisor chat at the centre of the layout. It is a coordination UI, not an extra terminal.
- Provide an Add Terminal control at the side. Added terminals use free workspace and trigger a responsive reflow; there is no hardcoded six-terminal maximum.
- Every session has its own identity, pairing token, lifecycle and input/output stream. Resizing, expanding, collapsing or rearranging a panel must not restart its process.
- Users launch their existing OMP, PrimePi, OpenCode or other installed CLI themselves inside the CMD sessions.
- Optional remote access is a separately enabled authenticated bridge to the same terminal-session service, never a prerequisite for the desktop app.

## Native rendering decision gate

The desktop shell should use a supported native Windows UI stack (WinUI 3 or WPF). Before choosing a terminal-control implementation, prove an *embeddable* native renderer works with Windows ConPTY and OMP's advanced TUI. Microsoft Windows Terminal is a reference implementation, but its terminal control must not be assumed to be an independently supported redistributable component without verification. Launching wt.exe as separate windows or using a WebView/xterm.js as the main renderer does NOT satisfy the embedded native control requirement.

Do not substitute a plain TextBox/RichTextBox for a VT terminal emulator. The candidate renderer must handle UTF-8, ANSI/VT escape sequences, alternate screen, cursor positioning, autowrap, DEC synchronized output (2026), terminal capability replies, keyboard escape sequences, mouse input, and reflow.

## Layout and resize

Layout expansion is primarily a GUI action. Once a panel is measured, synchronize the associated ConPTY rows/columns with the renderer's actual cell grid. Keep the ConPTY process alive; debounce size notifications. Ensure small panels can expand to an OMP-usable viewport without changing the user's installed agent.

## OMP / PrimePi acceptance tests (Windows hardware)

1. Launch CMD and start OMP from inside the embedded terminal.
2. Verify initial screen, Unicode and box drawing, full-screen redraw, menus, prompt, cursor and keyboard controls.
3. Expand and restore the panel repeatedly; check geometry and scrollback.
4. Run four concurrent sessions, including OMP and PrimePi, without output corruption or dropped bytes.
5. Add a fifth session while four are running, then reflow and expand it without restarting any process.
6. Close/reopen the GUI according to explicit session-lifetime policy; do not silently kill builds.
7. Verify optional remote pairing cannot access other terminals and remains disabled by default.

## Existing code migration

- Keep `terminal_control.py` as an interim Python ConPTY/session prototype; it now defaults to four slots and can allocate more.
- The current `terminal_room.py` and `terminal_room.html` are legacy browser prototype surfaces, not the desktop application.
- Do not ship the current WebSocket fanout unchanged: it drops terminal chunks on a full queue and cannot guarantee faithful OMP redraws.
- Implement the desktop renderer and IPC bridge before claiming OMP/PrimePi compatibility.
- Package and test a Windows executable only after a real native terminal renderer passes the acceptance suite.

## Work order

1. Verify and select a redistributable embeddable native terminal renderer with a Windows OMP proof of concept.
2. Build the native desktop shell with four corners, small centre chat, expand/restore and Add Terminal.
3. Connect the renderer to the ConPTY session manager with lossless ordered transport and resize synchronization.
4. Add local IPC and an opt-in authenticated network bridge for external chat control.
5. Run Windows OMP/PrimePi acceptance tests, then produce a signed/unsigned installer as appropriate.
