"""
Lightweight HTTP server supporting byte-range streaming and manifest endpoints.
Uses Python standard library socketserver and http.server.
"""

import os
import re
import json
import socketserver
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from liveextract.server.manifest_service import ManifestService


class RangeHTTPRequestHandler(BaseHTTPRequestHandler):
    """
    HTTP handler with full RFC 7233 byte-range support and manifest routing.
    """
    storage_dir: Path = Path(".")
    manifest_service: Optional[ManifestService] = None

    def do_HEAD(self):
        stream_match = re.match(r"^/archives/([^/]+)/stream$", self.path)
        requested_file = stream_match.group(1) if stream_match else self.path.lstrip("/")
        self._serve_file_with_ranges(requested_file, send_body=False)

    def do_GET(self):
        # Route 1: Health check
        if self.path in ("/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","engine":"LiveExtract"}')
            return

        # Route 2: Manifest endpoint: /archives/<filename>/manifest
        manifest_match = re.match(r"^/archives/([^/]+)/manifest$", self.path)
        if manifest_match:
            filename = manifest_match.group(1)
            manifest = self.manifest_service.get_manifest(filename)
            if not manifest:
                self.send_error(404, f"Archive not found: {filename}")
                return

            manifest_json = manifest.to_json().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(manifest_json)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(manifest_json)
            return

        # Route 3: File stream endpoint: /archives/<filename>/stream or direct file
        stream_match = re.match(r"^/archives/([^/]+)/stream$", self.path)
        if stream_match:
            requested_file = stream_match.group(1)
        else:
            requested_file = self.path.lstrip("/")

        self._serve_file_with_ranges(requested_file, send_body=True)

    def _serve_file_with_ranges(self, filename: str, send_body: bool = True):
        file_path = (self.storage_dir / filename).resolve()
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, f"File not found: {filename}")
            return

        file_size = file_path.stat().st_size
        range_header = self.headers.get("Range")

        if not range_header:
            # Full file download
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            if send_body:
                with open(file_path, "rb") as f:
                    self._copy_stream(f, self.wfile, file_size)
            return

        # Parse Range header: e.g. bytes=0-499 or bytes=-500 or bytes=500-
        range_match = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
        if not range_match:
            self.send_error(400, f"Malformed Range header: {range_header}")
            return

        start_str, end_str = range_match.groups()
        if not start_str and not end_str:
            self.send_error(400, "Empty range requested")
            return

        if not start_str:
            # Suffix range: bytes=-500 (last 500 bytes)
            length = int(end_str)
            start = max(0, file_size - length)
            end = file_size - 1
        elif not end_str:
            # Prefix range: bytes=500-
            start = int(start_str)
            end = file_size - 1
        else:
            # Absolute range: bytes=100-200
            start = int(start_str)
            end = min(file_size - 1, int(end_str))

        if start > end or start >= file_size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.end_headers()
            return

        content_length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        if send_body:
            with open(file_path, "rb") as f:
                f.seek(start)
                self._copy_stream(f, self.wfile, content_length)

    def _copy_stream(self, src_file, dst_wfile, length: int, chunk_size: int = 65536):
        remaining = length
        while remaining > 0:
            to_read = min(chunk_size, remaining)
            chunk = src_file.read(to_read)
            if not chunk:
                break
            try:
                dst_wfile.write(chunk)
                remaining -= len(chunk)
            except (ConnectionResetError, BrokenPipeError):
                break

    def log_message(self, format, *args):
        # Silence default standard logging unless debug mode enabled
        pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(storage_dir: Path, host: str = "127.0.0.1", port: int = 0) -> ThreadedHTTPServer:
    """Create and configure a threaded HTTP test/production server."""
    handler = RangeHTTPRequestHandler
    handler.storage_dir = Path(storage_dir).resolve()
    handler.manifest_service = ManifestService(handler.storage_dir)
    return ThreadedHTTPServer((host, port), handler)
