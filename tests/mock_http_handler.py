"""
In-memory mock HTTP handler simulating RFC 7233 byte-range streaming and manifest APIs.
Runs entirely in-process without network sockets.
"""

import io
import re
import urllib.request
import urllib.response
import email.message
from pathlib import Path
from typing import Optional
from liveextract.server.manifest_service import ManifestService


class MockRangeHTTPHandler(urllib.request.BaseHandler):
    """
    Simulates HTTP server responses for mock:// and http://mock.local/ URLs in-memory.
    """

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir).resolve()
        self.manifest_service = ManifestService(self.storage_dir)

    def default_open(self, req: urllib.request.Request):
        url = req.full_url
        if not (url.startswith("mock://") or url.startswith("http://mock.local/")):
            return None  # Let standard handlers handle other URLs

        # Strip prefix to get path
        path = re.sub(r"^(mock://[^/]*|http://mock\.local)", "", url)
        if not path.startswith("/"):
            path = "/" + path

        method = req.get_method()

        # Route 1: Health check
        if path in ("/health", "/healthz"):
            headers = email.message.EmailMessage()
            headers["Content-Type"] = "application/json"
            return urllib.response.addinfourl(
                io.BytesIO(b'{"status":"healthy"}'), headers, url, 200
            )

        # Route 2: Manifest endpoint: /archives/<name>/manifest
        manifest_match = re.match(r"^/archives/([^/]+)/manifest$", path)
        if manifest_match:
            filename = manifest_match.group(1)
            manifest = self.manifest_service.get_manifest(filename)
            if not manifest:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

            manifest_bytes = manifest.to_json().encode("utf-8")
            headers = email.message.EmailMessage()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(manifest_bytes))
            return urllib.response.addinfourl(
                io.BytesIO(manifest_bytes), headers, url, 200
            )

        # Route 3: File stream
        stream_match = re.match(r"^/archives/([^/]+)/stream$", path)
        if stream_match:
            filename = stream_match.group(1)
        else:
            filename = path.lstrip("/")

        file_path = (self.storage_dir / filename).resolve()
        if not file_path.exists() or not file_path.is_file():
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        file_bytes = file_path.read_bytes()
        file_size = len(file_bytes)
        range_header = req.get_header("Range")

        headers = email.message.EmailMessage()
        headers["Accept-Ranges"] = "bytes"
        headers["Content-Type"] = "application/octet-stream"

        if method == "HEAD":
            headers["Content-Length"] = str(file_size)
            return urllib.response.addinfourl(io.BytesIO(b""), headers, url, 200)

        if not range_header:
            headers["Content-Length"] = str(file_size)
            return urllib.response.addinfourl(io.BytesIO(file_bytes), headers, url, 200)

        # Parse range header
        range_match = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
        if not range_match:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

        start_str, end_str = range_match.groups()
        if not start_str:
            length = int(end_str)
            start = max(0, file_size - length)
            end = file_size - 1
        elif not end_str:
            start = int(start_str)
            end = file_size - 1
        else:
            start = int(start_str)
            end = min(file_size - 1, int(end_str))

        if start > end or start >= file_size:
            headers["Content-Range"] = f"bytes */{file_size}"
            raise urllib.error.HTTPError(url, 416, "Range Not Satisfiable", headers, None)

        slice_data = file_bytes[start:end + 1]
        headers["Content-Length"] = str(len(slice_data))
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        return urllib.response.addinfourl(io.BytesIO(slice_data), headers, url, 206)


def create_mock_opener(storage_dir: Path) -> urllib.request.OpenerDirector:
    """Build an OpenerDirector configured with MockRangeHTTPHandler."""
    handler = MockRangeHTTPHandler(storage_dir)
    return urllib.request.build_opener(handler)
