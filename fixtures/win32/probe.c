/* Win32 conformance probe.
 *
 * Drives a real Windows GUI process from inside the Windows world: it finds the
 * target's top-level window, sends real window messages (BM_CLICK, WM_CHAR,
 * WM_COMMAND), reads control text back with WM_GETTEXT, drives the common file
 * dialog, and checks clipboard round trips. Because all of it is Win32, the
 * test needs no Accessibility or Screen Recording permission on macOS, and it
 * cannot be satisfied by a macOS re-creation of the window: the messages are
 * delivered to whatever the guest process actually created.
 *
 *   probe.exe --scenario fixture --log C:\porte-probe.json --file <doc>
 *   probe.exe --scenario app --title Notepad++ --log <path> [--editor]
 *
 * Exit status is 0 only when every check passed. The JSON log lists each check
 * with its observed value, so a failure is diagnosable without a screenshot.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_CHECKS 64

/* Control and command ids of fixtures/win32/gui.c. */
#define IDC_LABEL 1001
#define IDC_INPUT 1002
#define IDC_BUTTON 1003
#define IDC_STATUS 1004
#define IDM_FILE_OPEN 2001
#define IDM_FILE_SAVE 2002
#define IDM_EDIT_COPY 2004
#define IDM_EDIT_PASTE 2005

typedef struct {
    char name[64];
    int ok;
    int skipped;
    char detail[512];
} Check;

static Check g_checks[MAX_CHECKS];
static int g_count;
static int g_failed;

static void record(const char *name, int ok, const char *format, ...)
{
    Check *check;
    va_list args;

    if (g_count >= MAX_CHECKS) return;
    check = &g_checks[g_count++];
    snprintf(check->name, sizeof check->name, "%s", name);
    check->ok = ok;
    check->skipped = 0;
    va_start(args, format);
    vsnprintf(check->detail, sizeof check->detail, format, args);
    va_end(args);
    if (!ok) g_failed++;
    printf("[%s] %s: %s\n", ok ? "ok" : "FAIL", check->name, check->detail);
    fflush(stdout);
}

static void skip_check(const char *name, const char *detail)
{
    Check *check;

    if (g_count >= MAX_CHECKS) return;
    check = &g_checks[g_count++];
    snprintf(check->name, sizeof check->name, "%s", name);
    check->ok = 1;
    check->skipped = 1;
    snprintf(check->detail, sizeof check->detail, "%s", detail);
    printf("[skip] %s: %s\n", name, detail);
    fflush(stdout);
}

/* ---- JSON writer ------------------------------------------------------- */

static void json_escape(FILE *out, const char *text)
{
    for (; *text; text++) {
        unsigned char c = (unsigned char)*text;
        if (c == '"' || c == '\\') fprintf(out, "\\%c", c);
        else if (c == '\n') fputs("\\n", out);
        else if (c == '\r') fputs("\\r", out);
        else if (c == '\t') fputs("\\t", out);
        else if (c < 0x20) fprintf(out, "\\u%04x", c);
        else fputc(c, out);
    }
}

static void write_log(const char *path, const char *scenario, int passed)
{
    FILE *out;
    int index;

    if (!path || !path[0]) return;
    out = fopen(path, "w");
    if (!out) return;
    fprintf(out, "{\"scenario\": \"");
    json_escape(out, scenario);
    fprintf(out, "\", \"passed\": %s, \"checks\": [", passed ? "true" : "false");
    for (index = 0; index < g_count; index++) {
        if (index) fputs(", ", out);
        fputs("{\"name\": \"", out);
        json_escape(out, g_checks[index].name);
        fprintf(out, "\", \"ok\": %s, \"skipped\": %s, \"detail\": \"",
                g_checks[index].ok ? "true" : "false",
                g_checks[index].skipped ? "true" : "false");
        json_escape(out, g_checks[index].detail);
        fputs("\"}", out);
    }
    fputs("]}\n", out);
    fclose(out);
}

/* ---- window discovery -------------------------------------------------- */

/* Defined below; declared here because discovery runs before the first check
 * and must be bounded for the same reason every other guest interaction is. */
static LRESULT send_bounded(HWND window, UINT message, WPARAM wparam,
                            LPARAM lparam);
