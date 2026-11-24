"""The web server: FastAPI served by uvicorn. It answers /ping and passes pushes to a handler."""

import asyncio
import io
import logging
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import BinaryIO

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from tfs.web.overview import Overview

PushHandler = Callable[[str, BinaryIO, Mapping[str, str]], tuple[int, dict]]

# A refused push may leave body bytes unread, so no reply keeps the connection open.
CLOSE = {"Connection": "close"}

DASHBOARD_DIR = Path(__file__).parent / "dashboard"
BUILD_HINT = "The dashboard is not built. Run: npm --prefix dashboard install && npm --prefix dashboard run build"

logger = logging.getLogger(__name__)


class _Body(io.RawIOBase):
    """The request body as a file. Reads run in a thread and pull chunks from the event loop."""

    def __init__(
        self,
        chunks: AsyncIterator[bytes],
        loop: asyncio.AbstractEventLoop,
        max_body: int,
        read_timeout_s: float,
    ) -> None:
        super().__init__()
        self._chunks = chunks
        self._loop = loop
        self._max_body = max_body
        self._read_timeout_s = read_timeout_s
        self._received = 0
        self._pending = b""

    def readable(self) -> bool:
        """Say that the body can be read."""
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        """Fill buffer with body bytes. Raise HTTPException if the body stops or grows too big."""
        if not self._pending:
            self._pending = self._next_chunk()
        count = min(len(buffer), len(self._pending))
        buffer[:count] = self._pending[:count]
        self._pending = self._pending[count:]
        return count

    def _next_chunk(self) -> bytes:
        async def pull() -> bytes:
            return await asyncio.wait_for(anext(self._chunks, b""), self._read_timeout_s)

        try:
            chunk = asyncio.run_coroutine_threadsafe(pull(), self._loop).result()
        except (TimeoutError, ClientDisconnect) as error:
            raise HTTPException(400, "The body stopped before all of it arrived.") from error
        self._received += len(chunk)
        if self._received > self._max_body:
            raise _too_large(self._max_body)
        return chunk


def _too_large(max_body: int) -> HTTPException:
    return HTTPException(413, f"The body is larger than the {max_body} bytes this server accepts.")


def _add_dashboard(
    app: FastAPI,
    check_host: Callable[[Request], Awaitable[None]],
    overview: Callable[[], Overview] | None,
) -> None:
    @app.get("/")
    async def dashboard() -> Response:
        if not (DASHBOARD_DIR / "index.html").is_file():
            return PlainTextResponse(BUILD_HINT)
        return FileResponse(DASHBOARD_DIR / "index.html")

    if overview:
        app.get("/api/overview", response_model=Overview, response_model_exclude_unset=True)(
            overview
        )

    if (DASHBOARD_DIR / "assets").is_dir():

        class GuardedFiles(StaticFiles):
            # A mounted app skips the app's dependencies, so the Host check runs here.
            async def get_response(self, path, scope):
                await check_host(Request(scope))
                return await super().get_response(path, scope)

        app.mount("/assets", GuardedFiles(directory=DASHBOARD_DIR / "assets"))


def lan_address() -> str:
    """The address a phone on the same Wi-Fi reaches this computer at."""
    # A UDP connect sends nothing. It only makes the OS pick the Wi-Fi address.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
        except OSError:
            return socket.gethostbyname(socket.gethostname())
        return probe.getsockname()[0]


def own_addresses() -> set[str]:
    """This computer's IPv4 addresses, including the one setup prints."""
    try:
        found = {
            info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        }
    except socket.gaierror:
        found = set()
    return found | {lan_address()}


def _make_app(
    push_handler: PushHandler,
    push_only: bool,
    max_body: int,
    read_timeout_s: float,
    overview: Callable[[], Overview] | None,
) -> FastAPI:
    own_names = {"localhost", "127.0.0.1", socket.gethostname().lower(), *own_addresses()}

    async def check_host(request: Request) -> None:
        host = request.headers.get("host", "").rsplit(":", 1)[0].lower()
        if host.removesuffix(".local") not in own_names and not host.endswith(".ts.net"):
            raise HTTPException(421, "This server answers only to this computer's own name.")

    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(check_host)]
    )

    @app.exception_handler(StarletteHTTPException)
    async def refuse(request: Request, error: StarletteHTTPException) -> Response:
        return JSONResponse({"ok": False, "error": error.detail}, error.status_code, CLOSE)

    if not push_only:

        @app.get("/ping")
        async def ping() -> Response:
            return PlainTextResponse("tfs")

        _add_dashboard(app, check_host, overview)

    @app.post("/push/{name}")
    async def push(name: str, request: Request) -> Response:
        if int(request.headers.get("content-length", 0)) > max_body:
            raise _too_large(max_body)
        loop = asyncio.get_running_loop()
        body = io.BufferedReader(_Body(request.stream(), loop, max_body, read_timeout_s))
        try:
            status, payload = await run_in_threadpool(push_handler, name, body, request.headers)
            return JSONResponse(payload, HTTPStatus(status), CLOSE)
        except HTTPException:
            raise
        except Exception as error:
            # Handler errors may echo a token. Only this fixed line is logged.
            logger.error("The push handler failed.")
            raise HTTPException(500, "The push could not be handled. See the TFS log.") from error

    return app


class WebServer(uvicorn.Server):
    """A uvicorn server that holds its port from creation until stop()."""

    def __init__(self, app: FastAPI, sock: socket.socket) -> None:
        super().__init__(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False))
        self.sock = sock
        self._thread = threading.Thread(target=self.run, args=([sock],))

    def start(self) -> None:
        """Serve in a thread."""
        self._thread.start()

    def stop(self) -> None:
        """Finish the requests in progress, then release the port."""
        self.should_exit = True
        if self._thread.is_alive():
            self._thread.join()
        self.sock.close()


def make_server(
    host: str,
    port: int,
    *,
    push_handler: PushHandler,
    push_only: bool = False,
    overview: Callable[[], Overview] | None = None,
    max_body: int = 1 << 30,
    read_timeout_s: float = 30,
) -> WebServer:
    """Bind host and port, or raise OSError. push_only drops /ping and the screen routes. The caller starts and stops it."""
    return WebServer(
        _make_app(push_handler, push_only, max_body, read_timeout_s, overview),
        socket.create_server((host, port)),
    )
