/* Win32 GUI conformance fixture.
 *
 * A real Windows program, compiled to an x86-64 PE and run through Porte's
 * compatibility runtime. It is not a SwiftUI reproduction: every widget below
 * is created by user32/gdi32 inside the Windows program, and the only thing
 * macOS sees is whatever window the runtime presents.
 *
 * Surface exercised (fixtures/win32/probe.c drives each one):
 *   window        WS_OVERLAPPEDWINDOW, real message loop
 *   label         STATIC, text changed by the button
 *   button        BUTTON, BM_CLICK and real mouse clicks
 *   keyboard      EDIT control receiving WM_CHAR from the message queue
 *   file dialog   comdlg32 GetOpenFileNameW, opens and reads a file
 *   file write    File > Save writes the edit control back to that file
 *   clipboard     File > Copy / File > Paste via OpenClipboard
 *   menu          File / Edit / Help menu bar
 *   termination   WM_CLOSE -> DestroyWindow -> PostQuitMessage(0)
 *
 * Build: fixtures/win32/build.sh (mingw-w64, -mwindows).
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <commdlg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define IDC_LABEL   1001
#define IDC_INPUT   1002
#define IDC_BUTTON  1003
#define IDC_STATUS  1004

#define IDM_FILE_OPEN   2001
#define IDM_FILE_SAVE   2002
#define IDM_FILE_EXIT   2003
#define IDM_EDIT_COPY   2004
#define IDM_EDIT_PASTE  2005
#define IDM_HELP_ABOUT  2006

#define CLASS_NAME "PorteWin32Fixture"
#define WINDOW_TITLE "Porte Win32 Fixture"

static HWND g_label;
static HWND g_button;
static HWND g_input;
static HWND g_status;
static char g_open_path[MAX_PATH];
static char g_file_dialog_dir[MAX_PATH];

static void set_status(const char *text)
{
    SetWindowTextA(g_status, text);
}

/* Replace the whole edit control contents, the way a document viewer would. */
static void set_input(const char *text)
{
    SetWindowTextA(g_input, text);
}

static char *read_file(const char *path, DWORD *length)
{
    HANDLE handle = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                               OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    DWORD size, read = 0;
    char *buffer;

    if (handle == INVALID_HANDLE_VALUE) return NULL;
    size = GetFileSize(handle, NULL);
    if (size == INVALID_FILE_SIZE || size > 1024 * 1024) {
        CloseHandle(handle);
        return NULL;
    }
    buffer = (char *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, size + 1);
    if (!buffer) {
        CloseHandle(handle);
        return NULL;
    }
    if (!ReadFile(handle, buffer, size, &read, NULL)) {
        HeapFree(GetProcessHeap(), 0, buffer);
        CloseHandle(handle);
        return NULL;
    }
    buffer[read] = 0;
    CloseHandle(handle);
    if (length) *length = read;
    return buffer;
}

static BOOL write_file(const char *path, const char *text, DWORD length)
{
    HANDLE handle = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                               FILE_ATTRIBUTE_NORMAL, NULL);
    DWORD written = 0;
    BOOL ok;

    if (handle == INVALID_HANDLE_VALUE) return FALSE;
    ok = WriteFile(handle, text, length, &written, NULL);
    CloseHandle(handle);
    return ok && written == length;
}

/* Load a document into the edit control, remembering where it came from so
 * File > Save can round-trip it. */
static void open_document(const char *path)
{
    DWORD length = 0;
    char *text = read_file(path, &length);
    char status[MAX_PATH + 32];

    if (!text) {
        snprintf(status, sizeof status, "cannot read: %s", path);
        set_status(status);
        return;
    }
    strncpy(g_open_path, path, sizeof g_open_path - 1);
    set_input(text);
    snprintf(status, sizeof status, "opened %s (%lu bytes)", path,
             (unsigned long)length);
    set_status(status);
    HeapFree(GetProcessHeap(), 0, text);
}