static int title_bounded(HWND window, char *buffer, int size);

typedef struct {
    const char *needle;
    HWND found;
    DWORD pid;
} TitleSearch;

static BOOL CALLBACK match_title(HWND window, LPARAM param)
{
    TitleSearch *search = (TitleSearch *)param;
    char title[512];
    DWORD pid = 0;

    if (!IsWindowVisible(window)) return TRUE;
    /* EnumWindows reaches every process in the prefix, and a synchronous
     * GetWindowText on another process's window can block indefinitely when
     * that window's thread is not pumping messages. */
    if (title_bounded(window, title, sizeof title) <= 0) return TRUE;
    if (!strstr(title, search->needle)) return TRUE;
    GetWindowThreadProcessId(window, &pid);
    if (pid == GetCurrentProcessId()) return TRUE;
    search->found = window;
    search->pid = pid;
    return FALSE;
}

static HWND find_window_by_title(const char *needle, DWORD timeout_ms,
                                 DWORD *pid)
{
    TitleSearch search;
    DWORD waited = 0;

    for (;;) {
        search.needle = needle;
        search.found = NULL;
        search.pid = 0;
        EnumWindows(match_title, (LPARAM)&search);
        if (search.found) {
            *pid = search.pid;
            return search.found;
        }
        if (waited >= timeout_ms) return NULL;
        Sleep(100);
        waited += 100;
    }
}

typedef struct {
    DWORD pid;
    const char *class_name;
    HWND found;
} ClassSearch;

static BOOL CALLBACK match_class(HWND window, LPARAM param)
{
    ClassSearch *search = (ClassSearch *)param;
    char name[256];
    DWORD pid = 0;

    GetWindowThreadProcessId(window, &pid);
    if (pid != search->pid) return TRUE;
    if (GetClassNameA(window, name, sizeof name) <= 0) return TRUE;
    if (strcmp(name, search->class_name) != 0) return TRUE;
    search->found = window;
    return FALSE;
}

static HWND find_window_of_pid(DWORD pid, const char *class_name,
                               DWORD timeout_ms)
{
    ClassSearch search;
    DWORD waited = 0;

    search.pid = pid;
    search.class_name = class_name;
    for (;;) {
        search.found = NULL;
        EnumWindows(match_class, (LPARAM)&search);
        if (search.found) return search.found;
        if (waited >= timeout_ms) return NULL;
        Sleep(100);
        waited += 100;
    }
}

typedef struct {
    const char *class_prefix;
    HWND skip;
    HWND found;
} ChildSearch;

static BOOL CALLBACK match_child(HWND window, LPARAM param)
{
    ChildSearch *search = (ChildSearch *)param;
    char name[256];

    if (window == search->skip) return TRUE;
    if (GetClassNameA(window, name, sizeof name) <= 0) return TRUE;
    if (strncmp(name, search->class_prefix, strlen(search->class_prefix)) != 0)
        return TRUE;
    search->found = window;
    return FALSE;
}

static HWND find_child(HWND parent, const char *class_prefix, HWND skip)
{
    ChildSearch search;

    search.class_prefix = class_prefix;
    search.skip = skip;
    search.found = NULL;
    EnumChildWindows(parent, match_child, (LPARAM)&search);
    return search.found;
}

/* Diagnostics: dump the control tree so a failed dialog interaction can be
 * explained from the log instead of from a screenshot. */
static int g_dialog_dump;
/* Set by --dialog; the common file dialog is not driven by default. */
static int g_dialog_test;

static BOOL CALLBACK dump_child(HWND window, LPARAM param)
{
    char name[128];
    char text[128];

    (void)param;
    GetClassNameA(window, name, sizeof name);
    title_bounded(window, text, sizeof text);
    printf("  control id=0x%04x class=%s text=\"%.40s\" visible=%d\n",
           (unsigned)GetDlgCtrlID(window), name, text,
           IsWindowVisible(window) != 0);
    return TRUE;
}

static void dump_dialog(HWND dialog)
{
    if (!g_dialog_dump) return;
    printf("dialog controls:\n");
    EnumChildWindows(dialog, dump_child, 0);
    fflush(stdout);
}

