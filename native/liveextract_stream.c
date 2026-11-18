/**
 * LiveExtract Native C Streaming Decompression Engine Implementation.
 */

#define LIVEEXTRACT_EXPORTS
#include "liveextract_stream.h"
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

struct ze_stream_s {
    z_stream z;
    int initialized;
    int is_finished;
};

ZE_API int ze_stream_init(ze_stream_t** stream_out, int window_bits) {
    if (!stream_out) return ZE_PARAM_ERROR;

    ze_stream_t* s = (ze_stream_t*)calloc(1, sizeof(ze_stream_t));
    if (!s) return ZE_MEM_ERROR;

    s->z.zalloc = Z_NULL;
    s->z.zfree = Z_NULL;
    s->z.opaque = Z_NULL;
    s->z.avail_in = 0;
    s->z.next_in = Z_NULL;

    /* inflateInit2 with window_bits = -15 binds raw DEFLATE with 32 KB sliding window */
    int ret = inflateInit2(&s->z, window_bits != 0 ? window_bits : -15);
    if (ret != Z_OK) {
        free(s);
        return ZE_DATA_ERROR;
    }

    s->initialized = 1;
    s->is_finished = 0;
    *stream_out = s;
    return ZE_OK;
}

ZE_API int ze_stream_decompress_chunk(
    ze_stream_t* s,
    const uint8_t* in_buf,
    size_t in_len,
    size_t* in_consumed,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_written
) {
    if (!s || !s->initialized) return ZE_PARAM_ERROR;
    if (in_consumed) *in_consumed = 0;
    if (out_written) *out_written = 0;

    if (s->is_finished) {
        return ZE_STREAM_END;
    }

    s->z.next_in = (Bytef*)in_buf;
    s->z.avail_in = (uInt)in_len;
    s->z.next_out = (Bytef*)out_buf;
    s->z.avail_out = (uInt)out_cap;

    int ret = Z_OK;
    while (s->z.avail_in > 0 && s->z.avail_out > 0) {
        ret = inflate(&s->z, Z_NO_FLUSH);
        if (ret == Z_STREAM_END) {
            s->is_finished = 1;
            break;
        }
        if (ret != Z_OK && ret != Z_BUF_ERROR) {
            break;
        }
    }

    size_t consumed = in_len - s->z.avail_in;
    size_t produced = out_cap - s->z.avail_out;

    if (in_consumed) *in_consumed = consumed;
    if (out_written) *out_written = produced;

    if (s->is_finished) {
        return ZE_STREAM_END;
    } else if (ret == Z_OK || ret == Z_BUF_ERROR) {
        return ZE_OK;
    }

    return ZE_DATA_ERROR;
}

ZE_API int ze_stream_flush(
    ze_stream_t* s,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_written
) {
    if (!s || !s->initialized) return ZE_PARAM_ERROR;
    if (out_written) *out_written = 0;

    if (s->is_finished) {
        return ZE_STREAM_END;
    }

    s->z.next_in = Z_NULL;
    s->z.avail_in = 0;
    s->z.next_out = (Bytef*)out_buf;
    s->z.avail_out = (uInt)out_cap;

    int ret = Z_OK;
    while (s->z.avail_out > 0) {
        ret = inflate(&s->z, Z_FINISH);
        if (ret == Z_STREAM_END) {
            s->is_finished = 1;
            break;
        }
        if (ret != Z_OK && ret != Z_BUF_ERROR) {
            break;
        }
    }

    size_t produced = out_cap - s->z.avail_out;
    if (out_written) *out_written = produced;

    if (s->is_finished) {
        return ZE_STREAM_END;
    } else if (ret == Z_OK || ret == Z_BUF_ERROR) {
        return ZE_OK;
    }

    return ZE_DATA_ERROR;
}

ZE_API uint32_t ze_crc32(uint32_t current_crc, const uint8_t* buf, size_t len) {
    return (uint32_t)crc32(current_crc, (const Bytef*)buf, (uInt)len);
}

ZE_API void ze_stream_free(ze_stream_t* s) {
    if (s) {
        if (s->initialized) {
            inflateEnd(&s->z);
        }
        free(s);
    }
}
