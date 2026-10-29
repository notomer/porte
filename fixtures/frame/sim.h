/* Shared frame simulation. Compiled into every arm: native arm64, x86-64 under
 * Rosetta, and the Windows PE. Integer-only fixed point (Q16.16) so the work is
 * deterministic and, in the PE arm, inside the integer subset Porte can lift.
 *
 * Not a game: a bounded 2D particle field with per-entity integration, wall
 * reflection and a light falloff. It is the CPU half of a frame; the GPU half is
 * one instanced draw of the same entities in every arm.
 */
#ifndef PORTE_FRAME_SIM_H
#define PORTE_FRAME_SIM_H

#include <stdint.h>

#define FRAME_FIXED_ONE 65536
#define FRAME_WORLD 262144 /* 4.0 in Q16.16 */
#define FRAME_ENTITIES_DEFAULT 8192

typedef struct {
    int32_t x, y;       /* position, Q16.16 */
    int32_t vx, vy;     /* velocity, Q16.16 */
    int32_t light;      /* 0..262144, derived from distance to the light */
    int32_t tick;       /* frame counter, keeps the struct 32 bytes */
    int32_t pad0, pad1;
} frame_entity;

void frame_sim_init(frame_entity *e, int n, uint32_t seed);
void frame_sim_step(frame_entity *e, int n, int32_t light_x, int32_t light_y);
void frame_light_for(uint64_t frame, int32_t *lx, int32_t *ly);

#endif