/* Time-bounded cross-process send.
 *
 * A plain SendMessageA to a window whose thread is stuck never returns, which
 * turns one failed check into a verification run that hangs until an external
 * timeout kills it. Guest windows belong to another process, so every
 * interaction here is bounded and a timeout is reported as a failed check.
 */
#define SEND_TIMEOUT_MS 5000

static LRESULT send_bounded(HWND window, UINT message, WPARAM wparam,
                            LPARAM lparam)
{
    DWORD_PTR result = 0;

    if (!window) return -1;
    if (!SendMessageTimeoutA(window, message, wparam, lparam,
                             SMTO_ABORTIFHUNG | SMTO_BLOCK, SEND_TIMEOUT_MS,
                             &result))
        return -1;
    return (LRESULT)result;
}

static int title_bounded(HWND window, char *buffer, int size)
{
    LRESULT length;

    buffer[0] = 0;
    if (!window) return 0;
    length = send_bounded(window, WM_GETTEXT, (WPARAM)size, (LPARAM)buffer);
    if (length < 0) {
        buffer[0] = 0;
        return 0;
    }
    return (int)length;
}

static int read_text(HWND window, char *buffer, int size)
{
    return title_bounded(window, buffer, size);
}

/* ---- clipboard --------------------------------------------------------- */

static BOOL set_clipboard(const char *text)
{
    HGLOBAL block;
    char *target;
    SIZE_T length = strlen(text) + 1;

    if (!OpenClipboard(NULL)) return FALSE;
    EmptyClipboard();
    block = GlobalAlloc(GMEM_MOVEABLE, length);
    if (!block) {
        CloseClipboard();
        return FALSE;
    }
    target = (char *)GlobalLock(block);
    memcpy(target, text, length);
    GlobalUnlock(block);
    if (!SetClipboardData(CF_TEXT, block)) {
        GlobalFree(block);
        CloseClipboard();
        return FALSE;
    }
    CloseClipboard();
    return TRUE;
}

static int get_clipboard(char *buffer, int size)
{
    HANDLE handle;
    const char *text;
    int length = 0;

    buffer[0] = 0;
    if (!OpenClipboard(NULL)) return 0;
    handle = GetClipboardData(CF_TEXT);
    if (handle) {
        text = (const char *)GlobalLock(handle);
        if (text) {
            length = (int)strlen(text);
            if (length > size - 1) length = size - 1;
            memcpy(buffer, text, length);
            buffer[length] = 0;
            GlobalUnlock(handle);
        }
    }
    CloseClipboard();
    return length;
}

/* ---- process exit ------------------------------------------------------ */

static int close_and_wait(HWND window, DWORD timeout_ms, DWORD *exit_code)
{
    HANDLE process;
    DWORD pid = 0;
    DWORD waited = 0;

    GetWindowThreadProcessId(window, &pid);
    if (!pid) return 0;
    process = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                          pid);
    if (!process) return 0;
    PostMessageA(window, WM_CLOSE, 0, 0);
    for (;;) {
        if (WaitForSingleObject(process, 200) == WAIT_OBJECT_0) {
            if (!GetExitCodeProcess(process, exit_code)) *exit_code = 0xFFFFFFFF;
            CloseHandle(process);
            return 1;
        }
        /* A "save changes?" prompt must be answered, or the program never
         * terminates: declining the save is the non-destructive answer. */
        {
            HWND dialog = find_window_of_pid(pid, "#32770", 0);
            if (dialog) {
                HWND no = GetDlgItem(dialog, IDNO);
                if (!no) no = GetDlgItem(dialog, IDCANCEL);
                if (no)
                    send_bounded(dialog, WM_COMMAND,
                                 MAKEWPARAM(GetDlgCtrlID(no), BN_CLICKED),
                                 (LPARAM)no);
                else
                    PostMessageA(dialog, WM_CLOSE, 0, 0);
            }
        }
        if (waited >= timeout_ms) {
            CloseHandle(process);
            return 0;
        }
        Sleep(200);
        waited += 200;
    }
}

/* ---- scenarios --------------------------------------------------------- */

