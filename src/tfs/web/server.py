"""A small HTTP server that answers /ping and passes well-framed pushes to a handler."""

import contextlib
import io
import json
import logging
import os
import re
from collections.abc import Callable
from http import HTTPStatus
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import BinaryIO

PushHandler = Callable[[str, BinaryIO, HTTPMessage], tuple[int, dict]]

PUSH_PATH = re.compile(r"/push/(?P<name>[A-Za-z0-9_-]+)")
JSON_TYPE = "application/json"
# Lengths with more digits than this are far above any body this server will take.
MAX_LENGTH_DIGITS = 18

logger = logging.getLogger(__name__)


class IncompleteBodyError(Exception):
    """The client stopped sending before Content-Length bytes arrived."""


class _RefusalError(Exception):
    """A request the server turns away. The message goes to the client."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Body(io.RawIOBase):
    """Exactly `length` bytes of the request body, read straight from the socket."""

    def __init__(self, source: BinaryIO, length: int) -> None:
        super().__init__()
        self._source = source
        self._remaining = length

    def readable(self) -> bool:
        """Say that the body can be read."""
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        """Fill buffer with body bytes. Raise IncompleteBodyError if the client stops early."""
        view = memoryview(buffer)[: self._remaining]
        if not view:
            return 0
        try:
            count = self._source.readinto1(view)
        except OSError as error:
            raise IncompleteBodyError(
                "The client stopped sending before the whole body arrived."
            ) from error
        if count == 0:
            raise IncompleteBodyError(
                "The client closed the connection before the whole body arrived."
            )
        self._remaining -= count
        return count


def _content_length(headers: HTTPMessage, max_body: int) -> int:
    """Return the one valid Content-Length, or raise the refusal the request earns."""
    if headers.get_all("Transfer-Encoding") is not None:
        raise _RefusalError(
            411, "Send the body with a Content-Length. Chunked bodies are not accepted."
        )
    values = {value.strip(" \t") for value in headers.get_all("Content-Length", [])}
    if not values:
        raise _RefusalError(411, "Send a Content-Length header.")
    if len(values) > 1:
        raise _RefusalError(400, "Send Content-Length once, with one value.")
    (value,) = values
    if not (value.isascii() and value.isdigit()):
        raise _RefusalError(400, "Content-Length must be a whole number of bytes.")
    digits = value.lstrip("0")
    length = int(digits or "0") if len(digits) <= MAX_LENGTH_DIGITS else None
    if length is None or length > max_body:
        raise _RefusalError(
            413, f"The body is larger than the {max_body} bytes this server accepts."
        )
    return length


class _Handler(BaseHTTPRequestHandler):
    server: "_PushServer"
    # HTTP/1.0 closes every connection. A refused request never leaves unread body bytes
    # behind to be read as the next request.
    protocol_version = "HTTP/1.0"

    def setup(self) -> None:
        """Apply the server's read timeout to this connection."""
        self.timeout = self.server.read_timeout_s
        super().setup()

    def handle(self) -> None:
        """Serve one request. A client that goes away mid-request ends it quietly."""
        with contextlib.suppress(OSError):
            super().handle()

    def log_message(self, format: str, *args) -> None:
        """Write no log lines. They would carry client addresses and request lines."""

    def _get(self) -> None:
        """Answer /ping."""
        if self.path == "/ping":
            self._send(200, b"tfs", "text/plain; charset=utf-8")
        else:
            self._refuse(404, "Nothing is served at this address.")

    def _post(self) -> None:
        """Pass a well-framed push to the push handler."""
        match = PUSH_PATH.fullmatch(self.path)
        if match is None:
            self._refuse(404, "Nothing is served at this address.")
            return
        try:
            length = _content_length(self.headers, self.server.max_body)
        except _RefusalError as refusal:
            self._refuse(refusal.status, str(refusal))
            return
        body = _Body(self.rfile, length)
        try:
            status, payload = self.server.push_handler(match["name"], body, self.headers)
            reply = (HTTPStatus(status), json.dumps(payload).encode())
        except IncompleteBodyError:
            self._refuse(400, "The body stopped before all Content-Length bytes arrived.")
        except Exception:
            logger.exception("The push handler failed.")
            self._refuse(500, "The push could not be handled. See the TFS log.")
        else:
            self._send(*reply, JSON_TYPE)

    def _refuse(self, status: int, message: str) -> None:
        self._send(status, json.dumps({"ok": False, "error": message}).encode(), JSON_TYPE)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# http.server finds its handlers by the names do_GET and do_POST.
_Handler.do_GET = _Handler._get
_Handler.do_POST = _Handler._post


class _PushServer(ThreadingHTTPServer):
    # On Windows a second process can bind a port that is already in use unless this is off.
    allow_reuse_address = os.name != "nt"
    daemon_threads = False
    block_on_close = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        push_handler: PushHandler,
        max_body: int,
        read_timeout_s: float,
    ) -> None:
        self.push_handler = push_handler
        self.max_body = max_body
        self.read_timeout_s = read_timeout_s
        super().__init__(address, _Handler)


def make_server(
    host: str,
    port: int,
    *,
    push_handler: PushHandler,
    max_body: int = 1 << 30,
    read_timeout_s: float = 30,
) -> ThreadingHTTPServer:
    """Bind the server to host and port. The caller runs serve_forever() and closes it."""
    if not read_timeout_s > 0:
        raise ValueError("read_timeout_s must be above zero.")
    if max_body < 0:
        raise ValueError("max_body cannot be negative.")
    return _PushServer(
        (host, port), push_handler=push_handler, max_body=max_body, read_timeout_s=read_timeout_s
    )
