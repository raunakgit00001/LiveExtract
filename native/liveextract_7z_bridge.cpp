/**
 * LiveExtract Native 7z.dll Virtual Stream Bridge (COM IInStream -> Direct Disk)
 *
 * Streams RAR4, RAR5 (Standard & Solid), 7z, and other archives directly from
 * HTTP Range callbacks in RAM into 7z.dll with STRICTLY 0 BYTES of compressed
 * archive ever written to disk.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <oleauto.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <string>
#include <vector>

#define ZE7Z_API extern "C" __declspec(dllexport)

// =========================================================================
// 7-Zip Standard Frozen COM GUIDs & Interfaces
// =========================================================================

static const GUID IID_IUnknown_Local =
    {0x00000000, 0x0000, 0x0000, {0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46}};

static const GUID IID_ISequentialInStream =
    {0x23170F69, 0x40C1, 0x278A, {0x00, 0x00, 0x00, 0x03, 0x00, 0x01, 0x00, 0x00}};
static const GUID IID_ISequentialOutStream =
    {0x23170F69, 0x40C1, 0x278A, {0x00, 0x00, 0x00, 0x03, 0x00, 0x02, 0x00, 0x00}};
static const GUID IID_IInStream =
    {0x23170F69, 0x40C1, 0x278A, {0x00, 0x00, 0x00, 0x03, 0x00, 0x03, 0x00, 0x00}};
static const GUID IID_IProgress =
    {0x23170F69, 0x40C1, 0x278A, {0x00, 0x00, 0x00, 0x00, 0x00, 0x05, 0x00, 0x00}};
static const GUID IID_IArchiveOpenCallback =
    {0x23170F69, 0x40C1, 0x278A, {0x00, 0x00, 0x00, 0x06, 0x00, 0x10, 0x00, 0x00}};
static const GUID IID_IArchiveExtractCallback =
    {0x23170F69, 0x40C1, 0x278A, {0x00, 0x00, 0x00, 0x06, 0x00, 0x20, 0x00, 0x00}};
static const GUID IID_IInArchive =
    {0x23170F69, 0x40C1, 0x278A, {0x00, 0x00, 0x00, 0x06, 0x00, 0x60, 0x00, 0x00}};

// Format Handler CLSIDs
static const GUID CLSID_CFormatZip  = {0x23170F69, 0x40C1, 0x278A, {0x10, 0x00, 0x00, 0x01, 0x10, 0x01, 0x00, 0x00}};
static const GUID CLSID_CFormatRar  = {0x23170F69, 0x40C1, 0x278A, {0x10, 0x00, 0x00, 0x01, 0x10, 0x03, 0x00, 0x00}};
static const GUID CLSID_CFormat7z   = {0x23170F69, 0x40C1, 0x278A, {0x10, 0x00, 0x00, 0x01, 0x10, 0x07, 0x00, 0x00}};
static const GUID CLSID_CFormatRar5 = {0x23170F69, 0x40C1, 0x278A, {0x10, 0x00, 0x00, 0x01, 0x10, 0xCC, 0x00, 0x00}};

// 7-Zip PropIDs
enum {
    kpidPath = 3,
    kpidIsDir = 6,
    kpidSize = 7,
    kpidPackSize = 8,
    kpidCRC = 19,
};

struct ISequentialInStream7z : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE Read(void* data, uint32_t size, uint32_t* processedSize) = 0;
};

struct IInStream7z : public ISequentialInStream7z {
    virtual HRESULT STDMETHODCALLTYPE Seek(int64_t offset, uint32_t seekOrigin, uint64_t* newPosition) = 0;
};

struct ISequentialOutStream7z : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE Write(const void* data, uint32_t size, uint32_t* processedSize) = 0;
};

struct IProgress7z : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE SetTotal(uint64_t total) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetCompleted(const uint64_t* completeValue) = 0;
};

struct IArchiveOpenCallback7z : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE SetTotal(const uint64_t* files, const uint64_t* bytes) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetCompleted(const uint64_t* files, const uint64_t* bytes) = 0;
};

struct IArchiveExtractCallback7z : public IProgress7z {
    virtual HRESULT STDMETHODCALLTYPE GetStream(uint32_t index, ISequentialOutStream7z** outStream, int32_t askExtractMode) = 0;
    virtual HRESULT STDMETHODCALLTYPE PrepareOperation(int32_t askExtractMode) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetOperationResult(int32_t opRes) = 0;
};

struct IInArchive7z : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE Open(IInStream7z* stream, const uint64_t* maxCheckStartPosition, IArchiveOpenCallback7z* openCallback) = 0;
    virtual HRESULT STDMETHODCALLTYPE Close() = 0;
    virtual HRESULT STDMETHODCALLTYPE GetNumberOfItems(uint32_t* numItems) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetProperty(uint32_t index, PROPID propID, PROPVARIANT* value) = 0;
    virtual HRESULT STDMETHODCALLTYPE Extract(const uint32_t* indices, uint32_t numItems, int32_t testMode, IArchiveExtractCallback7z* extractCallback) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetArchiveProperty(PROPID propID, PROPVARIANT* value) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetNumberOfProperties(uint32_t* numProps) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetPropertyInfo(uint32_t index, BSTR* name, PROPID* propID, VARTYPE* varType) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetNumberOfArchiveProperties(uint32_t* numProps) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetArchivePropertyInfo(uint32_t index, BSTR* name, PROPID* propID, VARTYPE* varType) = 0;
};

typedef HRESULT (WINAPI *CreateObjectFunc)(const GUID* clsID, const GUID* iid, void** outObject);

// =========================================================================
// Python Callback Signatures
// =========================================================================

// Reads up to `size` bytes at `offset` into `out_buf`. Returns bytes read, or -1 on error/abort.
typedef int32_t (*ze_read_at_cb)(uint64_t offset, uint8_t* out_buf, uint32_t size, void* user_data);

// Called once per archive item discovered before extraction (for manifest/journal).
typedef void (*ze_item_meta_cb)(
    uint32_t index,
    const wchar_t* rel_path,
    int32_t is_dir,
    uint64_t size,
    uint32_t crc32,
    void* user_data
);

// Called during decompression write. Return 0 to continue, non-zero to abort/cancel.
typedef int32_t (*ze_progress_cb)(
    uint32_t current_index,
    uint32_t total_items,
    const wchar_t* current_path,
    uint64_t bytes_written_delta,
    void* user_data
);

// =========================================================================
// Helper Utilities
// =========================================================================

static bool ensure_parent_dirs(const std::wstring& full_path) {
    size_t pos = 0;
    // Skip drive prefix like "D:\" or "\\?\"
    if (full_path.size() >= 3 && full_path[1] == L':' && (full_path[2] == L'\\' || full_path[2] == L'/')) {
        pos = 3;
    }
    while ((pos = full_path.find_first_of(L"\\/", pos)) != std::wstring::npos) {
        std::wstring sub = full_path.substr(0, pos);
        if (!sub.empty()) {
            CreateDirectoryW(sub.c_str(), NULL);
        }
        pos++;
    }
    return true;
}

static bool sanitize_relative_path(const std::wstring& rel, std::wstring& clean_out) {
    clean_out.clear();
    std::wstring norm = rel;
    for (wchar_t& c : norm) {
        if (c == L'/') c = L'\\';
    }
    while (!norm.empty() && norm[0] == L'\\') {
        norm.erase(0, 1);
    }
    // Block drive letters or path traversal ("..")
    if (norm.find(L':') != std::wstring::npos) return false;
    size_t start = 0;
    while (start <= norm.size()) {
        size_t sep = norm.find(L'\\', start);
        std::wstring part = (sep == std::wstring::npos) ? norm.substr(start) : norm.substr(start, sep - start);
        if (part == L"..") return false;
        if (sep == std::wstring::npos) break;
        start = sep + 1;
    }
    clean_out = norm;
    return !clean_out.empty();
}

static uint64_t propvariant_to_u64(const PROPVARIANT& pv) {
    switch (pv.vt) {
        case VT_UI1: return pv.bVal;
        case VT_UI2: return pv.uiVal;
        case VT_UI4: return pv.ulVal;
        case VT_UI8: return pv.uhVal.QuadPart;
        case VT_I4:  return pv.lVal >= 0 ? (uint64_t)pv.lVal : 0;
        case VT_I8:  return pv.hVal.QuadPart >= 0 ? (uint64_t)pv.hVal.QuadPart : 0;
        default:     return 0;
    }
}

// =========================================================================
// Virtual RAM-Only IInStream backed by Python HTTP Range Callback
// =========================================================================

class CallbackInStream : public IInStream7z {
    volatile LONG _ref = 1;
    uint64_t _pos = 0;
    uint64_t _size = 0;
    ze_read_at_cb _read_cb = nullptr;
    void* _user_data = nullptr;

public:
    CallbackInStream(uint64_t total_size, ze_read_at_cb read_cb, void* user_data)
        : _size(total_size), _read_cb(read_cb), _user_data(user_data) {}

    virtual ~CallbackInStream() = default;

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** outObject) override {
        if (!outObject) return E_POINTER;
        if (IsEqualGUID(iid, IID_IUnknown_Local) ||
            IsEqualGUID(iid, IID_ISequentialInStream) ||
            IsEqualGUID(iid, IID_IInStream)) {
            *outObject = static_cast<IInStream7z*>(this);
            AddRef();
            return S_OK;
        }
        *outObject = nullptr;
        return E_NOINTERFACE;
    }

    ULONG STDMETHODCALLTYPE AddRef() override { return (ULONG)InterlockedIncrement(&_ref); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG r = InterlockedDecrement(&_ref);
        if (r == 0) delete this;
        return (ULONG)r;
    }

    HRESULT STDMETHODCALLTYPE Read(void* data, uint32_t size, uint32_t* processedSize) override {
        if (processedSize) *processedSize = 0;
        if (size == 0 || _pos >= _size) return S_OK;

        uint64_t remain = _size - _pos;
        uint32_t to_read = (size > remain) ? (uint32_t)remain : size;

        int32_t n = _read_cb(_pos, reinterpret_cast<uint8_t*>(data), to_read, _user_data);
        if (n < 0) return E_ABORT;

        _pos += (uint64_t)n;
        if (processedSize) *processedSize = (uint32_t)n;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE Seek(int64_t offset, uint32_t seekOrigin, uint64_t* newPosition) override {
        int64_t target = 0;
        switch (seekOrigin) {
            case STREAM_SEEK_SET: target = offset; break;
            case STREAM_SEEK_CUR: target = (int64_t)_pos + offset; break;
            case STREAM_SEEK_END: target = (int64_t)_size + offset; break;
            default: return STG_E_INVALIDFUNCTION;
        }
        if (target < 0) return HRESULT_FROM_WIN32(ERROR_NEGATIVE_SEEK);
        _pos = (uint64_t)target;
        if (newPosition) *newPosition = _pos;
        return S_OK;
    }
};

// =========================================================================
// Minimal Open Callback
// =========================================================================

class SimpleOpenCallback : public IArchiveOpenCallback7z {
    volatile LONG _ref = 1;
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** outObject) override {
        if (!outObject) return E_POINTER;
        if (IsEqualGUID(iid, IID_IUnknown_Local) || IsEqualGUID(iid, IID_IArchiveOpenCallback)) {
            *outObject = static_cast<IArchiveOpenCallback7z*>(this);
            AddRef();
            return S_OK;
        }
        *outObject = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return (ULONG)InterlockedIncrement(&_ref); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG r = InterlockedDecrement(&_ref);
        if (r == 0) delete this;
        return (ULONG)r;
    }
    HRESULT STDMETHODCALLTYPE SetTotal(const uint64_t*, const uint64_t*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE SetCompleted(const uint64_t*, const uint64_t*) override { return S_OK; }
};

// =========================================================================
// Direct-to-Disk OutStream (writes ONLY final uncompressed extracted bytes)
// =========================================================================

class DirectFileOutStream : public ISequentialOutStream7z {
    volatile LONG _ref = 1;
    HANDLE _hFile = INVALID_HANDLE_VALUE;
    uint32_t _item_idx = 0;
    uint32_t _total_items = 0;
    std::wstring _rel_path;
    ze_progress_cb _progress_cb = nullptr;
    void* _user_data = nullptr;

public:
    DirectFileOutStream(
        HANDLE hFile,
        uint32_t item_idx,
        uint32_t total_items,
        const std::wstring& rel_path,
        ze_progress_cb progress_cb,
        void* user_data
    ) : _hFile(hFile),
        _item_idx(item_idx),
        _total_items(total_items),
        _rel_path(rel_path),
        _progress_cb(progress_cb),
        _user_data(user_data) {}

    virtual ~DirectFileOutStream() {
        Close();
    }

    void Close() {
        if (_hFile != INVALID_HANDLE_VALUE) {
            CloseHandle(_hFile);
            _hFile = INVALID_HANDLE_VALUE;
        }
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** outObject) override {
        if (!outObject) return E_POINTER;
        if (IsEqualGUID(iid, IID_IUnknown_Local) || IsEqualGUID(iid, IID_ISequentialOutStream)) {
            *outObject = static_cast<ISequentialOutStream7z*>(this);
            AddRef();
            return S_OK;
        }
        *outObject = nullptr;
        return E_NOINTERFACE;
    }

    ULONG STDMETHODCALLTYPE AddRef() override { return (ULONG)InterlockedIncrement(&_ref); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG r = InterlockedDecrement(&_ref);
        if (r == 0) delete this;
        return (ULONG)r;
    }

    HRESULT STDMETHODCALLTYPE Write(const void* data, uint32_t size, uint32_t* processedSize) override {
        if (processedSize) *processedSize = 0;
        if (_hFile == INVALID_HANDLE_VALUE) return E_FAIL;

        DWORD written = 0;
        if (size > 0) {
            if (!WriteFile(_hFile, data, size, &written, NULL)) {
                return HRESULT_FROM_WIN32(GetLastError());
            }
        }
        if (processedSize) *processedSize = (uint32_t)written;

        if (_progress_cb) {
            int32_t cancel = _progress_cb(_item_idx, _total_items, _rel_path.c_str(), (uint64_t)written, _user_data);
            if (cancel != 0) {
                return E_ABORT;
            }
        }
        return S_OK;
    }
};

// =========================================================================
// Direct-to-Disk Archive Extract Callback
// =========================================================================

class DirectExtractCallback : public IArchiveExtractCallback7z {
    volatile LONG _ref = 1;
    IInArchive7z* _archive = nullptr;
    std::wstring _dest_dir;
    uint32_t _total_items = 0;
    ze_progress_cb _progress_cb = nullptr;
    void* _user_data = nullptr;
    DirectFileOutStream* _cur_stream = nullptr;
    int32_t _last_op_error = 0;

public:
    uint32_t files_extracted = 0;
    uint64_t bytes_extracted = 0;

    DirectExtractCallback(
        IInArchive7z* archive,
        const std::wstring& dest_dir,
        uint32_t total_items,
        ze_progress_cb progress_cb,
        void* user_data
    ) : _archive(archive),
        _dest_dir(dest_dir),
        _total_items(total_items),
        _progress_cb(progress_cb),
        _user_data(user_data) {
        if (!_dest_dir.empty() && _dest_dir.back() != L'\\' && _dest_dir.back() != L'/') {
            _dest_dir.push_back(L'\\');
        }
    }

    virtual ~DirectExtractCallback() {
        if (_cur_stream) {
            _cur_stream->Close();
            _cur_stream->Release();
            _cur_stream = nullptr;
        }
    }

    int32_t last_op_error() const { return _last_op_error; }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** outObject) override {
        if (!outObject) return E_POINTER;
        if (IsEqualGUID(iid, IID_IUnknown_Local) ||
            IsEqualGUID(iid, IID_IProgress) ||
            IsEqualGUID(iid, IID_IArchiveExtractCallback)) {
            *outObject = static_cast<IArchiveExtractCallback7z*>(this);
            AddRef();
            return S_OK;
        }
        *outObject = nullptr;
        return E_NOINTERFACE;
    }

    ULONG STDMETHODCALLTYPE AddRef() override { return (ULONG)InterlockedIncrement(&_ref); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG r = InterlockedDecrement(&_ref);
        if (r == 0) delete this;
        return (ULONG)r;
    }

    HRESULT STDMETHODCALLTYPE SetTotal(uint64_t) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE SetCompleted(const uint64_t*) override { return S_OK; }

    HRESULT STDMETHODCALLTYPE GetStream(uint32_t index, ISequentialOutStream7z** outStream, int32_t askExtractMode) override {
        if (!outStream) return E_POINTER;
        *outStream = nullptr;

        if (_cur_stream) {
            _cur_stream->Close();
            _cur_stream->Release();
            _cur_stream = nullptr;
        }

        if (askExtractMode != 0) {  // 0 == kExtract
            return S_OK;
        }

        // Read item path & isDir
        PROPVARIANT pvPath;
        PropVariantInit(&pvPath);
        _archive->GetProperty(index, kpidPath, &pvPath);
        std::wstring raw_path;
        if (pvPath.vt == VT_BSTR && pvPath.bstrVal) {
            raw_path = pvPath.bstrVal;
        }
        PropVariantClear(&pvPath);

        PROPVARIANT pvIsDir;
        PropVariantInit(&pvIsDir);
        _archive->GetProperty(index, kpidIsDir, &pvIsDir);
        bool is_dir = (pvIsDir.vt == VT_BOOL && pvIsDir.boolVal != VARIANT_FALSE);
        PropVariantClear(&pvIsDir);

        std::wstring clean_rel;
        if (!sanitize_relative_path(raw_path, clean_rel)) {
            return S_OK;  // Skip unsafe traversal entry
        }

        std::wstring full_path = _dest_dir + clean_rel;

        if (is_dir) {
            ensure_parent_dirs(full_path + L"\\");
            CreateDirectoryW(full_path.c_str(), NULL);
            return S_OK;
        }

        ensure_parent_dirs(full_path);

        HANDLE hFile = CreateFileW(
            full_path.c_str(),
            GENERIC_WRITE,
            FILE_SHARE_READ,
            NULL,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            NULL
        );
        if (hFile == INVALID_HANDLE_VALUE) {
            return HRESULT_FROM_WIN32(GetLastError());
        }

        // Pre-allocate exact uncompressed size if known
        PROPVARIANT pvSize;
        PropVariantInit(&pvSize);
        if (SUCCEEDED(_archive->GetProperty(index, kpidSize, &pvSize))) {
            uint64_t unp_size = propvariant_to_u64(pvSize);
            if (unp_size > 0) {
                LARGE_INTEGER li;
                li.QuadPart = (LONGLONG)unp_size;
                if (SetFilePointerEx(hFile, li, NULL, FILE_BEGIN)) {
                    SetEndOfFile(hFile);
                    li.QuadPart = 0;
                    SetFilePointerEx(hFile, li, NULL, FILE_BEGIN);
                }
            }
            bytes_extracted += unp_size;
        }
        PropVariantClear(&pvSize);

        DirectFileOutStream* stream = new DirectFileOutStream(
            hFile, index, _total_items, clean_rel, _progress_cb, _user_data
        );
        _cur_stream = stream;
        _cur_stream->AddRef();
        *outStream = stream;
        files_extracted++;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE PrepareOperation(int32_t) override { return S_OK; }

    HRESULT STDMETHODCALLTYPE SetOperationResult(int32_t opRes) override {
        if (_cur_stream) {
            _cur_stream->Close();
            _cur_stream->Release();
            _cur_stream = nullptr;
        }
        if (opRes != 0) {  // 0 == kOK
            _last_op_error = opRes;
        }
        return S_OK;
    }
};

// =========================================================================
// Public C Exported Entry Point
// =========================================================================

// format_id: 1 = ZIP, 3 = RAR4, 7 = 7z, 204 (0xCC) = RAR5
ZE7Z_API int32_t ze7z_extract_stream(
    const wchar_t* sevenzip_dll_path,
    int32_t format_id,
    uint64_t archive_total_size,
    const wchar_t* dest_dir,
    ze_read_at_cb read_cb,
    ze_item_meta_cb meta_cb,
    ze_progress_cb progress_cb,
    void* user_data,
    uint32_t* out_files_extracted,
    uint64_t* out_bytes_extracted
) {
    if (!sevenzip_dll_path || !dest_dir || !read_cb) {
        return -1;
    }

    HMODULE h7z = LoadLibraryW(sevenzip_dll_path);
    if (!h7z) {
        return -2;
    }

    CreateObjectFunc createObj = reinterpret_cast<CreateObjectFunc>(GetProcAddress(h7z, "CreateObject"));
    if (!createObj) {
        FreeLibrary(h7z);
        return -3;
    }

    const GUID* clsid = nullptr;
    switch (format_id) {
        case 1:    clsid = &CLSID_CFormatZip;  break;
        case 3:    clsid = &CLSID_CFormatRar;  break;
        case 7:    clsid = &CLSID_CFormat7z;   break;
        case 0xCC: clsid = &CLSID_CFormatRar5; break;
        default:
            FreeLibrary(h7z);
            return -4;
    }

    IInArchive7z* archive = nullptr;
    HRESULT hr = createObj(clsid, &IID_IInArchive, reinterpret_cast<void**>(&archive));
    if (FAILED(hr) || !archive) {
        FreeLibrary(h7z);
        return -5;
    }

    CallbackInStream* inStream = new CallbackInStream(archive_total_size, read_cb, user_data);
    SimpleOpenCallback* openCb = new SimpleOpenCallback();
    uint64_t maxCheckStart = 1 << 22;  // 4 MB check window

    hr = archive->Open(inStream, &maxCheckStart, openCb);
    if (FAILED(hr)) {
        openCb->Release();
        inStream->Release();
        archive->Release();
        FreeLibrary(h7z);
        return -6;
    }

    uint32_t numItems = 0;
    archive->GetNumberOfItems(&numItems);

    // Report item metadata to Python before extraction (builds .extract_state.json)
    if (meta_cb) {
        for (uint32_t i = 0; i < numItems; i++) {
            PROPVARIANT pvPath, pvIsDir, pvSize, pvCRC;
            PropVariantInit(&pvPath);
            PropVariantInit(&pvIsDir);
            PropVariantInit(&pvSize);
            PropVariantInit(&pvCRC);

            archive->GetProperty(i, kpidPath, &pvPath);
            archive->GetProperty(i, kpidIsDir, &pvIsDir);
            archive->GetProperty(i, kpidSize, &pvSize);
            archive->GetProperty(i, kpidCRC, &pvCRC);

            const wchar_t* path_str = (pvPath.vt == VT_BSTR && pvPath.bstrVal) ? pvPath.bstrVal : L"";
            int32_t is_dir = (pvIsDir.vt == VT_BOOL && pvIsDir.boolVal != VARIANT_FALSE) ? 1 : 0;
            uint64_t sz = propvariant_to_u64(pvSize);
            uint32_t crc = (pvCRC.vt == VT_UI4) ? pvCRC.ulVal : 0;

            meta_cb(i, path_str, is_dir, sz, crc, user_data);

            PropVariantClear(&pvPath);
            PropVariantClear(&pvIsDir);
            PropVariantClear(&pvSize);
            PropVariantClear(&pvCRC);
        }
    }

    DirectExtractCallback* extractCb = new DirectExtractCallback(
        archive, dest_dir, numItems, progress_cb, user_data
    );

    // Extract ALL items (indices = nullptr, numItems = (uint32_t)-1)
    hr = archive->Extract(nullptr, (uint32_t)-1, 0, extractCb);

    if (out_files_extracted) *out_files_extracted = extractCb->files_extracted;
    if (out_bytes_extracted) *out_bytes_extracted = extractCb->bytes_extracted;
    int32_t op_err = extractCb->last_op_error();

    archive->Close();
    extractCb->Release();
    openCb->Release();
    inStream->Release();
    archive->Release();
    FreeLibrary(h7z);

    if (hr == E_ABORT) return 1;  // Cancelled by user
    if (FAILED(hr)) return -7;
    if (op_err != 0) return -(100 + op_err);
    return 0;  // Success!
}
