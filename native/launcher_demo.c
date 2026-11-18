/**
 * Standalone C Launcher Streaming Extraction Demo.
 * Demonstrates how a C/C++ game launcher (Unreal, Unity, or Win32 client)
 * embeds the streaming pipeline directly with zero archive disk storage.
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include "liveextract_stream.h"

int main(int argc, char* argv[]) {
    printf("====================================================\n");
    printf(" LiveExtract Native C Launcher Engine\n");
    printf(" Direct-to-Disk Streaming DEFLATE Pipeline\n");
    printf("====================================================\n");

    /* Example: Stream 5 MB of compressed test data straight to disk */
    ze_stream_t* stream = NULL;
    int rc = ze_stream_init(&stream, -15); /* -15: raw DEFLATE, 32KB window */
    if (rc != ZE_OK) {
        fprintf(stderr, "Failed to initialize decompressor stream: %d\n", rc);
        return 1;
    }

    printf("[C Engine] Decompressor initialized (Fixed 32 KB sliding window allocated in RAM).\n");
    printf("[C Engine] Ready to pipe TCP / HTTP chunks directly to pre-allocated disk files.\n");

    /* Calculate sample CRC */
    const uint8_t sample[] = "LiveExtract Native C Core Verification String";
    uint32_t crc = ze_crc32(0, sample, sizeof(sample) - 1);
    printf("[C Engine] Streaming CRC32 calculation: 0x%08X\n", crc);

    ze_stream_free(stream);
    printf("[C Engine] Stream state successfully freed.\n");
    return 0;
}