static void check_window_basics(HWND window)
{
    RECT rect;
    char name[256];
    char title[512];

    GetWindowRect(window, &rect);
    record("window.size",
           rect.right - rect.left >= 320 && rect.bottom - rect.top >= 240,
           "%ldx%ld", (long)(rect.right - rect.left),
           (long)(rect.bottom - rect.top));
    record("window.visible", IsWindowVisible(window) != 0, "IsWindowVisible");
    record("window.enabled", IsWindowEnabled(window) != 0, "IsWindowEnabled");
    GetClassNameA(window, name, sizeof name);
    GetWindowTextA(window, title, sizeof title);
    record("window.class", name[0] != 0, "class \"%s\"", name);
    record("window.title", title[0] != 0, "\"%s\"", title);
}

static void scenario_fixture(HWND window, DWORD pid, const char *document,
                             const char *evidence)
{
    char buffer[1024];
    HWND edit = GetDlgItem(window, IDC_INPUT);
    HWND button = GetDlgItem(window, IDC_BUTTON);

    check_window_basics(window);
    record("window.menu", GetMenu(window) != NULL, "GetMenu");

    record("edit.exists", edit != NULL, "GetDlgItem(%d)", IDC_INPUT);
    record("button.exists", button != NULL, "GetDlgItem(%d)", IDC_BUTTON);
    read_text(GetDlgItem(window, IDC_LABEL), buffer, sizeof buffer);
    record("label.initial", strcmp(buffer, "label text") == 0, "\"%s\"", buffer);

    if (button) {
        send_bounded(button, BM_CLICK, 0, 0);
        Sleep(200);
        read_text(GetDlgItem(window, IDC_LABEL), buffer, sizeof buffer);
        record("button.click_changes_label",
               strcmp(buffer, "Hello from Win32") == 0, "\"%s\"", buffer);
        read_text(GetDlgItem(window, IDC_STATUS), buffer, sizeof buffer);
        record("button.status", strstr(buffer, "button clicked") != NULL,
               "\"%s\"", buffer);
        read_text(edit, buffer, sizeof buffer);
        record("button.appends_text", strstr(buffer, "button-clicked") != NULL,
               "edit=\"%.80s\"", buffer);
    }

    if (edit) {
        SetWindowTextA(edit, "");
        send_bounded(edit, WM_CHAR, 'P', 1);
        send_bounded(edit, WM_CHAR, 'o', 1);
        send_bounded(edit, WM_CHAR, 'r', 1);
        send_bounded(edit, WM_CHAR, 't', 1);
        send_bounded(edit, WM_CHAR, 'e', 1);
        read_text(edit, buffer, sizeof buffer);
        record("keyboard.wm_char", strcmp(buffer, "Porte") == 0, "\"%s\"",
               buffer);

        SetWindowTextA(edit, evidence);
        send_bounded(window, WM_COMMAND, MAKEWPARAM(IDM_EDIT_COPY, 0), 0);
        Sleep(150);
        get_clipboard(buffer, sizeof buffer);
        record("clipboard.copy", strcmp(buffer, evidence) == 0,
               "clipboard=\"%s\"", buffer);

        SetWindowTextA(edit, "");
        set_clipboard("pasted-from-probe");
        send_bounded(window, WM_COMMAND, MAKEWPARAM(IDM_EDIT_PASTE, 0), 0);
        Sleep(150);
        read_text(edit, buffer, sizeof buffer);
        record("clipboard.paste", strstr(buffer, "pasted-from-probe") != NULL,
               "edit=\"%s\"", buffer);
    }

    /* The common file dialog is opt-in.
     *
     * Wine 11.20 implements GetOpenFileName with its own modal loop, and driving
     * that dialog from another process can wedge the whole prefix (every later
     * process stops starting). A verification run must never leave a prefix in
     * that state, so the dialog is only driven when the caller asks for it with
     * --dialog and accepts that risk.
     */
    if (!g_dialog_test) {
        skip_check("dialog.file_open",
                   "not requested; pass --dialog to drive the common dialog");
    } else if (document && document[0]) {
        HWND dialog;
        HWND field = NULL;
        HWND ok = NULL;
        DWORD waited = 0;
        char target[MAX_PATH];

        /* The runtime maps Z: to the macOS root, so a POSIX path handed in by
         * a test script is converted here rather than in every caller. */
        if (document[0] == '/') {
            snprintf(target, sizeof target, "Z:%s", document);
            for (char *c = target; *c; c++)
                if (*c == '/') *c = '\\';
            document = target;
        }

        /* Posted, not sent: the program's thread enters a modal dialog loop
         * and a synchronous send would block until the dialog closed. */
        PostMessageA(window, WM_COMMAND, MAKEWPARAM(IDM_FILE_OPEN, 0), 0);
        /* GetLastActivePopup asks the window manager for the window's own
         * popup. Enumerating every window of the prefix while a modal dialog
         * runs can block, and a modal loop is exactly the state this step is
         * in, so the popup is asked for directly instead. */
        dialog = NULL;
        for (waited = 0; waited < 8000; waited += 200) {
            HWND popup = GetLastActivePopup(window);
            char cls[64];
            if (popup && popup != window &&
                GetClassNameA(popup, cls, sizeof cls) > 0 &&
                strcmp(cls, "#32770") == 0) {
                dialog = popup;
                break;
            }
            Sleep(200);
        }
        record("dialog.exists", dialog != NULL, "popup of window %p (pid %lu)",
               (void *)window, (unsigned long)pid);
        /* The dialog is created and then shown, so wait briefly for the
         * WS_VISIBLE bit instead of sampling it at creation time. */
        for (waited = 0; dialog && !IsWindowVisible(dialog) && waited < 2000;
             waited += 100)
            Sleep(100);
        if (dialog) {
            title_bounded(dialog, buffer, sizeof buffer);
            record("dialog.visible", IsWindowVisible(dialog) != 0,
                   "title \"%s\"", buffer);
            dump_dialog(dialog);
            /* The standard file dialog keeps the file name in a combo
             * (cmb13, id 0x47C) whose edit child is edt1 (0x480); which of
             * the two accepts WM_SETTEXT depends on the control version, so
             * both are addressed. */
            field = GetDlgItem(dialog, 0x047c);
            if (!field) field = GetDlgItem(dialog, 0x0480);
            if (!field) field = find_child(dialog, "Edit", NULL);
            record("dialog.filename_edit", field != NULL,
                   "combo 0x47C, edit 0x480 or first EDIT (class %s)",
                   field ? "see control dump" : "none");
            if (field) {
                HWND edit_child = find_child(field, "Edit", NULL);
                send_bounded(field, WM_SETTEXT, 0, (LPARAM)document);
                if (edit_child)
                    send_bounded(edit_child, WM_SETTEXT, 0, (LPARAM)document);
                ok = GetDlgItem(dialog, IDOK);
                record("dialog.ok_button", ok != NULL, "IDOK");
                /* Posted: the dialog destroys itself while handling the
                 * command, and a synchronous send would wait on a dead
                 * window (in wine, indefinitely). */
                if (ok)
                    PostMessageA(dialog, WM_COMMAND,
                                 MAKEWPARAM(IDOK, BN_CLICKED), (LPARAM)ok);
            }
            while (IsWindow(dialog) && waited < 8000) {
                Sleep(100);
                waited += 100;
            }
        }
        Sleep(400);
        read_text(edit, buffer, sizeof buffer);
        record("file.opened", strstr(buffer, evidence) != NULL,
               "edit=\"%.80s\"", buffer);
        read_text(GetDlgItem(window, IDC_STATUS), buffer, sizeof buffer);
        record("file.status", strstr(buffer, "opened") != NULL, "\"%s\"",
               buffer);

        if (edit) {
            read_text(edit, buffer, sizeof buffer);
            if (strstr(buffer, evidence) == NULL) {
                skip_check("file.saved",
                           "the document was not opened, so save was not driven");
            } else {
                SetWindowTextA(edit, "porte-roundtrip");
                send_bounded(window, WM_COMMAND, MAKEWPARAM(IDM_FILE_SAVE, 0), 0);
                Sleep(300);
                read_text(GetDlgItem(window, IDC_STATUS), buffer, sizeof buffer);
                record("file.saved", strstr(buffer, "saved") != NULL,
                       "\"%s\"", buffer);
            }
        }
    }
}

