"""The phone setup page and the temporary server `tfs setup` runs while a phone scans its QR code."""

import sys
import threading
from collections.abc import Callable
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


class SetupServer(ThreadingHTTPServer):
    """Serves the page at /setup/<code> and answers 404 to everything else."""

    # On Windows, address reuse would let this bind next to a running TFS instead of failing.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

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
        super().__init__((bind, port), _Handler)
        self.page = render_page(title, buttons, tailscale=tailscale).encode()
        self.path = f"/setup/{code}"
        self.on_reach = on_reach
        self.reached = False
        self.port = self.server_address[1]
        self._timer = threading.Timer(lifetime_s, self.stop)
        self._timer.daemon = True
        self._stopped = False

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        self._timer.start()

    def stop(self) -> None:
        """Stop answering and release the port. Safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        self._timer.cancel()
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: SetupServer

    def do_GET(self) -> None:  # noqa: N802 (the name http.server calls)
        if self.path != self.server.path:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.server.reached:
            self.server.reached = True
            self.server.on_reach()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(self.server.page)))
        self.end_headers()
        self.wfile.write(self.server.page)

    def log_message(self, format, *args) -> None:
        pass
