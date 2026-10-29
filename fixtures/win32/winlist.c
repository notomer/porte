/* List on-screen macOS windows for one process id.
 *
 * `wine` presents guest windows as real NSWindows owned by a real macOS process,
 * so the honest question "did a window actually appear?" is answered with the
 * WindowServer's own list, not with the guest's opinion of itself.
 *
 *   winlist <pid> [--min-width N] [--all] [--json]
 *
 * --all also lists windows the WindowServer does not consider on screen, which
 * is how a created-but-not-presented window shows up.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <CoreGraphics/CoreGraphics.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    pid_t pid;
    CFArrayRef windows;
    CFIndex count, index;
    int json = 0, matched = 0, min_width = 0, include_offscreen = 0;

    if (argc < 2) {
        fprintf(stderr, "usage: winlist <pid> [--min-width N] [--json]\n");
        return 2;
    }
    pid = (pid_t)atoi(argv[1]);
    for (index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--json") == 0) json = 1;
        if (strcmp(argv[index], "--all") == 0) include_offscreen = 1;
        if (strcmp(argv[index], "--min-width") == 0 && index + 1 < argc)
            min_width = atoi(argv[index + 1]);
    }

    windows = CGWindowListCopyWindowInfo(
        (include_offscreen ? kCGWindowListOptionAll
                           : kCGWindowListOptionOnScreenOnly) |
            kCGWindowListExcludeDesktopElements,
        kCGNullWindowID);
    if (!windows) {
        fprintf(stderr, "window list unavailable\n");
        return 1;
    }

    if (json) printf("[");
    count = CFArrayGetCount(windows);
    for (index = 0; index < count; index++) {
        CFDictionaryRef info = CFArrayGetValueAtIndex(windows, index);
        CFNumberRef owner_pid_ref, layer_ref, number_ref;
        CFDictionaryRef bounds;
        CFStringRef owner_name = NULL;
        double x = 0, y = 0, width = 0, height = 0;
        int owner_pid = 0, layer = 0, number = 0;
        char owner[256] = "";
        int onscreen = 0;

        owner_pid_ref = CFDictionaryGetValue(info, kCGWindowOwnerPID);
        if (!owner_pid_ref ||
            !CFNumberGetValue(owner_pid_ref, kCFNumberIntType, &owner_pid))
            continue;
        if (owner_pid != (int)pid) continue;

        bounds = CFDictionaryGetValue(info, kCGWindowBounds);
        if (bounds) {
            CFDictionaryGetValueIfPresent(bounds, CFSTR("X"), (const void **)&number_ref);
            if (number_ref) CFNumberGetValue(number_ref, kCFNumberDoubleType, &x);
            CFDictionaryGetValueIfPresent(bounds, CFSTR("Y"), (const void **)&number_ref);
            if (number_ref) CFNumberGetValue(number_ref, kCFNumberDoubleType, &y);
            CFDictionaryGetValueIfPresent(bounds, CFSTR("Width"), (const void **)&number_ref);
            if (number_ref) CFNumberGetValue(number_ref, kCFNumberDoubleType, &width);
            CFDictionaryGetValueIfPresent(bounds, CFSTR("Height"), (const void **)&number_ref);
            if (number_ref) CFNumberGetValue(number_ref, kCFNumberDoubleType, &height);
        }
        layer_ref = CFDictionaryGetValue(info, kCGWindowLayer);
        if (layer_ref) CFNumberGetValue(layer_ref, kCFNumberIntType, &layer);
        number_ref = (CFNumberRef)CFDictionaryGetValue(info, kCGWindowNumber);
        if (number_ref) CFNumberGetValue(number_ref, kCFNumberIntType, &number);
        {
            CFBooleanRef shown = CFDictionaryGetValue(info, kCGWindowIsOnscreen);
            onscreen = shown && CFBooleanGetValue(shown);
        }
        owner_name = CFDictionaryGetValue(info, kCGWindowOwnerName);
        if (owner_name && CFGetTypeID(owner_name) == CFStringGetTypeID())
            CFStringGetCString(owner_name, owner, sizeof owner,
                               kCFStringEncodingUTF8);

        if (width < min_width) continue;
        matched++;
        if (json) {
            printf("%s{\"pid\": %d, \"owner\": \"%s\", \"x\": %.0f, \"y\": %.0f, "
                   "\"width\": %.0f, \"height\": %.0f, \"layer\": %d, "
                   "\"number\": %d, \"onscreen\": %s}",
                   matched > 1 ? ", " : "", owner_pid, owner, x, y, width,
                   height, layer, number, onscreen ? "true" : "false");
        } else {
            printf("pid %d  owner \"%s\"  %.0fx%.0f at %.0f,%.0f  layer %d%s\n",
                   owner_pid, owner, width, height, x, y, layer,
                   onscreen ? "" : "  (offscreen)");
        }
    }
    if (json) printf("]\n");
    CFRelease(windows);
    if (!matched) {
        fprintf(stderr, "no on-screen window for pid %d\n", (int)pid);
        return 1;
    }
    return 0;
}
