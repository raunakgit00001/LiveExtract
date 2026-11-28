# LiveExtract v1.3.0 — External Folder Repair, 1–3 GB/s Integrity Engine & Official Identity

> **LiveExtract** is a zero-overhead streaming archive downloader and extractor that streams ZIP, ZIP64, RAR4, RAR5, and 7z archives directly from HTTP/HTTPS endpoints with **0.00 bytes of temporary archive disk overhead**.

---

## 🚀 What's New in v1.3.0

### 1. External Folder Verification & Surgical Repair
- **Repair Non-LiveExtract Downloads**: If you previously downloaded an archive or game installation using **WinRAR, 7-Zip, or a web browser** and files were later deleted, corrupted, or removed by antivirus software, LiveExtract can now audit and surgically repair the folder.
- **100% Intact File Preservation**: Compares your local directory against the remote archive manifest in RAM. All intact files remain untouched (**0 bytes re-downloaded**).
- **Surgical Network Streaming**: LiveExtract streams **only** the missing or corrupted files directly into place.
- **Smart Prefix Alignment**: Automatically detects whether an archive was packed with an enclosing root folder (e.g. `GameName/bin/...` vs local `bin/...`) that was stripped during original extraction.

### 2. High-Throughput 1–3 GB/s Parallel Integrity Engine
- **Instant Metadata Audit**: Traverses file sizes and structural layouts in milliseconds before touching disk sectors.
- **Parallel 1 MB CRC-32 Engine**: Multi-threaded hashing using 1 MB uncompressed read buffers and GIL-releasing C hashing routines. Delivers **1–3 GB/s verification throughput** on NVMe SSDs, verifying 80–100 GB installations in seconds.
- **Real-Time Throughput Reporting**: Live verification progress bar, current throughput (MB/s), file counters, and byte progress in both desktop GUI and CLI (`liveextract repair`).

### 3. Fast-Path Transaction Validation
- Uninterrupted files recorded in the state journal validate in **0.001 ms**, completely eliminating redundant multi-gigabyte disk re-reads when resuming.

### 4. Official Brand Identity & Native Multi-Resolution Shell Icon
- **Precision Monogram (L·E)**: Architectural dark slate frame with kinetic cyan/blue streaming rails and an emerald green cryptographic integrity pip.
- **Zero-Blur Scaling**: Features native uncompressed DIB mipmaps (16, 24, 32, 48, 64, 128 px) and 256 px PNG. Sharp, crisp visuals in Windows File Explorer, taskbar, Alt+Tab, and desktop shortcuts on 1080p, 1440p, 4K, and 8K displays.

### 5. High-Throughput Speed Profiles
- **⚡ Max Speed**: 8 parallel workers, 1 MB streaming chunks, 64 MB contiguous batch coalescing, 4 MB TCP window, and 96 MB RAM cache for gigabit line saturation.
- **🌿 Optimized**: 4 workers, 256 KB chunks, 32 MB batches, 2 MB TCP window, and 32 MB RAM cache for balanced multitasking while gaming.

---

## 🔒 Security & Privacy Guarantees

- **Direct End-to-End Connection**: Direct socket connection between your machine and the file host. Zero proxy servers, zero third-party relays.
- **100% Zero Telemetry**: No tracking, no crash telemetry, and no background reporting.
- **Zip Slip Defense**: Strict directory traversal validation (`DiskAllocator.sanitize_path`) prevents files from escaping the target folder.
- **Zip Bomb Protection**: Pre-flight uncompressed size check prevents drive exhaustion before writing to disk.

---

## 📦 Asset Checksum & Download

| Asset | Size | SHA-256 Checksum |
| :--- | :--- | :--- |
| `LiveExtract-Portable-Windows-x64.zip` | 12.35 MB | `60641F6AB3430E310D11B74410185E8C3F7CA70B16F6A3ACA86E93088A37783A` |

Verify integrity on Windows PowerShell:
```powershell
Get-FileHash .\LiveExtract-Portable-Windows-x64.zip -Algorithm SHA256
```
