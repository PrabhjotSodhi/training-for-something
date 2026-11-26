"""The temporary phone setup page on the home Wi-Fi listener."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from html import escape
from time import monotonic
from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse

if TYPE_CHECKING:
    from tfs.web.server import HomeWifi

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


@contextmanager
def show(
    wifi: "HomeWifi", code: str, page: str, on_reach: Callable[[], None]
) -> Iterator[threading.Event]:
    """Publish the page for this setup step and signal its first visit."""
    reached = threading.Event()
    lock = threading.Lock()
    expires = monotonic() + SERVER_LIFETIME_S

    def visit() -> HTMLResponse:
        with lock:
            if monotonic() >= expires:
                return HTMLResponse(scan_page(), 404, {"Cache-Control": "no-store"})
            if not reached.is_set():
                reached.set()
                on_reach()
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    wifi.setup = (code, visit)
    try:
        yield reached
    finally:
        wifi.setup = None
