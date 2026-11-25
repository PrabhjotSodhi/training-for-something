import http.client
import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.helpers import insert_file, insert_workout, open_database
from tfs import config, providers
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

    def fact(self, day: str, metric: str, value: float = 50) -> None:
        self.conn.execute(
            "INSERT INTO day_facts VALUES (?, ?, ?, 'x', 'app', 'full', 'f1')", (day, metric, value)
        )

    def ago(self, days: int) -> str:
        return (self.today - timedelta(days=days)).isoformat()

    def load(self, score: str, days_ago: int, value: float) -> None:
        self.conn.execute(
            "INSERT INTO scores VALUES (?, ?, ?, NULL, NULL, '{}', 'f1')",
            (self.ago(days_ago), score, value),
        )

    def trend_cards(self, *cards: dict) -> list[dict]:
        layout = {"plate": {"goal_id": "g1", "colour": "blue", "rows": []}, "cards": list(cards)}
        self.conn.execute("INSERT INTO overview_layout VALUES (1, ?)", (json.dumps(layout),))
        return self.get()["cards"]

    def workout(self, key, day, family, label, minutes, km=None, sets=0, avg_hr=None) -> None:
        insert_file(self.conn, 1, "2026-10-01T00:00:00Z")
        insert_workout(self.conn, key, "invented", start=f"{day}T07:00:00Z", family=family)
        chosen = {"family": {"value": family, "record_id": key}}
        chosen["label"] = {"value": label, "record_id": key}
        chosen["duration_s"] = {"value": minutes * 60, "record_id": key}
        if avg_hr:
            chosen["avg_hr"] = {"value": avg_hr, "record_id": key}
        if km:
            chosen["distance_m"] = {"value": km * 1000, "record_id": key}
        self.conn.execute(
            "INSERT INTO session_groups VALUES (?, ?, ?, ?, ?)",
            (key, day, family, f"{day}T07:00:00Z", json.dumps(chosen)),
        )
        self.conn.execute("INSERT INTO session_group_members VALUES (?, ?)", (key, key))
        for seq in range(sets):
            self.conn.execute(
                "INSERT INTO session_parts (record_id, seq, part_kind) VALUES (?, ?, 'set')",
                (key, seq),
            )

    def plan(self, key, day, family, label, km, matched=None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO journal (request_id, proposal_json, before_json, approved_by,"
            " saved_at) VALUES ('r1', '{}', '{}', 'chat', '2026-11-01T10:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO plan_items (id, version, plan_version, local_date, family, label, why)"
            " VALUES (?, 1, 1, ?, ?, ?, 'w')",
            (key, day, family, label),
        )
        self.conn.execute(
            "INSERT INTO session_parts (plan_item_id, seq, part_kind, distance_m)"
            " VALUES (?, 0, 'run', ?)",
            (key, km * 1000),
        )
        if matched:
            self.conn.execute(
                "INSERT INTO plan_matches VALUES (?, ?, 'date_family')", (key, matched)
            )

    def week(self) -> dict:
        return self.get()["cards"][0]["data"]

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
            [(card["type"], card["data"] is None) for card in body["cards"]],
            [("week", False), ("trend", False)],
        )

    def test_a_saved_layout_is_returned_as_saved(self):
        # GIVEN a saved layout with one hrv trend card and an hrv reading
        self.fact("2026-11-20", "hrv_sdnn")
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
        self.assertEqual(
            [(card["type"], card["metric"]) for card in body["cards"]], [("trend", "hrv_sdnn")]
        )

    def test_no_goal_means_no_plate(self):
        # GIVEN no goals
        # WHEN the overview is requested
        # THEN the plate is null
        self.assertIsNone(self.get()["plate"])

    def test_no_goal_and_a_workout_puts_the_latest_workout_in_the_first_run_panel(self):
        # GIVEN no goal and two workouts
        self.workout("w1", "2026-11-20", "run", "Easy run", 40, km=7)
        self.workout("w2", "2026-11-22", "strength", "Legs", 55, sets=6)
        # WHEN the overview is requested
        first_run = self.get()["first_run"]
        # THEN the panel holds the newest workout and the first message
        self.assertEqual(
            first_run["workout"],
            {"date": "2026-11-22", "type": "strength", "minutes": 55, "source": "invented"},
        )
        self.assertEqual(first_run["message"], providers.FIRST_MESSAGE)

    def test_no_goal_and_no_workouts_still_offers_the_first_message(self):
        # GIVEN no goal and no workouts
        # WHEN the overview is requested
        first_run = self.get()["first_run"]
        # THEN the panel holds no workout and the first message
        self.assertIsNone(first_run["workout"])
        self.assertEqual(first_run["message"], providers.FIRST_MESSAGE)

    def test_an_enabled_source_with_no_records_is_waiting_and_a_phone_source_says_so(self):
        # GIVEN a file source and a phone source with no records, and a turned off source
        self.conn.execute("INSERT INTO sources(connector) VALUES ('hevy')")
        self.conn.execute("INSERT INTO sources(connector) VALUES ('health_auto_export')")
        self.conn.execute("INSERT INTO sources(connector, enabled) VALUES ('garmin', 0)")
        # WHEN the overview is requested
        waiting = self.get()["first_run"]["waiting"]
        # THEN each enabled source with no records is named, and only the phone one is flagged
        self.assertEqual(
            waiting,
            [
                {"app": "Health Auto Export", "phone": True},
                {"app": "Hevy", "phone": False},
            ],
        )

    def test_a_goal_means_no_first_run_panel(self):
        # GIVEN a goal
        self.conn.execute("INSERT INTO goals VALUES ('g1', 'race', '2027-04-11', '{}')")
        # WHEN the overview is requested
        # THEN there is no panel
        self.assertIsNone(self.get()["first_run"])

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

    def test_a_planned_run_matched_to_a_workout_is_one_tile_counted_once(self):
        # GIVEN a planned 10 km run on Wednesday matched to a 10.4 km workout
        self.workout("w1", "2026-11-25", "run", "Morning run", 55, km=10.4)
        self.plan("p1", "2026-11-25", "run", "Easy run", 10, matched="w1")
        # WHEN the overview is requested
        week = self.week()
        # THEN Wednesday has one done tile of 10.4 km
        wednesday = week["days"][2]
        self.assertEqual(wednesday["date"], "2026-11-25")
        self.assertEqual(
            [
                (s["name"], s["amount"], s["unit"], s["state"], s["minutes"])
                for s in wednesday["sessions"]
            ],
            [("Easy run", 10.4, "km", "done", 55)],
        )
        # AND the run total is 10.4 done of 10 planned
        self.assertEqual(
            week["totals"], [{"sport": "run", "unit": "km", "done": 10.4, "planned": 10}]
        )

    def test_a_strength_workout_counts_its_sets(self):
        # GIVEN a strength workout of 18 sets on Thursday
        self.workout("w1", "2026-11-26", "strength", "Push day", 60, sets=18)
        # WHEN the overview is requested
        week = self.week()
        # THEN Thursday shows 18 sets and the other days are empty
        session = week["days"][3]["sessions"][0]
        self.assertEqual((session["amount"], session["unit"]), (18, "sets"))
        self.assertEqual([len(day["sessions"]) for day in week["days"]], [0, 0, 0, 1, 0, 0, 0])

    def test_a_run_with_no_plan_item_is_its_own_done_tile_and_other_weeks_stay_out(self):
        # GIVEN a 12 km run on Tuesday with no plan item, and a run from last week
        self.workout("w1", "2026-11-24", "run", "Long run", 70, km=12)
        self.workout("w2", "2026-11-22", "run", "Last week", 30, km=5)
        # WHEN the overview is requested
        week = self.week()
        # THEN Tuesday has a done 12 km tile and the totals hold only that run
        self.assertEqual(week["days"][1]["sessions"][0]["state"], "done")
        self.assertEqual(week["totals"], [{"sport": "run", "unit": "km", "done": 12, "planned": 0}])

    def test_the_sleep_card_holds_seven_nights_with_a_gap_and_the_saved_target(self):
        # GIVEN six nights averaging 7 h 35 min, a gap on the 21st, and older sleep outside the week
        nights = (("11-18", 7), ("11-19", 8), ("11-20", 7.5), ("11-22", 7.75), ("11-23", 7))
        for day, hours in (*nights, ("11-24", 8.25), ("11-17", 3)):
            self.conn.execute(
                "INSERT INTO day_facts VALUES (?, 'sleep_asleep', ?, 'h', 'app', 'full', 'f1')",
                (f"2026-{day}", hours),
            )
        layout = {
            "plate": {"goal_id": "g1", "colour": "blue", "rows": []},
            "cards": [{"type": "sleep", "target_h": 8}, {"type": "sleep"}],
        }
        self.conn.execute("INSERT INTO overview_layout VALUES (1, ?)", (json.dumps(layout),))
        # WHEN the overview is requested
        with_target, without = (card["data"] for card in self.get()["cards"])
        # THEN the nights run from the 18th to today, the 21st is null and the target passes through
        hours = [night["hours"] for night in with_target["nights"]]
        self.assertEqual(hours, [7, 8, 7.5, None, 7.75, 7, 8.25])
        self.assertEqual(with_target["nights"][0]["date"], "2026-11-18")
        self.assertEqual(with_target["target_h"], 8)
        self.assertIsNone(without["target_h"])

    def test_the_sets_by_muscle_card_is_empty_in_a_week_with_no_hevy_sets(self):
        # GIVEN a saved layout with the card and no Hevy workouts
        layout = {
            "plate": {"goal_id": "g1", "colour": "blue", "rows": []},
            "cards": [{"type": "sets_by_muscle"}],
        }
        self.conn.execute("INSERT INTO overview_layout VALUES (1, ?)", (json.dumps(layout),))
        # WHEN the overview is requested
        card = self.get()["cards"][0]
        # THEN the card holds no muscles, not a zero for each
        self.assertEqual(card["data"], {"muscles": []})

    def test_resting_heart_rate_shows_today_against_the_median_and_spread_of_the_42_days_before(
        self,
    ):
        # GIVEN 42 earlier days of 47, 49 and 51 (median 49, spread 3) and 49 today
        for back in range(1, 43):
            self.fact(self.ago(back), "resting_hr", (47, 49, 51)[back % 3])
        self.fact(self.ago(0), "resting_hr", 49)
        # WHEN the overview is requested
        data = self.trend_cards({"type": "trend", "metric": "resting_hr"})[0]["data"]
        # THEN 30 days end today at 49 with band edges 46 and 52, and the card takes the plate colour
        points = data["parts"][0]["points"]
        self.assertEqual(len(points), 30)
        self.assertEqual(points[-1], {"date": self.ago(0), "value": 49, "low": 46, "high": 52})
        self.assertEqual(data["colour"], "blue")

    def test_a_day_with_no_reading_is_null(self):
        # GIVEN readings today and the day before yesterday
        self.fact(self.ago(0), "resting_hr")
        self.fact(self.ago(2), "resting_hr")
        # WHEN the overview is requested
        data = self.trend_cards({"type": "trend", "metric": "resting_hr"})[0]["data"]
        # THEN yesterday is null between two readings
        self.assertEqual([p["value"] for p in data["parts"][0]["points"][-3:]], [50, None, 50])

    def test_cardio_load_of_679_against_2340_over_the_previous_28_days_is_above_normal(self):
        # GIVEN 679 of cardio load in the last 7 days, 2340 in the 28 before, first load 30 days ago
        self.load("load_cardio", 30, 2340)
        self.load("load_cardio", 0, 679)
        # WHEN the overview is requested
        data = self.trend_cards({"type": "trend", "metric": "training_load"})[0]["data"]
        # THEN it is Above normal at +16% and carries no load value
        part = data["parts"][0]
        self.assertEqual(
            (part["state"], part["percent"], part["value"]), ("Above normal", 16, None)
        )

    def test_strength_data_only_has_one_part_and_no_chips(self):
        # GIVEN strength load with its first day 30 days ago
        self.load("load_strength", 30, 400)
        self.load("load_strength", 0, 100)
        # WHEN the overview is requested
        data = self.trend_cards({"type": "trend", "metric": "training_load", "first": "effort"})[0][
            "data"
        ]
        # THEN only strength shows, selected although effort was asked for
        self.assertEqual(
            ([part["key"] for part in data["parts"]], data["selected"]), (["strength"], "strength")
        )

    def test_a_first_workout_10_days_ago_drops_training_load_out(self):
        # GIVEN cardio load that started 10 days ago
        self.load("load_cardio", 10, 300)
        self.load("load_cardio", 0, 300)
        # WHEN the overview is requested
        # THEN there is no card
        self.assertEqual(self.trend_cards({"type": "trend", "metric": "training_load"}), [])

    def test_all_three_parts_show_in_order_and_the_layout_picks_the_first(self):
        # GIVEN cardio, strength and effort load that started 30 days ago
        for score in ("load_effort", "load_strength", "load_cardio"):
            self.load(score, 30, 400)
            self.load(score, 0, 100)
        # WHEN the overview is requested with and without a first part
        plain, picked = (
            card["data"]
            for card in self.trend_cards(
                {"type": "trend", "metric": "training_load"},
                {"type": "trend", "metric": "training_load", "first": "strength"},
            )
        )
        # THEN the chips run Cardio, Strength, Effort and the card opens on the one asked for
        self.assertEqual(
            [part["name"] for part in plain["parts"]], ["Cardio", "Strength", "Effort"]
        )
        self.assertEqual((plain["selected"], picked["selected"]), ("cardio", "strength"))

    def test_cardio_needs_heart_rate_on_three_in_four_workouts(self):
        # GIVEN cardio load with one workout with heart rate and one without
        self.load("load_cardio", 30, 400)
        self.load("load_cardio", 0, 100)
        self.workout("w1", self.ago(3), "run", "Run", 30, avg_hr=140)
        self.workout("w2", self.ago(2), "run", "Run", 30)
        # WHEN the overview is requested
        # THEN cardio does not show
        self.assertEqual(self.trend_cards({"type": "trend", "metric": "training_load"}), [])
