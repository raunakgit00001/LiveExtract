"""
LiveExtract command-line interface.
Commands:
  inspect   - Inspect remote or local archive structure in RAM without downloading.
  manifest  - Generate pre-flight byte-offset manifest.json from an archive.
  stream    - Stream & extract directly to disk with zero archive storage overhead.
  serve     - Start byte-range enabled HTTP file & manifest server.
  gui       - Launch the LiveExtract desktop application.
"""

import argparse
import sys
import time
from pathlib import Path

from liveextract.core.manifest import generate_manifest_from_zip, ArchiveManifest
from liveextract.client.range_reader import RangeReader
from liveextract.client.stream_client import StreamClient, StreamProgress
from liveextract.server.api import create_server


def _format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0 or unit == "TB":
            return f"{size_bytes:6.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def cmd_inspect(args: argparse.Namespace) -> int:
    source = args.source
    print(f"\n[LiveExtract] Inspecting archive: {source}")
    t0 = time.time()

    reader = RangeReader(source)
    try:
        total_size = reader.get_total_size()
        print(f"Archive total size: {_format_size(total_size)} ({total_size:,} bytes)")

        head_bytes = reader.read_range(0, min(total_size - 1, 15))
        from liveextract.core.rar_parser import RarBinaryParser
        if RarBinaryParser.is_rar(head_bytes):
            full_head = reader.read_range(0, min(total_size - 1, 131072))
            rar_info = RarBinaryParser.inspect(full_head)
            elapsed_ms = (time.time() - t0) * 1000
            print(f"Format:             {rar_info.version}")
            print(f"Archive Type:       {'SOLID RAR' if rar_info.is_solid else 'STANDARD (Non-Solid) RAR'}")
            print(f"Dictionary Size:    {_format_size(rar_info.dictionary_size_bytes)}")
            print(f"Inspection Time:    {elapsed_ms:.1f} ms\n")
            return 0

        tail_bytes, _ = reader.read_tail(65536 + 22)
        from liveextract.core.binary_parser import ZipBinaryParser
        eocd = ZipBinaryParser.find_eocd(tail_bytes, archive_size=total_size)
        print("Format:             ZIP Archive")
        print(f"Central Directory:  offset={eocd.cd_offset:,}, size={eocd.cd_size:,} bytes, entries={eocd.total_records}")

        cd_bytes = reader.read_range(eocd.cd_offset, eocd.cd_offset + eocd.cd_size - 1)
        entries = ZipBinaryParser.parse_central_directory(cd_bytes)
    finally:
        reader.close()

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
    archive_path = Path(args.archive)
    if not archive_path.exists():
        print(f"Error: Archive not found: {archive_path}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else archive_path.with_suffix(".manifest.json")
    t0 = time.time()
    manifest = generate_manifest_from_zip(archive_path)
    manifest.save(output_path)
    elapsed = time.time() - t0

    print(f"Manifest written to: {output_path}")
    print(f"Mapped {manifest.total_files} files ({_format_size(manifest.total_uncompressed_size)}) in {elapsed * 1000:.1f} ms")
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    source = args.source
    dest_dir = Path(args.dest).resolve()
    manifest_path = Path(args.manifest) if args.manifest else None

    print(f"\nLiveExtract Streaming Engine")
    print(f"  Source:      {source}")
    print(f"  Destination: {dest_dir}\n")

    client = StreamClient(
        chunk_size=args.chunk_size,
        max_workers=args.workers,
        timeout=args.timeout,
        verify_crc=not args.no_verify,
        resume=not args.no_resume,
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
        speed_mb = (p.net_bytes_per_second or p.bytes_per_second) / (1024 * 1024)

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

        print("\n\nExtraction completed.")
        print(f"  Files Extracted:      {res['total_files']}")
        print(f"  Uncompressed Data:    {_format_size(res['uncompressed_bytes'])}")
        print(f"  Archive Disk Usage:   0.00 B")
        print(f"  Elapsed Time:         {res['elapsed_seconds']:.2f} s\n")
        return 0
    except Exception as err:
        print(f"\n\nExtraction failed: {err}", file=sys.stderr)
        return 1


def cmd_serve(args: argparse.Namespace) -> int:
    storage_dir = Path(args.storage).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)

    server = create_server(storage_dir, host=args.host, port=args.port)
    actual_port = server.server_address[1]
    print(f"\nLiveExtract Server listening on http://{args.host}:{actual_port}/")
    print(f"Serving directory: {storage_dir}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    try:
        from gui.app import launch_gui
        launch_gui()
        return 0
    except ImportError as e:
        print(f"Error launching GUI: {e}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="liveextract",
        description="LiveExtract: Zero-disk streaming archive downloader and extractor.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_inspect = subparsers.add_parser("inspect", help="Inspect archive metadata in RAM without downloading")
    p_inspect.add_argument("source", help="URL or local path to archive")
    p_inspect.set_defaults(func=cmd_inspect)

    p_manifest = subparsers.add_parser("manifest", help="Generate pre-computed byte-offset manifest.json")
    p_manifest.add_argument("archive", help="Path to local ZIP archive")
    p_manifest.add_argument("-o", "--output", help="Output path for manifest.json")
    p_manifest.set_defaults(func=cmd_manifest)

    p_stream = subparsers.add_parser("stream", help="Stream & extract archive directly to disk")
    p_stream.add_argument("source", help="URL or path to archive")
    p_stream.add_argument("--dest", "-d", required=True, help="Destination directory")
    p_stream.add_argument("--manifest", "-m", help="Optional pre-computed manifest.json (Mode A)")
    p_stream.add_argument("--chunk-size", type=int, default=262144, help="Streaming chunk size (default: 256KB)")
    p_stream.add_argument("--workers", "-w", type=int, default=4, help="Parallel connections (default: 4)")
    p_stream.add_argument("--timeout", type=float, default=20.0, help="Network timeout in seconds")
    p_stream.add_argument("--no-verify", action="store_true", help="Skip CRC-32 integrity validation")
    p_stream.add_argument("--no-resume", action="store_true", help="Disable resumption journal")
    p_stream.set_defaults(func=cmd_stream)

    p_serve = subparsers.add_parser("serve", help="Start byte-range streaming & manifest HTTP server")
    p_serve.add_argument("storage", nargs="?", default=".", help="Directory containing archives to serve")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", "-p", type=int, default=8080, help="Bind port (default: 8080)")
    p_serve.set_defaults(func=cmd_serve)

    p_gui = subparsers.add_parser("gui", help="Launch LiveExtract desktop application")
    p_gui.set_defaults(func=cmd_gui)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
