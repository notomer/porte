/* Identical Metal renderer for every arm. C ABI so the same object file can be
 * linked into the native apps and loaded by ctypes in the Porte host. */
#ifndef PORTE_FRAME_RENDERER_H
#define PORTE_FRAME_RENDERER_H

typedef struct frame_renderer frame_renderer;

/* width/height in pixels, quad = half-size in NDC, work = fragment ALU loop. */
frame_renderer *fr_create(int width, int height, double quad, int work);
void fr_destroy(frame_renderer *r);

/* Milliseconds spent compiling the shader library and building the pipeline. */
double fr_compile_ms(frame_renderer *r);

/* Encode one instanced draw of `instances` int4 entities and wait for it.
 * verts must point at instances * 16 readable bytes (x,y,vx,vy int32).
 * Reports GPU milliseconds in *gpu_ms, CPU encode milliseconds in *encode_ms,
 * and milliseconds blocked waiting for a free ring slot in *wait_ms. The two
 * CPU components are separate because only one of them is work. */
void fr_frame(frame_renderer *r, const void *verts, int instances, int frame,
              double *gpu_ms, double *encode_ms, double *wait_ms);

#endif
