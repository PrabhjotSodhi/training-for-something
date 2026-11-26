import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import insert_file, insert_workout, open_database
from tests.test_setup import FakeCommands, FakeQuestionary, run_without_engine
from tfs import config
from tfs.cli import main
from tfs.connectors.core import runner
from tfs.connectors.garmin import connector
from tfs.duplicates.workouts import update
from tfs.persistence.sqlite import connect

API_KEY = "invented-key-123"
NOW = "2026-11-20T12:00:00Z"
ACTIVITY = {
    "activityId": 9001,
    "activityName": "Morning Run",
    "startTimeGMT": "2026-11-20 06:00:00",
    "startTimeLocal": "2026-11-20 19:00:00",
    "duration": 1800.0,
    "distance": 5000.0,
    "calories": 300.0,
    "averageHR": 150.0,
    "maxHR": 171.0,
    "activityType": {"typeKey": "running"},
}
# One invented reply per data endpoint, keyed by a piece of its path.
REPLIES = {
    "activities/search": [ACTIVITY],
    "dailySleepSummariesByDate": [
        {
            "calendarDate": "2026-11-20",
            "sleepTimeSeconds": 27000,
            "lightSleepSeconds": 16200,
            "deepSleepSeconds": 3600,
            "remSleepSeconds": 7200,
            "awakeSleepSeconds": 900,
            "sleepStartTimestampGMT": 1795125600000,
            "sleepEndTimestampGMT": 1795152600000,
            "sleepStartTimestampLocal": 1795172400000,
        }
    ],
    "hrv/daily": {"hrvSummaries": [{"calendarDate": "2026-11-20", "lastNightAvg": 62}]},
    "stats/steps/daily": [{"calendarDate": "2026-11-20", "totalSteps": 8200}],
    "v2/dailysummaries": {
        "data": [{"calendarDate": "2026-11-20", "heartRate": {"restingValue": 51}}]
    },
    "weight/range": {
        "dailyWeightSummaries": [
            {
                "summaryDate": "2026-11-20",
                "latestWeight": {
                    "calendarDate": "2026-11-20",
                    "weight": 71.5,
                    "timestampGMT": 1795125600000,
                },
            }
        ]
    },
    "maxmet/daily": [{"generic": {"calendarDate": "2026-11-20", "vo2MaxValue": 52.0}}],
}


class FakeGarmin:
    """Stands in for the Garmin Personal API and for the connector child process."""

    def __init__(self, *, key_valid: bool = True, data_status: int = 200, headers=None) -> None:
        self.key_valid = key_valid
        self.data_status = data_status
        self.headers = headers or {}
        self.exchanges = 0
        self.bearers: list[str] = []

    def http(self, url, headers, timeout_s=30, method="GET", body=None):
        if url.endswith("/personal-api-key/token"):
            assert method == "POST" and headers["Authorization"] == f"ApiKey {API_KEY}"
            if not self.key_valid:
                return 401, {}, b""
            self.exchanges += 1
            return 200, {}, f"token-{self.exchanges}".encode()
        assert method == "GET"
        self.bearers.append(headers["Authorization"])
        if self.data_status != 200:
            return self.data_status, self.headers, b""
        reply = next(data for path, data in REPLIES.items() if path in url)
        return 200, {}, json.dumps(reply).encode("utf-8")

    def run(self, name: str, op: str, payload: dict, **options) -> dict:
        extra = {"http": self.http} if op == "fetch" else {}
        return getattr(connector, op)(**payload, **extra)


class GarminFetchTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out_dir = folder.name

    def fetch(self, garmin: FakeGarmin, cursor: dict | None = None) -> dict:
        return connector.fetch(
            cursor=cursor,
            secrets={"api_key": API_KEY},
            out_dir=self.out_dir,
            http=garmin.http,
            now=NOW,
        )

    def test_given_two_fetches_then_each_exchanges_the_key_and_no_token_is_stored(self):
        # GIVEN a Garmin that accepts the key
        garmin = FakeGarmin()
        # WHEN TFS fetches twice
        first = self.fetch(garmin)
        second = self.fetch(garmin, first["cursor"])
        # THEN each fetch used its own fresh token and neither result holds a token
        self.assertEqual(garmin.exchanges, 2)
        self.assertEqual(set(garmin.bearers), {"Bearer token-1", "Bearer token-2"})
        self.assertNotIn("token-", json.dumps([first, second]))

    def test_given_an_expired_key_then_auth_failed_and_the_cursor_stays(self):
        cursor = {"since": "2026-11-19T12:00:00Z"}
        for garmin in (FakeGarmin(key_valid=False), FakeGarmin(data_status=401)):
            with self.subTest(token_endpoint_rejects=not garmin.key_valid):
                # GIVEN the token endpoint or a data endpoint answers 401
                # WHEN TFS fetches
                result = self.fetch(garmin, cursor)
                # THEN the fetch is auth_failed with the same cursor
                self.assertTrue(result["auth_failed"])
                self.assertEqual(result["files"], [])
                self.assertEqual(result["cursor"], cursor)

    def test_given_a_429_then_it_waits_as_the_reply_says_and_keeps_the_cursor(self):
        # GIVEN a Garmin that answers 429 with Retry-After
        cursor = {"since": "2026-11-19T12:00:00Z"}
        garmin = FakeGarmin(data_status=429, headers={"Retry-After": "120"})
        # WHEN TFS fetches
        result = self.fetch(garmin, cursor)
        # THEN it waits 120 seconds and the cursor stays
        self.assertEqual(result["wait_until"], "2026-11-20T12:02:00Z")
        self.assertEqual(result["cursor"], cursor)
        self.assertFalse(result["auth_failed"])


