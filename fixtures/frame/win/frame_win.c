/* Windows x86-64 frame loop with the same simulation as the native apps.
 *
 * Graphics submission is one imported call per frame, `PresentFrame`, served by
 * the Porte host (which drives the same Metal renderer the native arms use).
 * The PE is freestanding: no CRT, no D3D, no Wine. This measures the CPU path a
 * translated game would take into a graphics backend, not a real D3D runtime.
 */
typedef unsigned int u32;
typedef unsigned long long u64;

__declspec(dllimport) char *GetCommandLineA(void);
__declspec(dllimport) void *VirtualAlloc(void *addr, u64 size, u32 type, u32 prot);
__declspec(dllimport) int PresentFrame(void *ents, int n, int frame);
__declspec(dllimport) void ExitProcess(u32 code);

#include "../sim.h"

static u64 parse_arg(char *s, int index) {
    int seen = 0;
    while (*s) {
        if (*s == ' ') {
            seen++;
            if (seen == index) {
                s++;
                u64 v = 0;
                while (*s >= '0' && *s <= '9') { v = v * 10 + (u32)(*s - '0'); s++; }
                return v ? v : 1;
            }
        }
        s++;
    }
    return 0;
}

void porte_entry(void) {
    char *cmd = GetCommandLineA();
    int frames = (int)parse_arg(cmd, 1);
    int entities = (int)parse_arg(cmd, 2);
    int substeps = (int)parse_arg(cmd, 3);
    if (frames <= 0) frames = 120;
    if (entities <= 0) entities = FRAME_ENTITIES_DEFAULT;
    if (substeps <= 0) substeps = 1;

    frame_entity *ents = (frame_entity *)VirtualAlloc(
        0, (u64)entities * sizeof(frame_entity), 0x3000u, 0x04u);
    if (!ents) ExitProcess(2);
    frame_sim_init(ents, entities, 0x5EED1234u);

    for (int f = 0; f < frames; f++) {
        int32_t lx, ly;
        frame_light_for((u64)f, &lx, &ly);
        for (int s = 0; s < substeps; s++) {
            frame_sim_step(ents, entities, lx + s, ly - s);
        }
        PresentFrame(ents, entities, f);
    }
    ExitProcess(0);
}
