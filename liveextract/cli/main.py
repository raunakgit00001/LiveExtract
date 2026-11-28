"""
LiveExtract command-line interface.
Commands:
  inspect   - Inspect remote or local archive structure in RAM without downloading.
  manifest  - Generate pre-flight byte-offset manifest.json from an archive.
  stream    - Stream & extract directly to disk with zero archive storage overhead.
  serve     - Start byte-range enabled HTTP file & manifest server.
"""

import sys
import os
import argparse
import time
from pathlib import Path

from liveextract.core.manifest import generate_manifest_from_zip, ArchiveManifest
from liveextract.client.range_reader import RangeReader
from liveextract.client.stream_client import StreamClient, StreamProgress
from liveextract.server.api import create_server


def _format_size(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0 or unit == "TB":
            return f"{size_bytes:6.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def cmd_inspect(args: argparse.Namespace) -> int:
    """Inspect archive metadata without full download (supports ZIP and RAR)."""
    source = args.source
    print(f"\n[LiveExtract] Inspecting archive: {source}")
    t0 = time.time()

    reader = RangeReader(source)
    total_size = reader.get_total_size()
    print(f"Archive total size: {_format_size(total_size)} ({total_size:,} bytes)")

    # Read first 16 bytes to detect signature
    head_bytes = reader.read_range(0, min(total_size - 1, 15))
    from liveextract.core.rar_parser import RarBinaryParser
    if RarBinaryParser.is_rar(head_bytes):
        # RAR Archive
        full_head = reader.read_range(0, min(total_size - 1, 131072))
        rar_info = RarBinaryParser.inspect(full_head)
        elapsed_ms = (time.time() - t0) * 1000
        print(f"Format:             {rar_info.version}")
        print(f"Archive Type:       {'SOLID RAR' if rar_info.is_solid else 'STANDARD (Non-Solid) RAR'}")
        print(f"Dictionary Size:    {_format_size(rar_info.dictionary_size_bytes)} (Sliding Window RAM)")
        print(f"Streaming Mode:     {'Sequential stream from byte 0 only' if rar_info.is_solid else 'Byte-offset random access supported'}")
        print(f"Inspection Time:    {elapsed_ms:.1f} ms\n")
        print(f"Notes: {rar_info.notes}\n")

        if rar_info.files:
            print(f"{'Method':<8} {'Compressed':<12} {'Uncompressed':<14} {'Offset':<10} {'Filename'}")
            print("-" * 75)
            for f in rar_info.files:
                method = f"m{f.compression_method}"
                print(f"{method:<8} {_format_size(f.compressed_size):<12} {_format_size(f.uncompressed_size):<14} {f.data_offset:<10} {f.filename}")
            print("-" * 75)
            print(f"Total entries listed: {len(rar_info.files)}\n")
        return 0

    # ZIP Archive: Read tail
    tail_bytes, _ = reader.read_tail(65536 + 22)
    from liveextract.core.binary_parser import ZipBinaryParser
    eocd = ZipBinaryParser.find_eocd(tail_bytes, archive_size=total_size)
    print(f"Format:             ZIP Archive")
    print(f"EOCD offset:        {eocd.eocd_offset_in_archive:,}")
    print(f"Central Directory:  offset={eocd.cd_offset:,}, size={eocd.cd_size:,} bytes, entries={eocd.total_records}")

    cd_bytes = reader.read_range(eocd.cd_offset, eocd.cd_offset + eocd.cd_size - 1)
    entries = ZipBinaryParser.parse_central_directory(cd_bytes)

    elapsed_ms = (time.time() - t0) * 1000
    print(f"Metadata fetched and parsed in {elapsed_ms:.1f} ms\n")

    print(f"{'Method':<8} {'Compressed':<12} {'Uncompressed':<14} {'Ratio':<7} {'CRC-32':<9} {'Filename'}")
    print("-" * 80)

    total_uncompressed = 0
    total_compressed = 0
    from liveextract.core.decompressor import get_compression_method_name
    for e in entries:
        method = get_compression_method_name(e.compression_method)
        ratio = f"{((1 - e.compressed_size / max(1, e.uncompressed_size)) * 100):.1f}%" if e.uncompressed_size > 0 else "0%"
        total_uncompressed += e.uncompressed_size
        total_compressed += e.compressed_size
        print(f"{method:<8} {_format_size(e.compressed_size):<12} {_format_size(e.uncompressed_size):<14} {ratio:<7} {e.crc32_hex:<9} {e.filename}")

    print("-" * 80)
    overall_ratio = f"{((1 - total_compressed / max(1, total_uncompressed)) * 100):.1f}%" if total_uncompressed > 0 else "0%"
    print(f"{'TOTAL':<8} {_format_size(total_compressed):<12} {_format_size(total_uncompressed):<14} {overall_ratio:<7} {len(entries)} items\n")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    """Generate manifest.json from local archive."""
    archive_path = Path(args.archive)
    if not archive_path.exists():
        print(f"Error: Archive not found: {archive_path}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else archive_path.with_suffix(".manifest.json")
    print(f"[LiveExtract] Scanning archive {archive_path.name}...")
    t0 = time.time()
    manifest = generate_manifest_from_zip(archive_path)
    manifest.save(output_path)
    elapsed = time.time() - t0

    print(f"Manifest written to: {output_path}")
    print(f"Mapped {manifest.total_files} files ({_format_size(manifest.total_uncompressed_size)}) in {elapsed * 1000:.1f} ms")
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    """Stream and extract directly to target directory."""
    source = args.source
    dest_dir = Path(args.dest).resolve()
    manifest_path = Path(args.manifest) if args.manifest else None

    print(f"\n=======================================================")
    print(f" LiveExtract Streaming Engine")
    print(f" Source:      {source}")
    print(f" Destination: {dest_dir}")
    print(f" Mode:        {'Mode A (Manifest-driven)' if manifest_path else 'Mode B (Direct HTTP Range)'}")
    print(f"=======================================================\n")

    workers = args.workers
    chunk_size = args.chunk_size
    if getattr(args, "speed_mode", "max") == "max":
        if workers == 1:
            workers = 8
        if chunk_size == 262144:
            chunk_size = 1048576

    client = StreamClient(
        chunk_size=chunk_size,
        max_workers=workers,
        timeout=args.timeout,
        verify_crc=not args.no_verify,
        resume=not args.no_resume,
        speed_mode=getattr(args, "speed_mode", "max"),
    )

    last_print = 0.0

    def progress_callback(p: StreamProgress):
        nonlocal last_print
        now = time.time()
        if now - last_print < 0.1 and p.percent < 100.0:
            return
        last_print = now

        bar_len = 30
        filled = int(bar_len * (p.percent / 100.0))
        bar = "=" * filled + "-" * (bar_len - filled)
        speed_mb = p.bytes_per_second / (1024 * 1024)

        sys.stdout.write(
            f"\r[{bar}] {p.percent:5.1f}% | "
            f"{_format_size(p.uncompressed_bytes_done)} / {_format_size(p.total_uncompressed_bytes)} | "
            f"{speed_mb:5.2f} MB/s | [{p.file_index}/{p.total_files}] {p.current_file[:24]:<24}"
        )
        sys.stdout.flush()

    try:
        if manifest_path:
            manifest = ArchiveManifest.load(manifest_path)
            res = client.extract_from_manifest(
                manifest=manifest,
                archive_source=source,
                target_dir=dest_dir,
                progress_cb=progress_callback,
            )
        else:
            res = client.extract_from_url(
                archive_source=source,
                target_dir=dest_dir,
                progress_cb=progress_callback,
            )

        print("\n\nExtraction completed successfully!")
        print(f"Files Extracted:          {res['total_files']}")
        print(f"Uncompressed Data:        {_format_size(res['uncompressed_bytes'])}")
        print(f"Compressed Bytes Streamed:{_format_size(res['compressed_streamed_bytes'])}")
        print(f"Archive Disk Usage:       0.00 B (Archive was never saved to disk)")
        print(f"Elapsed Time:             {res['elapsed_seconds']:.2f} s")
        print(f"Effective Throughput:     {res['speed_mbps']:.2f} MB/s\n")
        return 0
    except Exception as err:
        print(f"\n\nExtraction failed: {err}", file=sys.stderr)
        return 1


def cmd_serve(args: argparse.Namespace) -> int:
    """Start local streaming and manifest server."""
    storage_dir = Path(args.storage).resolve()
    if not storage_dir.exists():
        storage_dir.mkdir(parents=True, exist_ok=True)

    server = create_server(storage_dir, host=args.host, port=args.port)
    actual_port = server.server_address[1]
    print(f"\n=======================================================")
    print(f" LiveExtract Streaming Server Active")
    print(f" Serving Directory: {storage_dir}")
    print(f" Endpoint:          http://{args.host}:{actual_port}/")
    print(f" Manifest URL:      http://{args.host}:{actual_port}/archives/<file>/manifest")
    print(f" Stream URL:        http://{args.host}:{actual_port}/archives/<file>/stream")
    print(f" Press Ctrl+C to terminate server")
    print(f"=======================================================\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer shutting down gracefully.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """Launch the LiveExtract graphical desktop launcher."""
    try:
        from gui.app import launch_gui
        launch_gui()
        return 0
    except ImportError as e:
        print(f"Error launching GUI: {e}", file=sys.stderr)
        return 1


def cmd_repair(args: argparse.Namespace) -> int:
    """Verify an existing folder against an archive link and re-download missing/corrupted files."""
    target_dir = Path(args.dest).resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"Error: Target directory does not exist: {target_dir}", file=sys.stderr)
        return 1

    source = args.source
    if not source:
        # Check .extract_state.json
        state_file = target_dir / ".extract_state.json"
        if state_file.exists():
            try:
                import json
                with open(state_file, "r", encoding="utf-8") as f:
                    s_data = json.load(f)
                    source = s_data.get("archive_source")
            except Exception:
                pass

    if not source:
        try:
            source = input("Enter archive download link or file path: ").strip()
        except EOFError:
            pass
        if not source:
            print("Error: Archive source link or file path is required.", file=sys.stderr)
            return 1

    print(f"\n[LiveExtract] Repairing folder: {target_dir}")
    print(f"Archive Source: {source}\n")

    from liveextract.core.repair import check_link_folder_match, prepare_repair
    from liveextract.core.verifier import verify_destination

    print("[1/3] Inspecting archive & checking content match...")
    is_match, match_res, expected_table = check_link_folder_match(target_dir, source)

    if not is_match:
        if match_res.matched_names == 0 and match_res.total_folder_files > 0:
            print(f"\n❌ Link Mismatch Error: {match_res.message}", file=sys.stderr)
            print("The link appears to be for a completely different file. Aborting.", file=sys.stderr)
            return 1
        print(f"\n⚠️  Warning: {match_res.message}")
        if not args.yes:
            try:
                ans = input("Proceed with repair anyway? [y/N]: ").strip().lower()
                if ans != "y":
                    return 1
            except EOFError:
                return 1

    print(f"      Match confirmed: {match_res.matched_names}/{match_res.total_folder_files} folder files match.")

    print("\n[2/3] Cryptographic CRC-32 integrity verification...")

    def _verify_prog(cur_file, idx=0, total=0, *extra):
        if hasattr(cur_file, "current_file"):
            p = cur_file
            speed_mb = p.bytes_per_second / (1024 * 1024)
            scanned_str = _format_size(p.bytes_scanned).strip()
            total_str = _format_size(p.total_bytes).strip()
            fname = Path(p.current_file).name[:24] if p.current_file else "Done"
            sys.stdout.write(
                f"\rVerifying: {p.percent:5.1f}% | {speed_mb:5.1f} MB/s | {scanned_str} / {total_str} | [{p.file_index}/{p.total_files}] {fname:<24}"
            )
        else:
            sys.stdout.write(f"\rVerifying: [{idx}/{total}] {Path(cur_file).name[:28]:<28}")
        sys.stdout.flush()

    report = verify_destination(
        target_dir,
        archive_source=source,
        progress_cb=_verify_prog,
        expected_table=expected_table,
        path_prefix=match_res.path_prefix,
    )
    print("")

    if report.is_valid:
        print(f"\n✅ All {report.total_expected} files are already 100% intact! No repairs needed.")
        return 0

    total_bad = len(report.missing_files) + len(report.corrupted_files)
    print(f"\nVerification Results:")
    print(f"  • Intact files:    {len(report.intact_files)} (preserved)")
    print(f"  • Missing files:   {len(report.missing_files)}")
    print(f"  • Corrupted files: {len(report.corrupted_files)}")
    print(f"  LiveExtract will only download the {total_bad} missing/corrupted file(s).")

    if not args.yes:
        try:
            ans = input(f"\nProceed with repair download of {total_bad} files? [Y/n]: ").strip().lower()
            if ans and ans != "y":
                return 1
        except EOFError:
            return 1

    print("\n[3/3] Purging corrupted files and streaming repair...")
    prepare_repair(target_dir, source, report, expected_table=expected_table, path_prefix=match_res.path_prefix)

    workers = args.workers
    chunk_size = args.chunk_size
    if getattr(args, "speed_mode", "max") == "max":
        if workers == 1:
            workers = 8
        if chunk_size == 262144:
            chunk_size = 1048576

    client = StreamClient(
        chunk_size=chunk_size,
        max_workers=workers,
        timeout=args.timeout,
        verify_crc=not args.no_verify,
        resume=True,
        speed_mode=getattr(args, "speed_mode", "max"),
    )

    def _prog(p: StreamProgress):
        pct = (p.uncompressed_bytes_done / max(1, p.total_uncompressed_bytes)) * 100
        speed_mb = p.net_bytes_per_second / (1024 * 1024)
        fname = Path(p.current_file).name[:30]
        print(f"\rRepairing: {pct:5.1f}% | {speed_mb:5.1f} MB/s | {fname:<30}", end="", flush=True)

    result = client.extract_from_url(source, target_dir, progress_cb=_prog)
    print("\n")
    if result.get("status") == "completed":
        print(f"✅ Folder successfully repaired and 100% verified! Total files: {result.get('total_files')}")
        return 0
    else:
        print(f"Repair ended with status: {result.get('status')}")
        return 1


def cmd_update(args: argparse.Namespace) -> int:
    """Check for differential updates and patch an existing folder."""
    target_dir = Path(args.dest).resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"Error: Target directory does not exist: {target_dir}", file=sys.stderr)
        return 1

    source = args.source
    if not source:
        state_file = target_dir / ".extract_state.json"
        if state_file.exists():
            try:
                import json
                with open(state_file, "r", encoding="utf-8") as f:
                    s_data = json.load(f)
                    source = s_data.get("archive_source")
            except Exception:
                pass

    if not source:
        try:
            source = input("Enter new archive update URL or file path: ").strip()
        except EOFError:
            pass
        if not source:
            print("Error: Archive update source link or file path is required.", file=sys.stderr)
            return 1

    print(f"\n[LiveExtract] Checking for updates: {target_dir}")
    print(f"Update Source: {source}\n")

    from liveextract.core.updater import check_for_updates, apply_update_plan

    print("[1/2] Inspecting remote archive & comparing local files...")
    plan = check_for_updates(target_dir, source)

    if not plan.has_updates:
        print(f"\n✅ Up to date! All {len(plan.unchanged_files)} files match the archive exactly.")
        print("No update download required.")
        return 0

    print("\nUpdate Plan:")
    print(f"  • Unchanged files: {len(plan.unchanged_files)} (preserved, 0 bytes downloaded)")
    print(f"  • Modified files:  {len(plan.modified_files)} (to download)")
    print(f"  • New files:       {len(plan.new_files)} (to download)")
    print(f"  • Obsolete files:  {len(plan.obsolete_files)}" + (" (to remove)" if args.clean else " (kept on disk)"))
    print(f"  • Total update DL: {_format_size(plan.download_bytes_needed)} (out of {_format_size(plan.total_uncompressed_bytes)})")
    print(f"  • Bandwidth saved: {plan.savings_percent:.1f}% ({_format_size(plan.bytes_saved)})")

    if not args.yes:
        try:
            ans = input(f"\nProceed with differential update? [Y/n]: ").strip().lower()
            if ans and ans != "y":
                return 1
        except EOFError:
            return 1

    print("\n[2/2] Streaming updates directly to disk...")

    apply_update_plan(plan, remove_obsolete=args.clean)

    workers = args.workers
    chunk_size = args.chunk_size
    if getattr(args, "speed_mode", "max") == "max":
        if workers == 1:
            workers = 8
        if chunk_size == 262144:
            chunk_size = 1048576

    client = StreamClient(
        chunk_size=chunk_size,
        max_workers=workers,
        timeout=args.timeout,
        verify_crc=not args.no_verify,
        resume=True,
        speed_mode=getattr(args, "speed_mode", "max"),
    )

    def _prog(p: StreamProgress):
        pct = (p.uncompressed_bytes_done / max(1, p.total_uncompressed_bytes)) * 100
        speed_mb = p.net_bytes_per_second / (1024 * 1024)
        fname = Path(p.current_file).name[:30]
        print(f"\rUpdating: {pct:5.1f}% | {speed_mb:5.1f} MB/s | {fname:<30}", end="", flush=True)

    result = client.extract_from_url(source, target_dir, progress_cb=_prog)
    print("\n")
    if result.get("status") == "completed":
        print(f"✅ Folder successfully updated and 100% verified!")
        print(f"   Saved {plan.savings_percent:.1f}% bandwidth ({_format_size(plan.bytes_saved)}) by streaming only changed files.")
        return 0
    else:
        print(f"Update ended with status: {result.get('status')}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="liveextract",
        description="LiveExtract: Zero-overhead streaming archive decompression engine.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect remote or local archive headers in RAM")
    p_inspect.add_argument("source", help="URL or local path to ZIP archive")
    p_inspect.set_defaults(func=cmd_inspect)

    # manifest
    p_manifest = subparsers.add_parser("manifest", help="Generate pre-computed byte-offset manifest.json")
    p_manifest.add_argument("archive", help="Path to local ZIP archive")
    p_manifest.add_argument("-o", "--output", help="Output path for manifest.json")
    p_manifest.set_defaults(func=cmd_manifest)

    # stream
    p_stream = subparsers.add_parser("stream", help="Stream & extract archive directly to disk")
    p_stream.add_argument("source", help="URL or path to archive")
    p_stream.add_argument("--dest", "-d", required=True, help="Destination directory")
    p_stream.add_argument("--manifest", "-m", help="Optional pre-computed manifest.json (Mode A)")
    p_stream.add_argument("--speed-mode", "--mode", dest="speed_mode", choices=["max", "optimized"], default="max", help="Extraction speed profile: 'max' (high concurrency, 1MB buffer, max TCP throughput) or 'optimized' (balanced RAM & CPU)")
    p_stream.add_argument("--chunk-size", type=int, default=262144, help="Streaming chunk size (default: 256KB)")
    p_stream.add_argument("--workers", "-w", type=int, default=1, help="Concurrent workers for parallel range streams (default: 1)")
    p_stream.add_argument("--timeout", type=float, default=20.0, help="Network timeout in seconds")
    p_stream.add_argument("--no-verify", action="store_true", help="Skip CRC-32 integrity validation")
    p_stream.add_argument("--no-resume", action="store_true", help="Disable resumption journal")
    p_stream.set_defaults(func=cmd_stream)

    # repair
    p_repair = subparsers.add_parser("repair", help="Verify and repair an existing folder against an archive link")
    p_repair.add_argument("dest", help="Path to existing extracted folder to verify and repair")
    p_repair.add_argument("--source", "-s", help="URL or path to original archive (auto-detected if omitted)")
    p_repair.add_argument("--speed-mode", "--mode", dest="speed_mode", choices=["max", "optimized"], default="max", help="Speed profile: 'max' or 'optimized'")
    p_repair.add_argument("--chunk-size", type=int, default=262144, help="Streaming chunk size (default: 256KB)")
    p_repair.add_argument("--workers", "-w", type=int, default=1, help="Concurrent workers (default: 1)")
    p_repair.add_argument("--timeout", type=float, default=20.0, help="Network timeout in seconds")
    p_repair.add_argument("--no-verify", action="store_true", help="Skip CRC-32 integrity validation")
    p_repair.add_argument("-y", "--yes", action="store_true", help="Auto-confirm repair download")
    p_repair.set_defaults(func=cmd_repair)

    # update
    p_update = subparsers.add_parser("update", help="Check for differential updates and patch an existing folder")
    p_update.add_argument("dest", help="Path to existing extracted folder to update")
    p_update.add_argument("--source", "-s", help="URL or path to new archive version")
    p_update.add_argument("--clean", action="store_true", help="Remove obsolete files deleted in the new version")
    p_update.add_argument("--speed-mode", "--mode", dest="speed_mode", choices=["max", "optimized"], default="max", help="Speed profile: 'max' or 'optimized'")
    p_update.add_argument("--chunk-size", type=int, default=262144, help="Streaming chunk size (default: 256KB)")
    p_update.add_argument("--workers", "-w", type=int, default=1, help="Concurrent workers (default: 1)")
    p_update.add_argument("--timeout", type=float, default=20.0, help="Network timeout in seconds")
    p_update.add_argument("--no-verify", action="store_true", help="Skip CRC-32 integrity validation")
    p_update.add_argument("-y", "--yes", action="store_true", help="Auto-confirm update download")
    p_update.set_defaults(func=cmd_update)

    # serve
    p_serve = subparsers.add_parser("serve", help="Start byte-range streaming & manifest HTTP server")
    p_serve.add_argument("storage", nargs="?", default=".", help="Directory containing archives to serve")
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    p_serve.add_argument("--port", "-p", type=int, default=8080, help="Bind port (default: 8080)")
    p_serve.set_defaults(func=cmd_serve)

    # gui
    p_gui = subparsers.add_parser("gui", help="Launch LiveExtract graphical desktop launcher")
    p_gui.set_defaults(func=cmd_gui)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
