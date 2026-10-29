/* Second payload in the installer fixture: a Windows GUI program.
 *
 * Porte has no user32/GDI stubs, so this image is discovered by the installer
 * but cannot be bundled. Its presence keeps the "discover installed
 * executables" path honest: the installer reports what it found and exactly
 * which imports stopped it, instead of producing an app that fails at launch.
 */
typedef unsigned int u32;

__declspec(dllimport) int MessageBoxA(void *owner, const char *text,
                                      const char *caption, u32 type);

void porte_entry(void) {
    MessageBoxA(0, "Fixture with a window", "Porte fixture", 0);
}