static void do_open_dialog(HWND window)
{
    OPENFILENAMEA ofn;
    char file[MAX_PATH];
    char initial[MAX_PATH];

    ZeroMemory(&ofn, sizeof ofn);
    ZeroMemory(file, sizeof file);
    if (g_file_dialog_dir[0]) {
        snprintf(initial, sizeof initial, "%s", g_file_dialog_dir);
        ofn.lpstrInitialDir = initial;
    }
    ofn.lStructSize = sizeof ofn;
    ofn.hwndOwner = window;
    ofn.lpstrFilter = "Text files\0*.txt\0All files\0*.*\0";
    ofn.lpstrFile = file;
    ofn.nMaxFile = sizeof file;
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_EXPLORER;

    if (GetOpenFileNameA(&ofn)) {
        open_document(file);
    } else {
        char status[MAX_PATH + 64];
        snprintf(status, sizeof status, "open cancelled (error %lu)",
                 (unsigned long)CommDlgExtendedError());
        set_status(status);
    }
}

static void do_save(HWND window)
{
    char text[8192];
    int length;

    if (!g_open_path[0]) {
        set_status("nothing to save: open a file first");
        return;
    }
    length = GetWindowTextA(g_input, text, sizeof text);
    if (write_file(g_open_path, text, (DWORD)length)) {
        char status[MAX_PATH + 32];
        snprintf(status, sizeof status, "saved %ld bytes to %s",
                 (long)length, g_open_path);
        set_status(status);
    } else {
        set_status("save failed");
    }
}

static void do_copy(HWND window)
{
    int length = GetWindowTextLengthA(g_input);
    HGLOBAL block;
    char *target;

    if (length <= 0) {
        set_status("nothing to copy");
        return;
    }
    if (!OpenClipboard(window)) {
        set_status("OpenClipboard failed");
        return;
    }
    EmptyClipboard();
    block = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, (SIZE_T)length + 1);
    if (!block) {
        CloseClipboard();
        set_status("clipboard allocation failed");
        return;
    }
    target = (char *)GlobalLock(block);
    GetWindowTextA(g_input, target, length + 1);
    GlobalUnlock(block);
    if (SetClipboardData(CF_TEXT, block)) {
        char status[64];
        snprintf(status, sizeof status, "copied %d bytes", length);
        set_status(status);
    } else {
        GlobalFree(block);
        set_status("SetClipboardData failed");
    }
    CloseClipboard();
}

static void do_paste(HWND window)
{
    HANDLE handle;
    const char *text;

    if (!OpenClipboard(window)) {
        set_status("OpenClipboard failed");
        return;
    }
    handle = GetClipboardData(CF_TEXT);
    if (!handle) {
        CloseClipboard();
        set_status("clipboard is empty");
        return;
    }
    text = (const char *)GlobalLock(handle);
    if (text) {
        int length = GetWindowTextLengthA(g_input);
        SendMessageA(g_input, EM_SETSEL, (WPARAM)length, (LPARAM)length);
        SendMessageA(g_input, EM_REPLACESEL, TRUE, (LPARAM)text);
        GlobalUnlock(handle);
        set_status("pasted from clipboard");
    }
    CloseClipboard();
}

static void say_hello(void)
{
    int length = GetWindowTextLengthA(g_input);

    SetWindowTextA(g_label, "Hello from Win32");
    SendMessageA(g_input, EM_SETSEL, (WPARAM)length, (LPARAM)length);
    SendMessageA(g_input, EM_REPLACESEL, TRUE, (LPARAM)"button-clicked\n");
    set_status("button clicked");
}

static void build_menu(HWND window)
{
    HMENU bar = CreateMenu();
    HMENU file = CreatePopupMenu();
    HMENU edit = CreatePopupMenu();
    HMENU help = CreatePopupMenu();

    AppendMenuA(file, MF_STRING, IDM_FILE_OPEN, "&Open...\tCtrl+O");
    AppendMenuA(file, MF_STRING, IDM_FILE_SAVE, "&Save\tCtrl+S");
    AppendMenuA(file, MF_SEPARATOR, 0, NULL);
    AppendMenuA(file, MF_STRING, IDM_FILE_EXIT, "E&xit");
    AppendMenuA(edit, MF_STRING, IDM_EDIT_COPY, "&Copy\tCtrl+C");
    AppendMenuA(edit, MF_STRING, IDM_EDIT_PASTE, "&Paste\tCtrl+V");
    AppendMenuA(help, MF_STRING, IDM_HELP_ABOUT, "&About");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)file, "&File");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)edit, "&Edit");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)help, "&Help");
    SetMenu(window, bar);
}

static LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam,
                                    LPARAM lparam)
{
    switch (message) {
    case WM_CREATE:
        g_label = CreateWindowA("STATIC", "label text", WS_CHILD | WS_VISIBLE,
                                20, 20, 580, 24, window, (HMENU)IDC_LABEL,
                                NULL, NULL);
        g_input = CreateWindowA("EDIT", "",
                                WS_CHILD | WS_VISIBLE | WS_BORDER |
                                    WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL,
                                20, 50, 580, 260, window, (HMENU)IDC_INPUT,
                                NULL, NULL);
        g_button = CreateWindowA("BUTTON", "Say Hello",
                                 WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                                 20, 320, 120, 30, window, (HMENU)IDC_BUTTON,
                                 NULL, NULL);
        g_status = CreateWindowA("STATIC", "ready",
                                 WS_CHILD | WS_VISIBLE,
                                 150, 326, 450, 24, window,
                                 (HMENU)IDC_STATUS, NULL, NULL);
        build_menu(window);
        return 0;
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case IDC_BUTTON:
            say_hello();
            return 0;
        case IDM_FILE_OPEN:
            do_open_dialog(window);
            return 0;
        case IDM_FILE_SAVE:
            do_save(window);
            return 0;
        case IDM_FILE_EXIT:
            PostMessageA(window, WM_CLOSE, 0, 0);
            return 0;
        case IDM_EDIT_COPY:
            do_copy(window);
            return 0;
        case IDM_EDIT_PASTE:
            do_paste(window);
            return 0;
        case IDM_HELP_ABOUT:
            MessageBoxA(window,
                        "Porte Win32 conformance fixture.\n"
                        "Built with mingw-w64 as an x86-64 Windows PE.",
                        WINDOW_TITLE, MB_OK | MB_ICONINFORMATION);
            return 0;
        }
        return 0;
    case WM_CLOSE:
        DestroyWindow(window);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcA(window, message, wparam, lparam);
}

/* The probe passes the directory the file dialog should start in, so the test
 * does not depend on whatever Wine considers the current folder. */
static void parse_command_line(char **document, char **dialog_dir)
{
    const char *line = GetCommandLineA();
    int index = 0;

    /* Skip argv[0], which may be quoted. */
    if (*line == '"') {
        line++;
        while (*line && *line != '"') line++;
        if (*line) line++;
    } else {
        while (*line && *line != ' ') line++;
    }
    for (;;) {
        while (*line == ' ' || *line == '\t') line++;
        if (!*line) return;
        char buffer[MAX_PATH];
        int length = 0;
        BOOL quoted = (*line == '"');
        if (quoted) line++;
        while (*line && (quoted ? *line != '"' : *line != ' ' && *line != '\t')) {
            if (length < MAX_PATH - 1) buffer[length++] = *line;
            line++;
        }
        if (quoted && *line == '"') line++;
        buffer[length] = 0;
        if (strcmp(buffer, "--dialog-dir") == 0) {
            while (*line == ' ' || *line == '\t') line++;
            quoted = (*line == '"');
            if (quoted) line++;
            length = 0;
            while (*line && (quoted ? *line != '"' : *line != ' ' && *line != '\t')) {
                if (length < MAX_PATH - 1) g_file_dialog_dir[length++] = *line;
                line++;
            }
            g_file_dialog_dir[length] = 0;
        } else if (index == 0) {
            if (document) *document = _strdup(buffer);
            index++;
        }
        (void)dialog_dir;
    }
}

int WINAPI WinMain(HINSTANCE instance, HINSTANCE previous, LPSTR command_line,
                   int show)
{
    WNDCLASSA cls;
    HWND window;
    MSG message;
    char *document = NULL;

    (void)previous;
    (void)command_line;
    parse_command_line(&document, NULL);

    ZeroMemory(&cls, sizeof cls);
    cls.lpfnWndProc = window_proc;
    cls.hInstance = instance;
    cls.hCursor = LoadCursorA(NULL, (LPCSTR)IDC_ARROW);
    cls.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    cls.lpszClassName = CLASS_NAME;
    RegisterClassA(&cls);

    window = CreateWindowA(CLASS_NAME, WINDOW_TITLE, WS_OVERLAPPEDWINDOW,
                           CW_USEDEFAULT, CW_USEDEFAULT, 660, 420,
                           NULL, NULL, instance, NULL);
    if (!window) return 2;

    /* A document named on the command line is opened immediately: that is the
     * file-association path an installed bundle uses. */
    if (document) {
        open_document(document);
        free(document);
    }
    ShowWindow(window, show);
    UpdateWindow(window);

    while (GetMessageA(&message, NULL, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageA(&message);
    }
    return (int)message.wParam;
}
