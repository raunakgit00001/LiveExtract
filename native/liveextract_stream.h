/**
 * LiveExtract Native C Streaming Decompression Engine.
 * Lightweight DEFLATE streaming core with fixed 32 KB sliding window (< 50 KB binary footprint).
 */

#ifndef LIVEEXTRACT_STREAM_H
#define LIVEEXTRACT_STREAM_H

#include <stdint.h>
#include <stddef.h>

#if defined(_WIN32) || defined(__CYGWIN__)
  #ifdef LIVEEXTRACT_EXPORTS
    #define ZE_API __declspec(dllexport)
  #else
    #define ZE_API __declspec(dllimport)
  #endif
#else
  #define ZE_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Return status codes */
#define ZE_OK         0
#define ZE_STREAM_END 1
#define ZE_MEM_ERROR -1
#define ZE_DATA_ERROR -2
#define ZE_PARAM_ERROR -3

typedef struct ze_stream_s ze_stream_t;

/**
 * Initialize a new streaming decompressor.
 * window_bits = -15 for raw RFC 1951 DEFLATE (32 KB sliding window).
 */
ZE_API int ze_stream_init(ze_stream_t** stream_out, int window_bits);

/**
 * Decompress an incoming byte chunk into out_buf.
 * in_buf: pointer to incoming network/file bytes
 * in_len: length of input chunk
 * in_consumed: output pointer receiving number of input bytes consumed
 * out_buf: output scratchpad buffer
 * out_cap: capacity of out_buf
 * out_written: output pointer receiving number of uncompressed bytes produced
 * Returns: ZE_OK on progress, ZE_STREAM_END when stream completes, or negative on error.
 */
ZE_API int ze_stream_decompress_chunk(
    ze_stream_t* s,
    const uint8_t* in_buf,
    size_t in_len,
    size_t* in_consumed,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_written
);

/**
 * Flush any remaining bytes in internal decompressor buffers.
 */
ZE_API int ze_stream_flush(
    ze_stream_t* s,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_written
);

/**
 * Calculate running CRC-32 for integrity validation.
 */
ZE_API uint32_t ze_crc32(uint32_t current_crc, const uint8_t* buf, size_t len);

/**
 * Free stream state and 32 KB scratchpad.
 */
ZE_API void ze_stream_free(ze_stream_t* s);

#ifdef __cplusplus
}
#endif

#endif /* LIVEEXTRACT_STREAM_H */
