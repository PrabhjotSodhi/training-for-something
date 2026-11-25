import contextlib
import io
import socket
import unittest
import urllib.error
import urllib.request
from html.parser import HTMLParser

from tfs.cli import setup
from tfs.web import setup_page

CODE = "k7q2abcd"
BUTTONS = [
    ("Add health metrics", "com.HealthExport://automation?url=http%3A%2F%2Fa&name=A%20B&x=<1>"),
    (
        "Add workouts",
        "com.HealthExport://automation?url=http%3A%2F%2Fa&name=W&headers=Bearer%20tok",
    ),
]


class Hrefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.found.append(dict(attrs)["href"])


def get(server, path):
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=5)
    except urllib.error.HTTPError as error:
        return error


class SetupServerTest(unittest.TestCase):
    def start(self, on_reach=lambda: None):
        server = setup_page.SetupServer(
            "127.0.0.1", 0, CODE, "Connect Example App", BUTTONS, on_reach
        )
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_given_the_code_when_requested_then_the_page_has_one_escaped_button_per_link(self):
        response = get(self.start(), f"/setup/{CODE}")
        body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("<h1>Connect Example App</h1>", body)
        parser = Hrefs()
        parser.feed(body)
        self.assertEqual(parser.found, [url for _, url in BUTTONS])
        self.assertIn("&amp;name=A%20B&amp;x=&lt;1&gt;", body)
        self.assertIn("Add health metrics", body)
        self.assertIn("Add workouts", body)
        self.assertIn('name="viewport"', body)

    def test_given_a_wrong_path_or_the_root_then_404_shows_no_link_or_token(self):
        server = self.start()
        for path in ("/setup/wrong", "/", f"/setup/{CODE}/more"):
            response = get(server, path)
            body = response.read().decode()
            self.assertEqual(response.status, 404)
            for text in ("HealthExport", "tok", CODE):
                self.assertNotIn(text, body)

    def test_given_the_server_stopped_then_the_address_refuses_the_connection(self):
        server = self.start()
        server.stop()
        with self.assertRaises(urllib.error.URLError) as caught:
            get(server, f"/setup/{CODE}")
        self.assertIsInstance(caught.exception.reason, ConnectionRefusedError)

    def test_given_page_requests_then_on_reach_runs_once_and_only_for_the_page(self):
        reached = []
        server = self.start(lambda: reached.append(1))
        get(server, "/").read()
        self.assertEqual(reached, [])
        get(server, f"/setup/{CODE}").read()
        get(server, f"/setup/{CODE}").read()
        self.assertEqual(reached, [1])

    def test_given_the_port_is_in_use_then_starting_raises_oserror(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            with self.assertRaises(OSError):
                setup_page.SetupServer(
                    "127.0.0.1", taken.getsockname()[1], CODE, "Connect Example App", BUTTONS, bool
                )

    def test_given_the_longest_address_then_the_qr_is_33_characters_wide_or_less(self):
        url = "http://255.255.255.255:47824/setup/" + "k7q2_abc"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.print_qr(url)
        self.assertLessEqual(max(len(line) for line in out.getvalue().splitlines()), 33)


if __name__ == "__main__":
    unittest.main()
