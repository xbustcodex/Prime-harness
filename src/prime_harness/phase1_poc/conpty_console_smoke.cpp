// Prime Harness Windows ConPTY executable smoke-test.
// Uses the Windows console's real VT renderer. This is NOT an embedded renderer
// and NOT the four-panel Prime Harness desktop application.
// Usage: prime-harness-terminal-poc.exe [command [arguments...]]
// Default: cmd.exe. Run OMP or PrimePi explicitly from the prompt or arguments.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <string>
#include <vector>
#include <thread>
#include <cstdio>
#include <algorithm>

using ClosePseudoConsoleFn = void (WINAPI*)(HPCON);
using CreatePseudoConsoleFn = HRESULT (WINAPI*)(COORD, HANDLE, HANDLE, DWORD, HPCON*);
using ResizePseudoConsoleFn = HRESULT (WINAPI*)(HPCON, COORD);

static std::wstring quote(const std::wstring& s) {
    if (s.find_first_of(L" \t\"") == std::wstring::npos) return s;
    std::wstring out = L"\"";
    size_t slash = 0;
    for (wchar_t c : s) {
        if (c == L'\\') { ++slash; continue; }
        if (c == L'"') { out.append(slash * 2 + 1, L'\\'); out += L'"'; slash = 0; continue; }
        out.append(slash, L'\\'); slash = 0; out += c;
    }
    out.append(slash * 2, L'\\');
    return out + L"\"";
}

int wmain(int argc, wchar_t** argv) {
    auto kernel = GetModuleHandleW(L"kernel32.dll");
    auto create = reinterpret_cast<CreatePseudoConsoleFn>(GetProcAddress(kernel, "CreatePseudoConsole"));
    auto close = reinterpret_cast<ClosePseudoConsoleFn>(GetProcAddress(kernel, "ClosePseudoConsole"));
    if (!create || !close) {
        fwprintf(stderr, L"ConPTY requires Windows 10 version 1809 or later.\n");
        return 2;
    }

    HANDLE inConsole = GetStdHandle(STD_INPUT_HANDLE);
    HANDLE outConsole = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD oldInputMode = 0, oldOutputMode = 0;
    if (!GetConsoleMode(inConsole, &oldInputMode) || !GetConsoleMode(outConsole, &oldOutputMode)) {
        fwprintf(stderr, L"Run this executable in an interactive Windows terminal.\n");
        return 2;
    }
    SetConsoleMode(outConsole, oldOutputMode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);
    // VT input converts keyboard keys to VT sequences for the child ConPTY.
    SetConsoleMode(inConsole, (oldInputMode | ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_EXTENDED_FLAGS) &
                             ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_QUICK_EDIT_MODE));

    HANDLE ptyInputRead = nullptr, ptyInputWrite = nullptr;
    HANDLE ptyOutputRead = nullptr, ptyOutputWrite = nullptr;
    auto fail = [&](const wchar_t* what) {
        fwprintf(stderr, L"%ls failed, Win32 error %lu\n", what, GetLastError());
        SetConsoleMode(inConsole, oldInputMode);
        SetConsoleMode(outConsole, oldOutputMode);
        return 1;
    };
    if (!CreatePipe(&ptyInputRead, &ptyInputWrite, nullptr, 0)) return fail(L"Input pipe");
    if (!CreatePipe(&ptyOutputRead, &ptyOutputWrite, nullptr, 0)) return fail(L"Output pipe");

    CONSOLE_SCREEN_BUFFER_INFO csbi{};
    GetConsoleScreenBufferInfo(outConsole, &csbi);
    COORD dimensions{static_cast<SHORT>(std::max(1, static_cast<int>(csbi.srWindow.Right - csbi.srWindow.Left + 1))),
                     static_cast<SHORT>(std::max(1, static_cast<int>(csbi.srWindow.Bottom - csbi.srWindow.Top + 1)))};
    HPCON pty = nullptr;
    HRESULT hr = create(dimensions, ptyInputRead, ptyOutputWrite, 0, &pty);
    CloseHandle(ptyInputRead);
    CloseHandle(ptyOutputWrite);
    if (FAILED(hr)) {
        CloseHandle(ptyInputWrite); CloseHandle(ptyOutputRead);
        fwprintf(stderr, L"CreatePseudoConsole failed: 0x%08lx\n", static_cast<unsigned long>(hr));
        SetConsoleMode(inConsole, oldInputMode); SetConsoleMode(outConsole, oldOutputMode);
        return 1;
    }

    STARTUPINFOEXW si{};
    si.StartupInfo.cb = sizeof(si);
    SIZE_T bytes = 0;
    InitializeProcThreadAttributeList(nullptr, 1, 0, &bytes);
    std::vector<unsigned char> attributeBuffer(bytes);
    si.lpAttributeList = reinterpret_cast<PPROC_THREAD_ATTRIBUTE_LIST>(attributeBuffer.data());
    if (!InitializeProcThreadAttributeList(si.lpAttributeList, 1, 0, &bytes) ||
        !UpdateProcThreadAttribute(si.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                   pty, sizeof(pty), nullptr, nullptr)) {
        close(pty); CloseHandle(ptyInputWrite); CloseHandle(ptyOutputRead);
        return fail(L"ConPTY process attributes");
    }

    std::wstring cmd = argc > 1 ? quote(argv[1]) : L"cmd.exe";
    for (int i = 2; i < argc; ++i) cmd += L" " + quote(argv[i]);
    std::vector<wchar_t> command(cmd.begin(), cmd.end());
    command.push_back(L'\0');
    PROCESS_INFORMATION pi{};
    BOOL launched = CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE,
                                   EXTENDED_STARTUPINFO_PRESENT, nullptr, nullptr,
                                   &si.StartupInfo, &pi);
    DeleteProcThreadAttributeList(si.lpAttributeList);
    if (!launched) {
        close(pty); CloseHandle(ptyInputWrite); CloseHandle(ptyOutputRead);
        return fail(L"CreateProcess");
    }

    // Output thread exits when ConPTY closes its output pipe.
    std::thread output([&] {
        char buffer[8192];
        DWORD n = 0;
        while (ReadFile(ptyOutputRead, buffer, sizeof(buffer), &n, nullptr) && n) {
            DWORD offset = 0;
            while (offset < n) {
                DWORD written = 0;
                if (!WriteFile(outConsole, buffer + offset, n - offset, &written, nullptr) || !written) return;
                offset += written;
            }
        }
    });
    // Input thread is detached: ReadFile on console can block after the child exits.
    // Process exit terminates it; do not close handles while it is still using them.
    std::thread input([=] {
        char buffer[4096];
        DWORD n = 0;
        while (ReadFile(inConsole, buffer, sizeof(buffer), &n, nullptr) && n) {
            DWORD offset = 0;
            while (offset < n) {
                DWORD written = 0;
                if (!WriteFile(ptyInputWrite, buffer + offset, n - offset, &written, nullptr) || !written) return;
                offset += written;
            }
        }
    });
    input.detach();

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD exitCode = 1;
    GetExitCodeProcess(pi.hProcess, &exitCode);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    close(pty);
    output.join();
    CloseHandle(ptyOutputRead);
    // Input thread may still be blocked on the console; handle is reclaimed at process exit.
    SetConsoleMode(inConsole, oldInputMode);
    SetConsoleMode(outConsole, oldOutputMode);
    return static_cast<int>(exitCode);
}
