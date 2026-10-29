/* Metal renderer shared by every arm.
 *
 * One offscreen 1920x1080 colour target, one instanced draw per frame, shader
 * compiled at runtime (there is no `metal` CLI in this toolchain). Compilation
 * time is reported, because it is the shader stall component of the frame.
 *
 * No ARC: objects are stored in the renderer struct and live for the process,
 * which is what a frame loop does anyway.
 */
#import <Metal/Metal.h>
#import <Foundation/Foundation.h>
#include <time.h>
#include "renderer.h"

/* Three frames in flight, like a real present loop. Waiting every frame would
 * serialize CPU and GPU and inflate the measured frame time with a driver
 * round trip that no shipping renderer pays. */
#define FR_RING 3

struct frame_renderer {
    id<MTLDevice> device;
    id<MTLCommandQueue> queue;
    id<MTLRenderPipelineState> pipeline;
    id<MTLTexture> target;
    id<MTLCommandBuffer> pending[FR_RING];
    CFTimeInterval pending_start[FR_RING];
    int head;
    double compile_ms;
    double quad;
    int work;
};

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static NSString *const kShader = @"\n"
"#include <metal_stdlib>\n"
"using namespace metal;\n"
"struct VOut { float4 pos [[position]]; float4 color; };\n"
"vertex VOut vs_main(uint vid [[vertex_id]], uint iid [[instance_id]],\n"
"                    const device int4 *ents [[buffer(0)]],\n"
"                    constant float4 &p [[buffer(1)]]) {\n"
"    int4 e = ents[iid];\n"
"    float2 c = float2((vid & 1u) ? 1.0 : -1.0, ((vid >> 1) & 1u) ? 1.0 : -1.0);\n"
"    float2 w = float2(e.x, e.y) * (1.0 / 32768.0);\n"
"    VOut o;\n"
"    o.pos = float4((w + c * p.z) * p.x + p.y, 0.0, 1.0);\n"
"    o.color = float4(float(e.z & 0x3ff) / 1023.0, float(e.w & 0x3ff) / 1023.0,\n"
"                     float(e.x & 0xff) / 255.0, 1.0);\n"
"    return o;\n"
"}\n"
"fragment float4 fs_main(VOut in [[stage_in]], constant int &work [[buffer(2)]]) {\n"
"    float3 c = in.color.rgb;\n"
"    for (int i = 0; i < work; i++) {\n"
"        c = fract(c * 1.37 + float3(0.11, 0.23, 0.37));\n"
"    }\n"
"    return float4(c, 1.0);\n"
"}\n";

frame_renderer *fr_create(int width, int height, double quad, int work) {
    @autoreleasepool {
        frame_renderer *r = (frame_renderer *)calloc(1, sizeof(frame_renderer));
        r->device = MTLCreateSystemDefaultDevice();
        if (!r->device) { free(r); return 0; }
        r->queue = [r->device newCommandQueue];
        r->quad = quad;
        r->work = work;

        double t0 = now_ms();
        NSError *err = nil;
        /* A fresh nonce defeats the driver's shader cache, which is how a cold
         * compile is measured without deleting system caches. */
        NSString *source = kShader;
        const char *nonce = getenv("PORTE_SHADER_NONCE");
        if (nonce && *nonce) {
            source = [kShader stringByAppendingFormat:@"// nonce %s\n", nonce];
        }
        id<MTLLibrary> lib = [r->device newLibraryWithSource:source options:nil error:&err];
        if (!lib) {
            fprintf(stderr, "shader compile failed: %s\n",
                    err ? [[err localizedDescription] UTF8String] : "?");
            free(r);
            return 0;
        }
        MTLRenderPipelineDescriptor *pd = [[MTLRenderPipelineDescriptor alloc] init];
        pd.vertexFunction = [lib newFunctionWithName:@"vs_main"];
        pd.fragmentFunction = [lib newFunctionWithName:@"fs_main"];
        pd.colorAttachments[0].pixelFormat = MTLPixelFormatBGRA8Unorm;
        r->pipeline = [r->device newRenderPipelineStateWithDescriptor:pd error:&err];
        if (!r->pipeline) {
            fprintf(stderr, "pipeline failed: %s\n",
                    err ? [[err localizedDescription] UTF8String] : "?");
            free(r);
            return 0;
        }

        MTLTextureDescriptor *td = [MTLTextureDescriptor
            texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                         width:(NSUInteger)width
                                        height:(NSUInteger)height
                                     mipmapped:NO];
        td.usage = MTLTextureUsageRenderTarget;
        td.storageMode = MTLStorageModePrivate;
        r->target = [r->device newTextureWithDescriptor:td];
        r->compile_ms = now_ms() - t0;
        return r;
    }
}

void fr_destroy(frame_renderer *r) {
    if (!r) return;
    for (int i = 0; i < FR_RING; i++) {
        if (r->pending[i]) [r->pending[i] waitUntilCompleted];
    }
    free(r);
}

double fr_compile_ms(frame_renderer *r) {
    return r ? r->compile_ms : 0.0;
}

void fr_frame(frame_renderer *r, const void *verts, int instances, int frame,
              double *gpu_ms, double *encode_ms, double *wait_ms) {
    @autoreleasepool {
        if (gpu_ms) *gpu_ms = 0.0;
        if (encode_ms) *encode_ms = 0.0;
        if (wait_ms) *wait_ms = 0.0;
        if (!r || instances <= 0) return;
        double t0 = now_ms();
        int slot = r->head % FR_RING;
        if (r->pending[slot]) {
            [r->pending[slot] waitUntilCompleted];
            CFTimeInterval g0 = r->pending[slot].GPUStartTime;
            CFTimeInterval g1 = r->pending[slot].GPUEndTime;
            if (gpu_ms && g1 > g0) *gpu_ms = (g1 - g0) * 1000.0;
            [r->pending[slot] release];
            r->pending[slot] = nil;
        }
        if (wait_ms) *wait_ms = now_ms() - t0;
        double t_encode = now_ms();
        id<MTLBuffer> vb = [r->device newBufferWithBytes:verts
                                                 length:(NSUInteger)instances * 16u
                                                options:MTLResourceStorageModeShared];
        MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
        rp.colorAttachments[0].texture = r->target;
        rp.colorAttachments[0].loadAction = MTLLoadActionClear;
        rp.colorAttachments[0].storeAction = MTLStoreActionStore;
        rp.colorAttachments[0].clearColor = MTLClearColorMake(0.02, 0.03, 0.05, 1.0);
        id<MTLCommandBuffer> cb = [r->queue commandBuffer];
        id<MTLRenderCommandEncoder> enc = [cb renderCommandEncoderWithDescriptor:rp];
        [enc setRenderPipelineState:r->pipeline];
        [enc setVertexBuffer:vb offset:0 atIndex:0];
        float params[4] = {0.75f, (float)((frame & 7) - 4) * 0.01f,
                           (float)r->quad, 0.0f};
        [enc setVertexBytes:params length:sizeof(params) atIndex:1];
        int work = r->work;
        [enc setFragmentBytes:&work length:sizeof(work) atIndex:2];
        [enc drawPrimitives:MTLPrimitiveTypeTriangleStrip
                vertexStart:0
                vertexCount:4
              instanceCount:(NSUInteger)instances];
        [enc endEncoding];
        if (encode_ms) *encode_ms = now_ms() - t_encode;
        [cb commit];
        r->pending[slot] = [cb retain];
        r->head++;
    }
}
