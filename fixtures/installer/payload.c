/* Payload shipped inside the installer fixtures.
 *
 * Small on purpose: it must be a real Windows PE that Porte can execute with the
 * host stubs it has (GetStdHandle/WriteFile/ExitProcess), so the fixture can
 * prove the whole path end to end - MSI parsed, files extracted, bundle built,
 * application launched, bytes printed by the Windows image.
 */
typedef unsigned int u32;

__declspec(dllimport) void *GetStdHandle(u32 which);
__declspec(dllimport) int WriteFile(void *handle, const void *buf, u32 length,
                                    u32 *written, void *overlapped);
__declspec(dllimport) void ExitProcess(u32 code);

void porte_entry(void) {
    static const char message[] =
        "PorteFixture: extracted by Porte, running as a Windows image\n";
    u32 written = 0;
    WriteFile(GetStdHandle(0xFFFFFFF5u), message, sizeof(message) - 1,
              &written, 0);
    ExitProcess(0);
}