class GarminSetupTest(unittest.TestCase):
    def run_setup(self, garmin: FakeGarmin) -> Path:
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"TFS_HOME": home.name}),
            mock.patch.object(runner, "run", garmin.run),
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)),
            mock.patch("tfs.cli.setup.questionary", FakeQuestionary(["garmin"], API_KEY)),
            mock.patch("tfs.providers.shutil.which", FakeCommands(found=set()).which),
            mock.patch("pathlib.Path.home", return_value=Path(home.name)),
            mock.patch("tfs.cli.setup.webbrowser.open"),
            mock.patch("tfs.cli.setup.start.run", side_effect=run_without_engine),
            contextlib.redirect_stdout(out),
        ):
            main(["setup"])
            return config.db_path()

    def test_empty_account_stays_enabled(self):
        # GIVEN every data endpoint returns an empty result list
        empty = {
            path: {key: [] for key in reply} if isinstance(reply, dict) else []
            for path, reply in REPLIES.items()
        }
        # WHEN setup connects Garmin
        with mock.patch.dict(REPLIES, empty):
            database = self.run_setup(FakeGarmin())
        # THEN the source stays enabled and stores no empty pages
        with contextlib.closing(connect(database)) as conn:
            enabled = conn.execute("SELECT enabled FROM sources WHERE connector = 'garmin'")
            self.assertEqual(enabled.fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_given_a_valid_key_then_setup_fetches_a_token_and_stores_the_data(self):
        # GIVEN a Garmin that accepts the key
        garmin = FakeGarmin()
        # WHEN setup connects Garmin
        database = self.run_setup(garmin)
        # THEN light sleep counts as core and each stage has its own stored record
        with contextlib.closing(connect(database)) as conn:
            stored = conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
            sleep = dict(
                conn.execute(
                    "SELECT metric, json_extract(values_json, '$.value') FROM source_records WHERE stream='sleep'"
                )
            )
        self.assertEqual(
            sleep,
            {
                "sleep_asleep": 7.5,
                "sleep_core": 4.5,
                "sleep_deep": 1,
                "sleep_rem": 2,
                "sleep_awake": 0.25,
            },
        )
        self.assertEqual(garmin.exchanges, 1)
        self.assertEqual(stored, len(REPLIES) + 4)


class GarminReadTest(unittest.TestCase):
    def test_given_a_page_with_one_good_day_and_one_empty_day_then_one_record_is_read(self):
        # GIVEN a steps page where Garmin left one day's value out
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        page = Path(folder.name) / "steps.json"
        days = [
            {"calendarDate": "2026-11-19", "totalSteps": 8200},
            {"calendarDate": "2026-11-20", "totalSteps": None},
        ]
        page.write_text(json.dumps(days), encoding="utf-8")
        out_path = Path(folder.name) / "records.jsonl"
        # WHEN the connector reads it
        result = connector.read(str(page), "steps", str(out_path))
        # THEN the good day is read and the empty day is skipped
        self.assertEqual((result["status"], result["count"]), ("ok", 1))


class GarminDuplicateTest(unittest.TestCase):
    def test_given_the_same_run_in_garmin_and_apple_health_then_tfs_counts_it_once(self):
        # GIVEN a Garmin run read by the connector, and the same run from Apple Health
        conn = open_database(self)
        insert_file(conn, 1, "2026-11-20T12:00:00Z")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        page = Path(folder.name) / "activities.json"
        page.write_text(json.dumps([ACTIVITY]), encoding="utf-8")
        out_path = page.with_name("records.jsonl")
        connector.read(str(page), "activities", str(out_path))
        run = json.loads(out_path.read_text(encoding="utf-8"))
        insert_workout(
            conn,
            "garmin-1",
            "garmin",
            run["start_utc"],
            run["end_utc"],
            run["values"],
            family=run["family"],
        )
        insert_workout(
            conn,
            "apple-1",
            "apple_health",
            "2026-11-20T06:00:20Z",
            "2026-11-20T06:30:20Z",
            family=run["family"],
        )
        # WHEN both import
        groups = update(conn, {"2026-11-20"})
        # THEN the two sessions are one group
        self.assertEqual(groups, 1)
