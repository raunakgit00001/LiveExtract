"""
Unit tests for CDN tunnels, download proxies, and signed URLs with query parameters.
"""

import io
import unittest
import urllib.request
import urllib.response
import email.message
import tempfile
import shutil
from pathlib import Path
from tests.make_test_archive import write_sample_zip_file
from liveextract.client.range_reader import RangeReader
from liveextract.client.stream_client import StreamClient


class MockProxyHandler(urllib.request.BaseHandler):
    """
    Simulates a CDN download proxy / tunnel endpoint:
    - URL path has no .zip/.rar extension (e.g. /download/<token>?sig=...)
    - Rejects HEAD requests (only accepts GET)
    - Returns Content-Disposition with original filename
    - Rejects suffix ranges (Range: bytes=-65536) to test positive offset fallback
    """

    def __init__(self, sample_zip_path: Path, expected_token: str = "SampleToken12345"):
        self.zip_bytes = sample_zip_path.read_bytes()
        self.zip_size = len(self.zip_bytes)
        self.expected_token = expected_token

    def default_open(self, req: urllib.request.Request):
        url = req.full_url
        if not url.startswith("mock://tunnel/download/"):
            return None

        # Disallow HEAD request (simulate S3 / CDN presigned token rejection on HEAD)
        if req.get_method() == "HEAD":
            raise urllib.error.HTTPError(url, 403, "Forbidden: HEAD method not signed", {}, None)

        range_hdr = req.get_header("Range")
        headers = email.message.EmailMessage()
        headers["Content-Disposition"] = 'attachment; filename="GameArchive.zip"'
        headers["Accept-Ranges"] = "bytes"
        headers["Content-Type"] = "application/octet-stream"

        if not range_hdr:
            headers["Content-Length"] = str(self.zip_size)
            return urllib.response.addinfourl(io.BytesIO(self.zip_bytes), headers, url, 200)

        # Reject negative suffix range (e.g. bytes=-65536) to simulate proxy limitation
        if range_hdr.startswith("bytes=-"):
            raise urllib.error.HTTPError(url, 400, "Bad Request: Suffix ranges not supported", {}, None)

        import re
        m = re.match(r"^bytes=(\d+)-(\d+)$", range_hdr.strip())
        if not m:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

        start = int(m.group(1))
        end = min(self.zip_size - 1, int(m.group(2)))
        slice_data = self.zip_bytes[start:end + 1]

        headers["Content-Length"] = str(len(slice_data))
        headers["Content-Range"] = f"bytes {start}-{end}/{self.zip_size}"
        return urllib.response.addinfourl(io.BytesIO(slice_data), headers, url, 206)


class TestProxyTunnels(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.zip_file = self.temp_root / "sample.zip"
        write_sample_zip_file(self.zip_file, num_files=3, file_size_kb=8)

        self.handler = MockProxyHandler(self.zip_file)
        self.opener = urllib.request.build_opener(self.handler)
        self.proxy_url = "mock://tunnel/download/SampleToken12345?sig=TestSig123"

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_content_disposition_filename_extraction(self):
        reader = RangeReader(self.proxy_url, opener=self.opener)
        total_size = reader.get_total_size()
        self.assertEqual(total_size, len(self.handler.zip_bytes))
        self.assertEqual(reader.suggested_filename, "GameArchive.zip")

    def test_streaming_extraction_from_proxy_url(self):
        dest_dir = self.temp_root / "extracted"
        client = StreamClient(opener=self.opener)
        res = client.extract_from_url(
            archive_source=self.proxy_url,
            target_dir=dest_dir,
        )

        self.assertEqual(res["status"], "completed")
        self.assertTrue((dest_dir / "docs" / "readme.txt").exists())
        self.assertTrue((dest_dir / "bin" / "app.cfg").exists())


if __name__ == "__main__":
    unittest.main()