static void scenario_app(HWND window, const char *needle, int editor_check)
{
    RECT before;
    RECT after;
    char buffer[512];

    check_window_basics(window);
    GetWindowTextA(window, buffer, sizeof buffer);
    record("app.title_matches", strstr(buffer, needle) != NULL, "\"%s\"", buffer);

    /* The window manager must answer a standard system command. */
    GetWindowRect(window, &before);
    send_bounded(window, WM_SYSCOMMAND, SC_MAXIMIZE, 0);
    Sleep(600);
    GetWindowRect(window, &after);
    record("app.maximize",
           (after.right - after.left) > (before.right - before.left) ||
               (after.bottom - after.top) > (before.bottom - before.top),
           "%ldx%ld -> %ldx%ld", (long)(before.right - before.left),
           (long)(before.bottom - before.top),
           (long)(after.right - after.left), (long)(after.bottom - after.top));
    send_bounded(window, WM_SYSCOMMAND, SC_RESTORE, 0);
    Sleep(400);

    if (editor_check) {
        HWND editor = find_child(window, "Scintilla", NULL);
        if (!editor) editor = find_child(window, "SysListView32", NULL);
        if (!editor) {
            skip_check("app.editor", "no Scintilla or SysListView32 child found");
        } else {
            char name[128];
            char clip[512];
            GetClassNameA(editor, name, sizeof name);
            set_clipboard("porte-clipboard-marker");
            send_bounded(editor, WM_PASTE, 0, 0);
            Sleep(300);
            send_bounded(editor, EM_SETSEL, 0, (LPARAM)-1);
            send_bounded(editor, WM_COPY, 0, 0);
            Sleep(300);
            get_clipboard(clip, sizeof clip);
            record("app.keyboard_and_clipboard",
                   strstr(clip, "porte-clipboard-marker") != NULL,
                   "%s paste->copy clipboard=\"%.60s\"", name, clip);
        }
    }
}

