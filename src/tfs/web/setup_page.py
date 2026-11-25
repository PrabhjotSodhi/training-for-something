"""The phone setup page and the temporary server `tfs setup` runs while a phone scans its QR code."""

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape

from fastapi.responses import HTMLResponse

from tfs import config
from tfs.web import server
from tfs.web.overview import read_overview

SERVER_LIFETIME_S = 600

# Colours and font stack come from dashboard/src/theme.css.
_STYLE = """
:root{--page:#f5f5f7;--card:#fff;--ink:#1c1c1e;--muted:#6b6b70;--line:#e5e5ea;color-scheme:light dark}
@media(prefers-color-scheme:dark){:root{--page:#0a0a0b;--card:#1c1c1e;--ink:#f5f5f7;--muted:#98989f;--line:#2c2c2e}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--page);color:var(--ink);
font:400 16px/1.4 "Plus Jakarta Sans",system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:-.005em}
main{max-width:420px;margin:0 auto;padding:48px 20px 32px}
.brand{margin:0 0 28px;color:var(--muted);font-size:13px}
h1{margin:0 0 24px;font-size:28px;line-height:1.1;font-weight:700}
.card{padding:16px;background:var(--card);border:1px solid var(--line);border-radius:20px}
a.button{display:block;margin:0 0 12px;padding:18px 16px;border-radius:14px;background:var(--ink);color:var(--card);
font-weight:700;font-size:17px;text-align:center;text-decoration:none}
a.button:last-of-type{margin-bottom:0}
a.button:active{opacity:.8}
.hint{margin:20px 4px 0;color:var(--muted);font-size:15px}
"""


def render_page(title: str, buttons: list[tuple[str, str]], *, tailscale: bool = False) -> str:
    """Return the setup page with one full-width button per (label, link). Values are escaped."""
    links = "\n".join(
        f'<a class="button" href="{escape(url)}">{escape(label)}</a>' for label, url in buttons
    )
    hint = "Tap each button, then tap Enable in the app."
    if tailscale:
        hint += (
            " Tailscale automations send only while the Tailscale app is connected on your phone."
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>{escape(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<p class="brand">Training For Something</p>
<h1>{escape(title)}</h1>
<div class="card">
{links}
</div>
<p class="hint">{hint}</p>
</main>
</body>
</html>
"""


def scan_page() -> str:
    """Return the public instruction shown to unpaired phones."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Pair your phone</title><style>{_STYLE}</style></head>
<body><main><p class="card">Scan the QR code from <code>tfs setup</code> on your computer.</p></main></body></html>"""


class SetupServer:
    """Serve setup links and the paired dashboard until setup ends."""

    def __init__(
        self,
        bind: str,
        port: int,
        code: str,
        title: str,
        buttons: list[tuple[str, str]],
        on_reach: Callable[[], None],
        lifetime_s: float = SERVER_LIFETIME_S,
        *,
        tailscale: bool = False,
    ) -> None:
        page = render_page(title, buttons, tailscale=tailscale)
        reached = False
        lock = threading.Lock()

        def show_setup() -> HTMLResponse:
            nonlocal reached
            with lock:
                if not reached:
                    reached = True
                    on_reach()
            return HTMLResponse(page, headers={"Cache-Control": "no-store"})

        db = config.db_path()
        self._server = server.make_server(
            bind,
            port,
            push_handler=None,
            home_wifi=server.HomeWifi(db, (f"/setup/{code}", show_setup)),
            overview=lambda: read_overview(db, datetime.now(UTC).astimezone()),
        )
        self.port = self._server.sock.getsockname()[1]
        self._timer = threading.Timer(lifetime_s, self.stop)
        self._timer.daemon = True
        self._stopped = False

    def start(self) -> None:
        """Start serving and set the setup deadline."""
        self._server.start()
        self._timer.start()

    def stop(self) -> None:
        """Stop answering and release the port. Safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        self._timer.cancel()
        self._server.stop()
