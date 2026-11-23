import http.client
import json
import unittest
from datetime import date
from pathlib import Path

from tests.helpers import open_database
from tfs import config
from tfs.web.overview import read_overview
from tfs.web.server import make_server

TODAY = date(2026, 11, 24)


class OverviewApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(self)
        path = Path(self.conn.execute("PRAGMA database_list").fetchone()[2])
        self.server = make_server(
            config.SCREEN_HOST,
            0,
            push_handler=lambda *_: (200, {}),
            overview=lambda: read_overview(path, TODAY),
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    def fact(self, day: str, metric: str) -> None:
        self.conn.execute(
            "INSERT INTO day_facts VALUES (?, ?, 50, 'x', 'app', 'full', 'f1')", (day, metric)
        )

    def get(self) -> dict:
        host, port = self.server.sock.getsockname()
        client = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(client.close)
        client.request("GET", "/api/overview")
        return json.loads(client.getresponse().read())

    def test_the_default_layout_has_the_nearest_goal_and_only_cards_with_recent_data(self):
        # GIVEN two goals, resting heart rate this week and hrv from before the 28 day window
        for goal, day in (("far", "2027-09-01"), ("near", "2027-04-11"), ("past", "2026-01-01")):
            self.conn.execute("INSERT INTO goals VALUES (?, 'race', ?, '{}')", (goal, day))
        self.fact("2026-11-20", "resting_hr")
        self.fact("2026-09-01", "hrv_sdnn")
        # WHEN the overview is requested
        body = self.get()
        # THEN the plate is the nearest future goal with the rows that have data
        self.assertEqual(
            body["plate"], {"goal_id": "near", "colour": "orange", "rows": ["resting_hr"]}
        )
        # AND only week and resting heart rate remain, each with null data
        self.assertEqual(
            body["cards"],
            [
                {"type": "week", "data": None},
                {"type": "trend", "metric": "resting_hr", "data": None},
            ],
        )

    def test_a_saved_layout_is_returned_as_saved(self):
        # GIVEN a saved layout with one hrv trend card
        layout = {
            "plate": {"goal_id": "g1", "colour": "blue", "rows": ["readiness"]},
            "cards": [{"type": "trend", "metric": "hrv_sdnn"}],
        }
        self.conn.execute("INSERT INTO overview_layout VALUES (1, ?)", (json.dumps(layout),))
        # WHEN the overview is requested
        body = self.get()
        # THEN it holds that plate and that card only
        self.assertEqual(body["plate"], layout["plate"])
        self.assertEqual(body["cards"], [{"type": "trend", "metric": "hrv_sdnn", "data": None}])

    def test_no_goal_means_no_plate(self):
        # GIVEN no goals
        # WHEN the overview is requested
        # THEN the plate is null
        self.assertIsNone(self.get()["plate"])