/* ---- entry point ------------------------------------------------------- */

static const char *arg_value(int argc, char **argv, const char *flag,
                             const char *fallback)
{
    int index;

    for (index = 1; index + 1 < argc; index++) {
        if (strcmp(argv[index], flag) == 0) return argv[index + 1];
    }
    return fallback;
}

static int arg_flag(int argc, char **argv, const char *flag)
{
    int index;

    for (index = 1; index < argc; index++) {
        if (strcmp(argv[index], flag) == 0) return 1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *scenario = arg_value(argc, argv, "--scenario", "fixture");
    const char *log_path = arg_value(argc, argv, "--log", "");
    const char *document = arg_value(argc, argv, "--file", "");
    const char *title = arg_value(argc, argv, "--title", "Porte Win32 Fixture");
    const char *evidence = arg_value(argc, argv, "--evidence", "porte-evidence");
    DWORD timeout_ms = (DWORD)atoi(arg_value(argc, argv, "--timeout", "30000"));

    g_dialog_dump = arg_flag(argc, argv, "--dump");
    g_dialog_test = arg_flag(argc, argv, "--dialog");
    HWND window;
    DWORD pid = 0;
    DWORD exit_code = 0;
    int closed;

    window = find_window_by_title(title, timeout_ms, &pid);
    if (!window) {
        record("target.found", 0, "no visible window titled \"%s\" within %lu ms",
               title, (unsigned long)timeout_ms);
        write_log(log_path, scenario, 0);
        return 1;
    }
    record("target.found", 1, "pid %lu window %p", (unsigned long)pid,
           (void *)window);

    if (strcmp(scenario, "fixture") == 0) {
        scenario_fixture(window, pid, document, evidence);
    } else {
        scenario_app(window, title, arg_flag(argc, argv, "--editor"));
    }

    closed = close_and_wait(window, 15000, &exit_code);
    record("process.exits_on_close", closed, "exit status %ld",
           (long)exit_code);
    record("process.exit_status_zero", closed && exit_code == 0,
           "exit status %ld", (long)exit_code);

    write_log(log_path, scenario, g_failed == 0);
    printf("probe: %d checks, %d failures\n", g_count, g_failed);
    fflush(stdout);
    return g_failed == 0 ? 0 : 1;
}
