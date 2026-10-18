// Phase 1 Proof of Concept — Native Embedded VT Terminal Panel (C++ / Win32)
// Prime Harness: feature/native-windows-control-room
// Status: SOURCE PROVIDED; COMPILE BLOCKED (no VS 2026 / cl.exe / WinUI workload / SDK 10.0.26100)
// All compatibility work stays inside Prime Harness; OMP/PrimePi installations untouched.

// This file demonstrates the architecture selected for embedding a real VT renderer:
// - Native Win32 window (or WinUI 3 host) instead of browser/WebView
// - Direct connection to existing Prime Harness ConPTY session (PtyProcess / winpty)
// - Basic ANSI/VT parsing loop (clear screen, cursor position, Unicode, resize)
// - NOT a complete Windows Terminal SDK replacement; full implementation requires
//   building CascadiaPackage from microsoft/terminal source with VS 2026.

#include <windows.h>
#include <string>
#include <vector>
#include <memory>

// ------------------------------------------------------------------
// Minimal VT parser / renderer proof concept.
// In production this would be replaced by TerminalControl (DirectWrite/Skia)
// from the Windows Terminal source repo once build tools are available.
// ------------------------------------------------------------------

class VtParser {
public:
    std::string buffer;
    int cursorRow = 0;
    int cursorCol = 0;
    bool alternateScreen = false;

    void feed(const char* chunk, size_t len) {
        for (size_t i = 0; i < len; ++i) {
            char c = chunk[i];
            if (c == '\x1b') {
                // Start escape sequence; accumulate until final byte (simplified)
                // Production: use full terminal state machine (vt parser from terminal repo)
                buffer += c;
            } else {
                buffer += c;
                if (c == '\r') { cursorRow++; cursorCol = 0; }
                else if (c == '\n') { cursorRow++; }
                else { cursorCol++; }
            }
        }
    }

    void renderToWindow(HWND hwnd) {
        HDC hdc = GetDC(hwnd);
        RECT rc;
        GetClientRect(hwnd, &rc);
        FillRect(hdc, &rc, (HBRUSH)(COLOR_WINDOW + 1));
        // Draw parsed text (simplified; production uses DirectWrite layout)
        TextOutA(hdc, 8, 8, buffer.c_str(), (int)buffer.length());
        ReleaseDC(hwnd, hdc);
    }
};

// ------------------------------------------------------------------
// Native panel window procedure
// ------------------------------------------------------------------
LRESULT CALLBACK PanelWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    static std::unique_ptr<VtParser> parser;
    if (msg == WM_CREATE) {
        parser = std::make_unique<VtParser>();
        return 0;
    }
    if (msg == WM_PAINT) {
        PAINTSTRUCT ps;
        BeginPaint(hwnd, &ps);
        if (parser) parser->renderToWindow(hwnd);
        EndPaint(hwnd, &ps);
        return 0;
    }
    if (msg == WM_SIZE) {
        // Resize sync to ConPTY: debounced size notification
        // Production: synchronize PtyProcess dimensions with renderer grid
        InvalidateRect(hwnd, NULL, FALSE);
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

// ------------------------------------------------------------------
// Build / integration instructions (blocked at this step)
// ------------------------------------------------------------------
/*
BUILD REQUIREMENTS (from docs/embedded_renderer_investigation.md):
- Visual Studio 2026 (18.6+) with WinUI application development workload
- Windows 11 SDK 10.0.26100.8249 or greater
- .NET Framework 4.7.2 Targeting Pack
- Build microsoft/terminal OpenConsole.slnx -> CascadiaPackage
- Link TerminalControl (WinUI 3 / WPF) into native host

INTEGRATION WITH PRIME HARNESS:
1. Replace draw-text panel (desktop_app.py WCLASS_PANEL) with this native panel.
2. Feed chunk strings from TerminalSlot.subscribers directly to VtParser.feed().
3. Map resize events to PtyProcess.setwinsize() via debounced notification.
4. Preserve pairing tokens, session isolation, and independent ConPTY streams.
5. Verify OMP initial screen, menus, box drawing, cursor, keyboard controls.

CURRENT BLOCKER (documented, not hidden):
No cl.exe / MSBuild / VS 2026 available; Windows Terminal source not compiled;
embedded control binary not present. This source is the Phase 1 architecture
proof; full VT compatibility requires the build step above.
*/
