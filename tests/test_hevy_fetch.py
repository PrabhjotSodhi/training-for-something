import json
import tempfile
import unittest
from pathlib import Path

from tfs.connectors.hevy.connector import connect, fetch

NOW = "2026-10-27T07:00:00Z"
API_KEY = "invented-key-123"
BASE = "https://api.hevyapp.com/v1"
ROUTINES_URL = f"{BASE}/routines?page=1&pageSize=10"


class FakeHttp:
    """Answers each URL from `replies` and records every URL and header set."""

    def __init__(self, replies: dict[str, tuple[int, dict, bytes]]) -> None:
        self.replies = replies
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url: str, headers: dict, timeout_s: float = 30) -> tuple[int, dict, bytes]:
        self.urls.append(url)
        self.headers.append(headers)
        return self.replies[url]


def _page(page: int, page_count: int, key: str = "workouts") -> tuple[int, dict, bytes]:
    body = {"page": page, "page_count": page_count, key: []}
    return 200, {}, json.dumps(body).encode("utf-8")


class HevyFetchTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out_dir = Path(folder.name)

    def _fetch(self, cursor: dict | None, http: FakeHttp) -> dict:
        return fetch(
            cursor=cursor,
            secrets={"api_key": API_KEY},
            out_dir=str(self.out_dir),
            http=http,
            now=NOW,
        )

    def test_first_fetch_pages_through_all_workouts(self) -> None:
        http = FakeHttp(
            {
                f"{BASE}/workouts?page=1&pageSize=10": _page(1, 2),
                f"{BASE}/workouts?page=2&pageSize=10": _page(2, 2),
                ROUTINES_URL: _page(1, 1, key="routines"),
            }
        )

        result = self._fetch(None, http)

        self.assertEqual(
            http.urls,
            [
                f"{BASE}/workouts?page=1&pageSize=10",
                f"{BASE}/workouts?page=2&pageSize=10",
                ROUTINES_URL,
            ],
        )
        self.assertEqual(http.headers, [{"api-key": API_KEY, "accept": "application/json"}] * 3)
        self.assertEqual(
            result,
            {
                "files": [
                    {
                        "path": str(self.out_dir / "workouts_page-1.json"),
                        "fmt": "workouts_page",
                        "ext": "json",
                    },
                    {
                        "path": str(self.out_dir / "workouts_page-2.json"),
                        "fmt": "workouts_page",
                        "ext": "json",
                    },
                ],
                "cursor": {"since": "2026-10-27T06:55:00Z"},
                "auth_failed": False,
                "wait_until": None,
                "targets": [],
            },
        )
        self.assertEqual(json.loads((self.out_dir / "workouts_page-2.json").read_text())["page"], 2)

    def test_later_fetch_asks_for_events_since_the_cursor(self) -> None:
        url = f"{BASE}/workouts/events?page=1&pageSize=10&since=2026-10-26T07%3A00%3A00Z"
        http = FakeHttp({url: _page(1, 1, key="events"), ROUTINES_URL: _page(1, 1, key="routines")})

        result = self._fetch({"since": "2026-10-26T07:00:00Z"}, http)

        self.assertEqual(http.urls, [url, ROUTINES_URL])
        self.assertEqual(
            result["files"],
            [
                {
                    "path": str(self.out_dir / "events_page-1.json"),
                    "fmt": "events_page",
                    "ext": "json",
                }
            ],
        )
        self.assertEqual(result["cursor"], {"since": "2026-10-27T06:55:00Z"})

    def test_bad_key_gives_auth_failed_and_keeps_the_cursor(self) -> None:
        http = FakeHttp({f"{BASE}/workouts?page=1&pageSize=10": (401, {}, b"")})

        result = self._fetch(None, http)

        self.assertTrue(result["auth_failed"])
        self.assertEqual(result["files"], [])
        self.assertIsNone(result["cursor"])
        self.assertEqual(list(self.out_dir.iterdir()), [])

    def test_rate_limit_waits_for_retry_after(self) -> None:
        cursor = {"since": "2026-10-26T07:00:00Z"}
        url = f"{BASE}/workouts/events?page=1&pageSize=10&since=2026-10-26T07%3A00%3A00Z"
        http = FakeHttp({url: (429, {"Retry-After": "120"}, b"")})

        result = self._fetch(cursor, http)

        self.assertEqual(result["files"], [])
        self.assertEqual(result["cursor"], cursor)
        self.assertEqual(result["wait_until"], "2026-10-27T07:02:00Z")
        self.assertFalse(result["auth_failed"])

    def test_rate_limit_without_a_usable_header_waits_5_minutes(self) -> None:
        url = f"{BASE}/workouts?page=1&pageSize=10"
        for headers in ({}, {"Retry-After": "Tue, 27 Oct 2026 07:02:00 GMT"}):
            with self.subTest(headers=headers):
                result = self._fetch(None, FakeHttp({url: (429, headers, b"")}))

                self.assertEqual(result["wait_until"], "2026-10-27T07:05:00Z")

    def test_server_error_on_page_2_raises_without_the_key(self) -> None:
        http = FakeHttp(
            {
                f"{BASE}/workouts?page=1&pageSize=10": _page(1, 2),
                f"{BASE}/workouts?page=2&pageSize=10": (500, {}, f"oops {API_KEY}".encode()),
            }
        )

        with self.assertRaises(ValueError) as caught:
            self._fetch(None, http)

        self.assertIn("page 2", str(caught.exception))
        self.assertNotIn(API_KEY, str(caught.exception))


class HevyConnectTest(unittest.TestCase):
    def test_connect_returns_the_key_as_a_secret(self) -> None:
        self.assertEqual(connect({"api_key": "abc"}), {"secrets": {"api_key": "abc"}})

    def test_connect_rejects_an_empty_key(self) -> None:
        with self.assertRaises(ValueError) as caught:
            connect({"api_key": ""})

        self.assertEqual(str(caught.exception), "Enter your Hevy API key.")
