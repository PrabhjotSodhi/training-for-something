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
        self.today = TODAY
        self.conn = open_database(self)
        path = Path(self.conn.execute("PRAGMA database_list").fetchone()[2])
        self.server = make_server(
            config.SCREEN_HOST,
            0,
            push_handler=lambda *_: (200, {}),
            overview=lambda: read_overview(path, self.today),
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
            {key: body["plate"][key] for key in ("goal_id", "colour", "rows")},
            {"goal_id": "near", "colour": "orange", "rows": ["resting_hr"]},
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
        self.assertEqual(body["plate"]["goal_id"], "g1")
        self.assertEqual(body["plate"]["rows"], ["readiness"])
        self.assertIsNone(body["plate"]["data"])
        self.assertEqual(body["cards"], [{"type": "trend", "metric": "hrv_sdnn", "data": None}])

    def test_no_goal_means_no_plate(self):
        # GIVEN no goals
        # WHEN the overview is requested
        # THEN the plate is null
        self.assertIsNone(self.get()["plate"])

    def test_the_plate_shows_the_forecast_on_a_scale_with_the_target_and_the_days_and_weeks(self):
        # GIVEN a goal on 2027-04-11 aiming at 180 minutes, a forecast of 175 to 185 and a plan
        # that started on 2026-11-02, with readiness and sleep for today
        self.today = date(2026, 11, 21)
        self.conn.execute(
            "INSERT INTO goals VALUES ('g1', 'marathon', '2027-04-11',"
            ' \'{"description": "Finish", "value": 180, "unit": "minutes"}\')'
        )
        self.conn.execute(
            "INSERT INTO coach_forecasts VALUES ('g1', 175, 185, 'minutes', 'm', '[]', '2026-11-20')"
        )
        self.conn.execute(
            "INSERT INTO journal (request_id, proposal_json, before_json, approved_by, saved_at)"
            " VALUES ('r1', '{}', '{}', 'chat', '2026-11-01T10:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO plan_items (id, version, plan_version, local_date, family, label, why,"
            " goal_ids_json) VALUES ('p1', 1, 1, '2026-11-02', 'run', 'Easy', 'w', '[\"g1\"]')"
        )
        self.conn.execute(
            "INSERT INTO scores VALUES ('2026-11-21', 'readiness', 72, 60, 80, '{}', 'f1')"
        )
        self.conn.execute(
            "INSERT INTO day_facts VALUES ('2026-11-21', 'sleep_asleep', 7.25, 'h', 'app', 'full', 'f1')"
        )
        layout = {
            "plate": {"goal_id": "g1", "colour": "blue", "rows": ["readiness", "sleep_asleep"]},
            "cards": [],
        }
        self.conn.execute("INSERT INTO overview_layout VALUES (1, ?)", (json.dumps(layout),))
        # WHEN the overview is requested
        data = self.get()["plate"]["data"]
        # THEN the range reads 2:55 to 3:05 with the target at the middle of the scale
        forecast = data["forecast"]
        self.assertEqual(
            (forecast["label"], forecast["low"], forecast["high"]),
            ("Coach estimate", "2:55", "3:05"),
        )
        self.assertEqual((forecast["start"], forecast["end"], forecast["target"]), (25, 75, 50))
        # AND 141 days remain in week 3 of 23
        self.assertEqual((data["days_to_go"], data["week"], data["weeks"]), (141, 3, 23))
        # AND the Today rows are exactly the two chosen, in order
        self.assertEqual(
            data["today"],
            [
                {"name": "readiness", "value": "72"},
                {"name": "sleep_asleep", "value": "7.2 h"},
            ],
        )

    def test_a_goal_with_no_forecast_and_no_plan_items_still_shows_its_days(self):
        # GIVEN a goal with no forecast, no plan items and no data today
        self.conn.execute("INSERT INTO goals VALUES ('g1', 'hyrox', '2027-04-11', '{}')")
        layout = {"plate": {"goal_id": "g1", "colour": "green", "rows": ["readiness"]}, "cards": []}
        self.conn.execute("INSERT INTO overview_layout VALUES (1, ?)", (json.dumps(layout),))
        # WHEN the overview is requested
        data = self.get()["plate"]["data"]
        # THEN there is no forecast and no week, the days remain and the row says No data
        self.assertIsNone(data["forecast"])
        self.assertEqual((data["week"], data["weeks"], data["days_to_go"]), (None, None, 138))
        self.assertEqual(data["today"], [{"name": "readiness", "value": "No data"}])
