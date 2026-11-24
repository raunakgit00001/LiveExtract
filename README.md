# LiveExtract

**Stream-extract remote `.zip`, `.rar`, and `.7z` archives straight to disk with zero temporary archive storage.**

Instead of downloading a 50 GB archive to your drive, waiting for it to finish, and then needing another 50+ GB of free space to extract it, **LiveExtract** reads archive headers over HTTP Range requests, decompresses incoming network chunks in RAM, and writes only the final extracted files directly to your destination folder.

---

## Key Features

* **Zero Temporary Archive Storage**: Never saves the `.zip`, `.rar`, or `.7z` archive file to disk — even temporarily.
* **Full Line-Speed Streaming & Selectable Speed Profiles**:
  * **⚡ Max Speed Profile**: Saturated gigabit throughput with 8 parallel connections, 1 MB chunk buffers, 64 MB batch coalescing, and 4 MB TCP receive windows.
  * **🌿 Optimized Profile**: Balanced resource profile for background downloads or lower-spec machines.
  * Persistent HTTP/1.1 Keep-Alive TLS connections (eliminates repeated handshakes).
  * Contiguous Range Batch Coalescing (merges hundreds of small files into continuous streams up to 64 MB).
  * Multi-connection parallel workers (`1–16` connections).
* **Instant Start on Massive Archives (60–80+ GB)**:
  * Eliminates multi-minute disk pre-allocation freezes on archives with tens of thousands of files.
  * Validates disk space in RAM in 1 ms and writes files on-the-fly, avoiding empty placeholder folders.
* **Broad Archive & Codec Support**:
  * **ZIP & ZIP64 (> 4 GB)**: `STORED (0)`, `DEFLATE (8)`, `BZIP2 (12)`, `LZMA (14)`, `ZSTD (93)`, and `XZ (95)`.
  * **RAR4, RAR5 (Standard & Solid), and 7z**: Pure zero-disk virtual COM stream bridge (`liveextract_7z_bridge.dll` → `7z.dll`) with a bounded 12 MB RAM read-ahead cache.
* **Works With Signed & Proxy Links**: Automatically resolves filenames from `Content-Disposition`, preserves query signatures across redirects, and works without file extensions in the URL.
* **Live Pause, Resume, Cryptographic Verification & Smart Differential Updates**:
  * Pause and resume active transfers without losing progress.
  * Lockstep CRC-32 verification plus one-click integrity checking and missing-file repair.
  * **Smart Differential Updater**: Updates existing folders against new archive releases by inspecting remote headers over HTTP Range and downloading *only* new or modified files, saving 90–99% of bandwidth.
* **Lightweight Desktop App + Reusable Library**:
  * Minimal native GUI designed to stay smooth even on low-end hardware (no Electron/Chromium overhead).
  * Core `liveextract` package is completely decoupled from the GUI for use in scripts, CLIs, or custom launchers.

---

## Quick Start (Portable Windows App)

1. Download **`LiveExtract-Portable-Windows-x64.zip`** from the [Releases](../../releases) page and extract it anywhere (no installation or admin rights required).
2. Run **`LiveExtract.exe`**.
3. Copy or paste an archive link (`.zip`, `.rar`, or `.7z`). LiveExtract automatically displays the file name and size before downloading.
4. Click **Download & Extract**.
5. Optional: click **Advanced ▸** to adjust parallel connections, buffer size, verify files, or inspect activity logs.

---

## Requirements & Dependencies

### Using the Portable `.exe` (Windows x64)
* **Windows 10 / 11 (64-bit)**.
* **`.zip` / `.zip64` (all codecs including Zstd)**: **No extra software required** — works completely standalone.
* **`.rar` (RAR4/RAR5) & `.7z`**: Requires **[7-Zip](https://www.7-zip.org/)** installed on your PC (`7z.dll` is loaded dynamically at runtime; LiveExtract does not redistribute `7z.dll`).

### Running From Python Source
* **Python 3.10+** (Python **3.14+** recommended for built-in stdlib `compression.zstd` with zero third-party packages).
* **Python < 3.14 only**: Install `zstandard` if extracting Zstd-compressed (Method 93) ZIPs:
  ```bash
  pip install zstandard
  ```
* **Building the native DLLs from source (Windows)**: MinGW-w64 (`gcc` / `g++`) and `pyinstaller` (`pip install pyinstaller`).

---

## CLI & Python Library Usage

Because the core engine (`liveextract/`) is separate from `gui/`, developers can integrate it directly into their own projects.

### Install Editable Package
```bash
pip install -e .
```

### CLI Commands
```bash
# Launch the minimal desktop UI
liveextract gui

# Inspect a remote archive's contents in RAM without downloading it
liveextract inspect https://example.com/archive.zip

# Stream-extract directly to disk at maximum speed (8 parallel connections, 0 B archive written)
liveextract stream https://example.com/archive.zip --dest ./output --mode max

# Stream-extract with balanced/optimized profile
liveextract stream https://example.com/archive.zip --dest ./output --mode optimized

# Verify cryptographic CRC-32 integrity of an extracted folder & repair missing files
liveextract repair ./output --source https://example.com/archive.zip

# Differential update: compare local folder against a new archive & stream only changed files
liveextract update ./output --source https://example.com/archive_v2.zip --clean

# Pre-generate a byte-offset manifest for custom game launchers (Mode A)
liveextract manifest ./build.zip -o build.manifest.json

# Run a local RFC 7233 byte-range test server
liveextract serve ./archives --port 8080
```

### Python API
```python
from liveextract.client import StreamClient, StreamController

controller = StreamController()
client = StreamClient(max_workers=4, verify_crc=True, resume=True, controller=controller)

result = client.extract_from_url(
    archive_source="https://example.com/package.zip",
    target_dir="./extracted",
    progress_cb=lambda p: print(f"{p.percent:.1f}%  {p.net_bytes_per_second / 1e6:.2f} MB/s"),
)
```

---

## License & Attribution

This project is licensed under the **PolyForm Noncommercial License 1.0.0**.

* **Free for Personal, Educational, and Non-Commercial Use**: You may download, use, modify, redistribute, and adapt this software freely for personal or non-commercial purposes.
* **Attribution Required**: You must retain the original copyright notice, author credits, and license notice in all copies or substantial portions of the software.
* **Commercial Use**: Any commercial use, sale, integration into paid commercial software, or monetize-for-profit distribution requires an explicit commercial agreement.
* See the full [LICENSE](LICENSE) and [NOTICE](NOTICE) files for legal terms.
