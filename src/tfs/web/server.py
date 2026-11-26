"""The web server: FastAPI served by uvicorn. It answers /ping and passes pushes to a handler."""

import asyncio
import io
import logging
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import BinaryIO, TypedDict, Unpack

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from uvicorn.protocols.http.h11_impl import H11Protocol

from tfs.web import pairing, setup_page
from tfs.web.overview import Overview
from tfs.web.pairing import lan_address
from tfs.web.settings import Settings

PushHandler = Callable[[str, BinaryIO, Mapping[str, str]], tuple[int, dict]]

# A refused push may leave body bytes unread, so no reply keeps the connection open.
CLOSE = {"Connection": "close"}

DASHBOARD_DIR = Path(__file__).parent / "dashboard"
BUILD_HINT = "The dashboard is not built. Run: npm --prefix dashboard install && npm --prefix dashboard run build"

logger = logging.getLogger(__name__)


@dataclass
class HomeWifi:
    """The pairing database and optional public setup link on this listener."""

    db: Path
    setup: tuple[str, Callable[[], Response]] | None = None
    pairing_scan: tuple[str, threading.Event] | None = None


class DashboardReaders(TypedDict, total=False):
    """The read-only routes available on this listener."""

    overview: Callable[[], Overview]
    settings: Callable[[bool], Settings]


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
    settings: Callable[[bool], Settings] | None,
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

    if settings:
        app.get("/api/settings", response_model=Settings)(settings)

    if (DASHBOARD_DIR / "assets").is_dir():

        class GuardedFiles(StaticFiles):
            # A mounted app skips the app's dependencies, so the Host check runs here.
            async def get_response(self, path, scope):
                await check_host(Request(scope))
                return await super().get_response(path, scope)

        app.mount("/assets", GuardedFiles(directory=DASHBOARD_DIR / "assets"))


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
    push_handler: PushHandler | None,
    home_wifi: HomeWifi | None,
    max_body: int,
    read_timeout_s: float,
    overview: Callable[[], Overview] | None = None,
    settings: Callable[[bool], Settings] | None = None,
) -> FastAPI:
    # Only phones on the Wi-Fi reach the server by IP address.
    wifi_ips = own_addresses() if home_wifi else set()
    own_names = {"localhost", "127.0.0.1", socket.gethostname().lower(), *wifi_ips}

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

    if home_wifi is None:

        @app.get("/ping")
        async def ping() -> Response:
            return PlainTextResponse("tfs")

        _add_dashboard(app, check_host, overview, settings)

    if push_handler is not None:
        _add_push(app, push_handler, max_body, read_timeout_s)
    if home_wifi is not None:
        _add_paired_dashboard(app, home_wifi, check_host, overview, settings)
    return app


def _add_push(
    app: FastAPI, push_handler: PushHandler, max_body: int, read_timeout_s: float
) -> None:
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


def _add_paired_dashboard(
    app: FastAPI,
    home_wifi: HomeWifi,
    check_host: Callable[[Request], Awaitable[None]],
    overview: Callable[[], Overview] | None,
    settings: Callable[[bool], Settings] | None,
) -> None:
    def scan() -> Response:
        return HTMLResponse(setup_page.scan_page(), 404, {"Cache-Control": "no-store"})

    @app.get("/setup/{code}")
    def setup(code: str) -> Response:
        active = home_wifi.setup
        return active[1]() if active and active[0] == code else scan()

    @app.get("/pair/{code}")
    def pair(code: str, request: Request) -> Response:
        token = pairing.redeem(home_wifi.db, code, request.headers.get("user-agent", ""))
        if token is None:
            return scan()
        waiting = home_wifi.pairing_scan
        if waiting and waiting[0] == code:
            waiting[1].set()
        response = RedirectResponse(
            "/", 303, {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        )
        response.set_cookie(
            "tfs_phone", token, max_age=365 * 86400, httponly=True, samesite="strict"
        )
        return response

    private = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(check_host)]
    )

    @private.middleware("http")
    async def require_phone(request: Request, call_next) -> Response:
        token = request.cookies.get("tfs_phone")
        if not token or not await run_in_threadpool(pairing.paired, home_wifi.db, token):
            return scan()
        response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    _add_dashboard(private, check_host, overview, settings)
    app.mount("/", private)


def _timed_protocol(request_timeout_s: float) -> type[H11Protocol]:
    # uvicorn limits neither how long headers take nor how long a whole request takes.
    class TimedProtocol(H11Protocol):
        def connection_made(self, transport: asyncio.Transport) -> None:
            super().connection_made(transport)
            self._deadline = self.loop.call_later(request_timeout_s, transport.close)

        def on_response_complete(self) -> None:
            self._deadline.cancel()
            self._deadline = self.loop.call_later(request_timeout_s, self.transport.close)
            super().on_response_complete()

        def connection_lost(self, exc: Exception | None) -> None:
            self._deadline.cancel()
            super().connection_lost(exc)

    return TimedProtocol


class WebServer(uvicorn.Server):
    """A uvicorn server that holds its port from creation until stop()."""

    def __init__(self, config: uvicorn.Config, sock: socket.socket) -> None:
        super().__init__(config)
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
    push_handler: PushHandler | None,
    home_wifi: HomeWifi | None = None,
    max_body: int = 1 << 30,
    read_timeout_s: float = 30,
    request_timeout_s: float = 15 * 60,
    max_requests: int = 20,
    shutdown_timeout_s: float = 10,
    **dashboard: Unpack[DashboardReaders],
) -> WebServer:
    """Bind the screen or paired Wi-Fi listener. The caller starts and stops it."""
    config = uvicorn.Config(
        _make_app(push_handler, home_wifi, max_body, read_timeout_s, **dashboard),
        lifespan="off",
        log_config=None,
        access_log=False,
        http=_timed_protocol(request_timeout_s),
        # uvicorn counts the arriving connection too, so this lets max_requests run.
        limit_concurrency=max_requests + 1,
        timeout_graceful_shutdown=shutdown_timeout_s,
    )
    return WebServer(config, socket.create_server((host, port)))
