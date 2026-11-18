"""
High-performance HTTP/1.1 Keep-Alive Range Reader and file offset reader.
Reuses persistent TCP + TLS connections across requests to eliminate repeated TLS handshake stalls.
"""

import http.client
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Generator, Optional, Tuple


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}


class RangeReadError(IOError):
    """Raised when an HTTP Range request fails or server does not support byte ranges."""
    pass


class RangeReader:
    def __init__(
        self,
        source: str,
        max_retries: int = 4,
        backoff_sec: float = 0.5,
        timeout: float = 20.0,
        opener: Optional[urllib.request.OpenerDirector] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ):
        self.source = source
        self.max_retries = max_retries
        self.backoff_sec = backoff_sec
        self.timeout = timeout
        self.opener = opener
        self.headers = dict(DEFAULT_HEADERS)
        if custom_headers:
            self.headers.update(custom_headers)

        self._total_size: Optional[int] = None
        self._effective_url: str = source
        self._suggested_filename: Optional[str] = None

        self._conn: Optional[http.client.HTTPConnection] = None
        self._conn_key: Optional[Tuple[str, str, int]] = None
        self._ssl_ctx: Optional[ssl.SSLContext] = None

        if source.startswith("file://"):
            clean_path = source[7:]
            if clean_path.startswith("/") and len(clean_path) > 2 and clean_path[2] == ":":
                clean_path = clean_path[1:]
            self.local_path = Path(clean_path)
            self.is_url = False
        elif source.startswith("http://") or source.startswith("https://") or source.startswith("mock://"):
            self.local_path = None
            self.is_url = True
        else:
            self.local_path = Path(source)
            self.is_url = False

    @property
    def suggested_filename(self) -> Optional[str]:
        return self._suggested_filename

    @property
    def effective_url(self) -> str:
        return self._effective_url

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._conn_key = None

    def __del__(self):
        self.close()

    def _get_connection(self, parsed: urllib.parse.ParseResult) -> http.client.HTTPConnection:
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        key = (scheme, host, port)

        if self._conn is not None and self._conn_key == key and self._conn.sock is not None:
            return self._conn

        self.close()
        if scheme == "https":
            if self._ssl_ctx is None:
                self._ssl_ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._ssl_ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)

        conn.connect()
        if conn.sock is not None:
            try:
                conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
            except OSError:
                pass

        self._conn = conn
        self._conn_key = key
        return conn

    def _http_request(
        self,
        url: str,
        method: str = "GET",
        extra_headers: Optional[Dict[str, str]] = None,
        max_redirects: int = 5,
    ) -> Tuple[int, Any, Any, str]:
        if self.opener is not None or url.startswith("mock://"):
            req = urllib.request.Request(url, method=method)
            for k, v in self.headers.items():
                req.add_header(k, v)
            if extra_headers:
                for k, v in extra_headers.items():
                    req.add_header(k, v)
            resp = self.opener.open(req, timeout=self.timeout) if self.opener else urllib.request.urlopen(req, timeout=self.timeout)
            status = getattr(resp, "status", getattr(resp, "code", 200))
            final_url = getattr(resp, "url", getattr(resp, "full_url", url))
            return status, resp.headers, resp, final_url

        current_url = url
        for _ in range(max_redirects + 1):
            parsed = urllib.parse.urlsplit(current_url)
            target_path = parsed.path or "/"
            if parsed.query:
                target_path = f"{target_path}?{parsed.query}"

            req_headers = dict(self.headers)
            if extra_headers:
                req_headers.update(extra_headers)

            resp = None
            for conn_try in range(2):
                conn = self._get_connection(parsed)
                try:
                    conn.request(method, target_path, headers=req_headers)
                    resp = conn.getresponse()
                    break
                except (http.client.HTTPException, OSError):
                    self.close()
                    if conn_try == 1:
                        raise

            assert resp is not None
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                try:
                    resp.read()
                except Exception:
                    self.close()
                if not loc:
                    return resp.status, resp.headers, resp, current_url
                current_url = urllib.parse.urljoin(current_url, loc)
                continue

            return resp.status, resp.headers, resp, current_url

        raise RangeReadError(f"Too many redirects following {url}")

    def _extract_filename_from_headers(self, headers) -> Optional[str]:
        cd = headers.get("Content-Disposition", "")
        if not cd:
            return None

        m_star = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;\s]+)", cd, re.IGNORECASE)
        if m_star:
            return urllib.parse.unquote(m_star.group(1).strip('"\''))

        m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r'filename\s*=\s*([^;\s]+)', cd)
        if m:
            return m.group(1).strip('"\'')

        return None

    def get_total_size(self) -> int:
        if self._total_size is not None:
            return self._total_size

        if not self.is_url:
            if not self.local_path.exists():
                raise RangeReadError(f"Local file not found: {self.source}")
            self._total_size = self.local_path.stat().st_size
            self._suggested_filename = self.local_path.name
            return self._total_size

        try:
            status, headers, resp, final_url = self._http_request(
                self._effective_url, method="GET", extra_headers={"Range": "bytes=0-0"}
            )
            self._effective_url = final_url
            self._suggested_filename = self._extract_filename_from_headers(headers)

            cr = headers.get("Content-Range")
            if status == 206:
                try:
                    resp.read()
                    if self.opener is not None or self._effective_url.startswith("mock://"):
                        resp.close()
                except Exception:
                    self.close()
            else:
                try:
                    resp.close()
                except Exception:
                    pass
                self.close()

            if cr and "/" in cr:
                total_part = cr.split("/")[-1].strip()
                if total_part != "*":
                    self._total_size = int(total_part)
                    return self._total_size

            length_hdr = headers.get("Content-Length")
            if length_hdr and status == 200:
                self._total_size = int(length_hdr)
                return self._total_size
        except Exception:
            self.close()

        try:
            status, headers, resp, final_url = self._http_request(self._effective_url, method="HEAD")
            try:
                resp.read()
            except Exception:
                self.close()
            self._effective_url = final_url
            if not self._suggested_filename:
                self._suggested_filename = self._extract_filename_from_headers(headers)
            length_hdr = headers.get("Content-Length")
            if length_hdr:
                self._total_size = int(length_hdr)
                return self._total_size
        except Exception as err:
            self.close()
            raise RangeReadError(
                f"Failed to query archive size from {self.source}: {err}"
            ) from err

        raise RangeReadError(f"Server at {self.source} did not provide archive size or byte-range support")

    def read_tail(self, tail_size: int) -> Tuple[bytes, int]:
        total = self.get_total_size()
        actual_fetch = min(total, tail_size)
        start_offset = total - actual_fetch
        data = self.read_range(start_offset, total - 1)
        return data, total

    def read_range(self, start: int, end: int) -> bytes:
        if start > end:
            return b""
        chunks = list(self.stream_range(start, end, chunk_size=min(1048576, end - start + 1)))
        return b"".join(chunks)

    def stream_range(
        self,
        start: int,
        end: int,
        chunk_size: int = 262144,
    ) -> Generator[bytes, None, None]:
        if start > end:
            return

        if not self.is_url:
            with open(self.local_path, "rb") as f:
                f.seek(start)
                bytes_left = end - start + 1
                while bytes_left > 0:
                    read_len = min(chunk_size, bytes_left)
                    chunk = f.read(read_len)
                    if not chunk:
                        break
                    bytes_left -= len(chunk)
                    yield chunk
            return

        attempt = 0
        current_start = start

        while current_start <= end:
            attempt += 1
            try:
                status, headers, resp, final_url = self._http_request(
                    self._effective_url,
                    method="GET",
                    extra_headers={"Range": f"bytes={current_start}-{end}"},
                )
                self._effective_url = final_url

                if status not in (200, 206):
                    self.close()
                    raise RangeReadError(f"Unexpected HTTP status: {status}")

                conn_hdr = (headers.get("Connection") or "").lower()

                while current_start <= end:
                    read_len = min(chunk_size, end - current_start + 1)
                    chunk = resp.read(read_len)
                    if not chunk:
                        break
                    current_start += len(chunk)
                    yield chunk

                if conn_hdr == "close" or current_start <= end:
                    self.close()
                elif hasattr(resp, "close") and (self.opener is not None or self._effective_url.startswith("mock://")):
                    resp.close()

                if current_start > end:
                    break
                raise RangeReadError(f"Premature EOF at byte {current_start}/{end}")
            except Exception as err:
                self.close()
                if attempt >= self.max_retries:
                    raise RangeReadError(
                        f"Failed reading range {start}-{end} from {self.source} after {attempt} attempts: {err}"
                    ) from err
                time.sleep(self.backoff_sec * (2 ** (attempt - 1)))
