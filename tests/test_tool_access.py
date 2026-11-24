import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from tfs.mcp.tool_access import VIEWS, describe, open_reader, query
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.proposals.apply import save


class ToolAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "tfs.db"
        writer = connect(path)
        migrate(writer)
        writer.executemany(
            "INSERT INTO day_facts (local_date, metric, value, unit, source, coverage,"
            " formula_version) VALUES (?, 'steps', 1000, 'count', 'apple_health', 'full', 'v1')",
            [((date(2025, 1, 1) + timedelta(days=n)).isoformat(),) for n in range(600)],
        )
        writer.executemany(
            "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
            " received_at) VALUES (?, ?, ?, 'json', 'json', 1, '2025-03-03T00:00:00Z')",
            [("f1", 1, "strava"), ("f2", 2, "garmin"), ("f3", 3, "hevy"), ("f4", 4, "strongapp")],
        )
        writer.executemany(
            "INSERT INTO sources (connector, enabled, last_received_at, last_error)"
            " VALUES (?, ?, ?, ?)",
            [
                ("strava", 1, "2025-03-03T08:00:00Z", "Token expired"),
                ("garmin", 1, "2025-03-03T09:00:00Z", None),
                ("hevy", 1, None, None),
                ("polar", 0, None, None),
            ],
        )
        writer.executemany(
            "INSERT INTO source_records (id, connector, stream, key, kind, revision_at,"
            " arrival_seq, file_sha256, reader_version, start_utc, end_utc, utc_offset_min,"
            " local_date, label, family, values_json) VALUES (?, ?, 's', ?, 'session',"
            " '2025-03-03T00:00:00Z', 1, ?, '1', ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "a",
                    "strava",
                    "a",
                    "f1",
                    "2025-03-01T07:00:00Z",
                    "2025-03-01T07:50:00Z",
                    60,
                    "2025-03-01",
                    "Morning run",
                    "run",
                    '{"duration_s": 3000, "distance_m": 12000}',
                ),
                (
                    "b",
                    "garmin",
                    "b",
                    "f2",
                    "2025-03-01T07:00:30Z",
                    "2025-03-01T07:51:20Z",
                    60,
                    "2025-03-01",
                    "Run",
                    "run",
                    '{"duration_s": 3050, "distance_m": 10236}',
                ),
                (
                    "c",
                    "hevy",
                    "c",
                    "f3",
                    "2025-03-02T17:00:00Z",
                    "2025-03-02T18:00:00Z",
                    -300,
                    "2025-03-02",
                    "Push day",
                    "strength",
                    '{"duration_s": 3600}',
                ),
                (
                    "d",
                    "strongapp",
                    "d",
                    "f4",
                    "2025-03-02T17:00:10Z",
                    "2025-03-02T18:00:10Z",
                    -300,
                    "2025-03-02",
                    "Push",
                    "strength",
                    '{"duration_s": 3600}',
                ),
            ],
        )
        writer.executemany(
            "INSERT INTO session_parts (record_id, seq, part_kind, label, duration_s, distance_m,"
            " reps, weight_kg, avg_hr) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("a", 1, "lap", "Lap 1", 1500, 6000, None, None, 150),
                ("a", 2, "lap", "Lap 2", 1500, 6000, None, None, 152),
                ("b", 1, "lap", "Km 1-3", 900, 3000.5, None, None, 147),
                ("b", 2, "lap", "Km 4-6", 900, 3000, None, None, 148),
                ("b", 3, "lap", "Km 7-10", 1250, 4235.5, None, None, 149),
                ("c", 1, "set", "Bench press", None, None, 8, 60, None),
                ("c", 2, "set", "Bench press", None, None, 8, 62.5, None),
                ("c", 3, "set", "Row", None, None, 10, 50, None),
                ("d", 1, "set", "Bench", None, None, 5, 40, None),
                ("d", 2, "set", "Bench", None, None, 5, 40, None),
                ("d", 3, "set", "Row", None, None, 5, 40, None),
            ],
        )
        chosen_run = {
            "label": {"value": "Run", "record_id": "b"},
            "family": {"value": "run", "record_id": "b"},
            "duration_s": {"value": 3050, "record_id": "b"},
            "distance_m": {"value": 10236, "record_id": "b"},
            "active_energy_kcal": {"value": 612, "record_id": "a"},
            "avg_hr": {"value": 148.5, "record_id": "b"},
            "max_hr": {"value": 171, "record_id": "a"},
        }
        chosen_strength = {
            "label": {"value": "Push day", "record_id": "c"},
            "family": {"value": "strength", "record_id": "c"},
            "duration_s": {"value": 3600, "record_id": "c"},
        }
        writer.executemany(
            "INSERT INTO session_groups (id, local_date, family, start_utc, chosen_json)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                ("a", "2025-03-01", "run", "2025-03-01T07:00:00Z", json.dumps(chosen_run)),
                (
                    "c",
                    "2025-03-02",
                    "strength",
                    "2025-03-02T17:00:00Z",
                    json.dumps(chosen_strength),
                ),
            ],
        )
        writer.executemany(
            "INSERT INTO session_group_members (group_id, record_id) VALUES (?, ?)",
            [("a", "a"), ("a", "b"), ("c", "c"), ("c", "d")],
        )
        writer.close()
        self.path = path
        self.reader = open_reader(path)
        self.addCleanup(self.reader.close)

    def test_describe_lists_each_view_with_its_columns(self) -> None:
        result = describe(self.reader)
        self.assertEqual(list(result), list(VIEWS))
        self.assertEqual(
            result["coach_v1_day_facts"]["columns"],
            ["local_date", "metric", "value", "unit", "source", "coverage"],
        )
        self.assertIn("importing", result["coach_v1_day_facts"]["meaning"])

    def test_describe_lists_the_coach_views(self) -> None:
        result = describe(self.reader)
        self.assertEqual(
            list(result),
            [
                "coach_v1_deliveries",
                "coach_v1_day_facts",
                "coach_v1_scores",
                "coach_v1_workouts",
                "coach_v1_workout_parts",
                "coach_v1_sources",
                "coach_v1_plan_items",
                "coach_v1_plan_parts",
                "coach_v1_plan_state",
                "coach_v1_goals",
                "coach_v1_forecasts",
                "coach_v1_overview",
                "coach_v1_limits",
                "coach_v1_app_targets",
            ],
        )
        meaning = result["coach_v1_workouts"]["meaning"]
        self.assertIn("start_utc is UTC and utc_offset_min gives local time", meaning)

    def test_workouts_have_one_row_per_session_group(self) -> None:
        result = query(self.reader, "SELECT * FROM coach_v1_workouts ORDER BY workout_id")
        self.assertEqual(
            result["columns"],
            [
                "workout_id",
                "local_date",
                "start_utc",
                "utc_offset_min",
                "family",
                "label",
                "duration_min",
                "distance_km",
                "active_energy_kcal",
                "avg_hr",
                "max_hr",
                "source",
                "active_energy_source",
                "avg_hr_source",
                "max_hr_source",
                "sources",
                "conflict",
            ],
        )
        self.assertEqual(
            result["rows"][1],
            [
                "c",
                "2025-03-02",
                "2025-03-02T17:00:00Z",
                -300,
                "strength",
                "Push day",
                60.0,
                None,
                None,
                None,
                None,
                "hevy",
                None,
                None,
                None,
                "hevy, strongapp",
                0,
            ],
        )
        self.assertEqual(len(result["rows"]), 2)

    def test_run_from_two_apps_is_one_row(self) -> None:
        result = query(self.reader, "SELECT * FROM coach_v1_workouts WHERE family = 'run'")
        self.assertEqual(
            result["rows"],
            [
                [
                    "a",
                    "2025-03-01",
                    "2025-03-01T07:00:00Z",
                    60,
                    "run",
                    "Run",
                    50.8,
                    10.24,
                    612,
                    148.5,
                    171,
                    "garmin",
                    "strava",
                    "garmin",
                    "strava",
                    "garmin, strava",
                    1,
                ]
            ],
        )

    def test_conflict_needs_more_than_ten_percent_and_a_chosen_value(self) -> None:
        writer = connect(self.path)
        writer.execute(
            "UPDATE source_records SET values_json = ? WHERE id = 'a'",
            ('{"duration_s": 3000, "distance_m": 9500}',),
        )
        writer.execute("UPDATE source_records SET values_json = '{}' WHERE id = 'd'")
        writer.execute(
            "UPDATE session_groups SET chosen_json = json_remove(chosen_json, '$.duration_s')"
            " WHERE id = 'c'"
        )
        writer.close()
        result = query(self.reader, "SELECT workout_id, conflict FROM coach_v1_workouts")
        self.assertEqual(result["rows"], [["a", 0], ["c", 0]])
        writer = connect(self.path)
        writer.execute(
            "UPDATE source_records SET values_json = ? WHERE id = 'a'",
            ('{"duration_s": 3000, "distance_m": 9200}',),
        )
        writer.close()
        result = query(self.reader, "SELECT workout_id, conflict FROM coach_v1_workouts")
        self.assertEqual(result["rows"], [["a", 1], ["c", 0]])

    def test_parts_come_from_the_member_with_most_parts_then_lowest_id(self) -> None:
        result = query(self.reader, "SELECT * FROM coach_v1_workout_parts ORDER BY workout_id, seq")
        self.assertEqual(
            result["columns"],
            [
                "workout_id",
                "seq",
                "part_kind",
                "label",
                "reps",
                "weight_kg",
                "duration_min",
                "distance_km",
                "avg_hr",
                "source",
            ],
        )
        self.assertEqual(
            result["rows"],
            [
                ["a", 1, "lap", "Km 1-3", None, None, 15.0, 3.0, 147.0, "garmin"],
                ["a", 2, "lap", "Km 4-6", None, None, 15.0, 3.0, 148.0, "garmin"],
                ["a", 3, "lap", "Km 7-10", None, None, 20.8, 4.24, 149.0, "garmin"],
                ["c", 1, "set", "Bench press", 8, 60.0, None, None, None, "hevy"],
                ["c", 2, "set", "Bench press", 8, 62.5, None, None, None, "hevy"],
                ["c", 3, "set", "Row", 10, 50.0, None, None, None, "hevy"],
            ],
        )

    def test_sources_show_status_and_coverage(self) -> None:
        result = query(self.reader, "SELECT * FROM coach_v1_sources ORDER BY connector")
        self.assertEqual(
            result["columns"],
            ["connector", "enabled", "last_received_at", "last_error", "covered_until"],
        )
        self.assertEqual(
            result["rows"],
            [
                ["garmin", 1, "2025-03-03T09:00:00Z", None, "2025-03-01T07:51:20Z"],
                ["hevy", 1, None, None, "2025-03-02T18:00:00Z"],
                ["polar", 0, None, None, None],
                ["strava", 1, "2025-03-03T08:00:00Z", "Token expired", "2025-03-01T07:50:00Z"],
            ],
        )

    def test_deleted_records_count_for_nothing(self) -> None:
        writer = connect(self.path)
        writer.execute(
            "UPDATE source_records SET deleted_at = '2025-03-04T00:00:00Z'"
            " WHERE id IN ('b', 'c', 'd')"
        )
        writer.close()
        self.assertEqual(
            query(self.reader, "SELECT workout_id, sources, conflict FROM coach_v1_workouts")[
                "rows"
            ],
            [["a", "strava", 0]],
        )
        chosen_query = (
            "SELECT family, label, duration_min, distance_km, active_energy_kcal, avg_hr, max_hr,"
            " source, active_energy_source, avg_hr_source, max_hr_source FROM coach_v1_workouts"
        )
        self.assertEqual(
            query(self.reader, chosen_query)["rows"],
            [[None, None, None, None, 612, None, 171, None, "strava", None, "strava"]],
        )
        self.assertEqual(
            query(self.reader, "SELECT workout_id, seq, source FROM coach_v1_workout_parts")[
                "rows"
            ],
            [["a", 1, "strava"], ["a", 2, "strava"]],
        )
        self.assertEqual(
            query(self.reader, "SELECT covered_until FROM coach_v1_sources WHERE connector='hevy'")[
                "rows"
            ],
            [[None]],
        )

        writer = connect(self.path)
        writer.execute("UPDATE source_records SET deleted_at = NULL WHERE id = 'b'")
        writer.execute(
            "UPDATE source_records SET deleted_at = '2025-03-04T00:00:00Z' WHERE id = 'a'"
        )
        writer.close()
        self.assertEqual(
            query(self.reader, chosen_query)["rows"],
            [["run", "Run", 50.8, 10.24, None, 148.5, None, "garmin", None, "garmin", None]],
        )

    def test_select_over_the_view(self) -> None:
        result = query(self.reader, "SELECT metric, value FROM coach_v1_day_facts LIMIT 2")
        self.assertEqual(
            result,
            {"columns": ["metric", "value"], "rows": [["steps", 1000.0]] * 2, "clipped": None},
        )
        self.assertEqual(
            query(self.reader, "SELECT count(*) FROM coach_v1_day_facts")["rows"], [[600]]
        )
        cte = "WITH t AS (SELECT sum(value) AS total FROM coach_v1_day_facts) SELECT total FROM t"
        self.assertEqual(query(self.reader, cte)["rows"], [[600000.0]])
        self.assertEqual(
            query(self.reader, "SELECT count(*) FROM temp.coach_v1_day_facts")["rows"], [[600]]
        )
        self.assertEqual(
            query(self.reader, "SELECT 'training with friends'")["rows"],
            [["training with friends"]],
        )
        self.assertEqual(
            query(
                self.reader,
                "WITH coach_v1_day_facts AS (SELECT metric FROM temp.coach_v1_day_facts) SELECT count(*) FROM coach_v1_day_facts",
            )["rows"],
            [[600]],
        )
        self.assertEqual(
            query(
                self.reader,
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<3) SELECT sum(x) FROM n",
            )["rows"],
            [[6]],
        )

    def test_everything_else_is_refused(self) -> None:
        views_only = {"error": "Only coach_v1 views can be read."}
        refused = {
            "SELECT * FROM day_facts": views_only,
            "SELECT count(*) FROM day_facts": views_only,
            "SELECT count(*) FROM main.journal": views_only,
            "SELECT * FROM main.source_records": views_only,
            "SELECT count(*) FROM sqlite_temp_master": views_only,
            "SELECT count(*) FROM SQLITE_MASTER": views_only,
            "SELECT * FROM pragma_table_info('source_records')": views_only,
            "SELECT rowid FROM coach_v1_workouts": views_only,
            "SELECT load_extension('unavailable')": views_only,
            "WITH coach_v1_day_facts AS (SELECT * FROM day_facts) SELECT * FROM coach_v1_day_facts": views_only,
            "WITH coach_v1_workouts AS (SELECT values_json, deleted_at FROM source_records) SELECT values_json FROM coach_v1_workouts WHERE deleted_at IS NOT NULL": views_only,
            "WITH outer_cte AS (WITH coach_v1_sources AS (SELECT proposal_json FROM journal) SELECT * FROM coach_v1_sources) SELECT * FROM outer_cte": views_only,
            "SELECT * FROM sqlite_master": views_only,
            "WITH x AS (SELECT * FROM day_facts) SELECT * FROM x": views_only,
            "INSERT INTO day_facts (local_date, metric, value, unit, source, coverage,"
            " formula_version) VALUES ('2030-01-01', 'steps', 1, 'count', 'apple_health',"
            " 'full', 'v1')": views_only,
            "UPDATE day_facts SET value = 0": views_only,
            "DELETE FROM day_facts": views_only,
            "CREATE TABLE x (a)": views_only,
            "DROP TABLE day_facts": views_only,
            "ATTACH DATABASE ':memory:' AS other": views_only,
            "PRAGMA table_info(day_facts)": views_only,
            "SELECT 1; SELECT 2": {"error": "You can only execute one statement at a time."},
        }
        for sql, expected in refused.items():
            with self.subTest(sql=sql):
                self.assertEqual(query(self.reader, sql), expected)

    def test_rows_stop_at_500(self) -> None:
        result = query(self.reader, "SELECT local_date FROM coach_v1_day_facts")
        self.assertEqual(len(result["rows"]), 500)
        self.assertEqual(result["clipped"], "Stopped at 500 rows.")

    def test_bytes_stop_before_200000(self) -> None:
        # Row sizes step by one character so some size lands the count on the limit.
        for length in range(1000, 1200):
            with self.subTest(length=length):
                sql = "SELECT substr(hex(zeroblob(2000)), 1, LENGTH) FROM coach_v1_day_facts"
                result = query(self.reader, sql.replace("LENGTH", str(length)))
                self.assertEqual(result["clipped"], "Stopped at 200,000 bytes.")
                self.assertLessEqual(len(json.dumps(result)), 200_000)

    def test_huge_rows_stop_at_the_byte_limit(self) -> None:
        result = query(self.reader, "SELECT printf('%.*c', 10000000, 'a') FROM coach_v1_day_facts")
        self.assertEqual(
            result,
            {
                "columns": ["printf('%.*c', 10000000, 'a')"],
                "rows": [],
                "clipped": "Stopped at 200,000 bytes.",
            },
        )

    def test_binary_value_is_an_error(self) -> None:
        self.assertEqual(
            query(self.reader, "SELECT x'00'"),
            {"error": "A result held binary data, which cannot be shown."},
        )

    def test_long_query_is_stopped(self) -> None:
        sql = (
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT count(*) FROM n"
        )
        self.assertEqual(query(self.reader, sql), {"error": "The query ran longer than 2 seconds."})

    def test_waiting_date_shows_as_importing(self) -> None:
        writer = connect(self.path)
        writer.execute("INSERT INTO rebuild_dates (local_date) VALUES ('2025-01-01')")
        writer.close()
        result = query(
            self.reader,
            "SELECT local_date, coverage FROM coach_v1_day_facts WHERE local_date <= '2025-01-02'"
            " ORDER BY local_date",
        )
        self.assertEqual(result["rows"], [["2025-01-01", "importing"], ["2025-01-02", "full"]])

    def test_empty_plan_views_and_described_columns(self) -> None:
        expected = {
            "coach_v1_plan_items": [
                "plan_item_id",
                "version",
                "plan_version",
                "local_date",
                "start_hint",
                "family",
                "label",
                "goal_ids_json",
                "why",
                "sources_json",
                "send_into_json",
                "status",
                "matched_workout_id",
                "match_reason",
                "matched_record_ids_json",
            ],
            "coach_v1_plan_parts": [
                "plan_item_id",
                "seq",
                "part_kind",
                "label",
                "reps",
                "weight_kg",
                "duration_min",
                "distance_km",
                "avg_hr",
                "extras_json",
            ],
            "coach_v1_plan_state": ["plan_version"],
        }
        meanings = describe(self.reader)
        for view, columns in expected.items():
            with self.subTest(view=view):
                self.assertEqual(meanings[view]["columns"], columns)
        self.assertEqual(query(self.reader, "SELECT * FROM coach_v1_plan_items")["rows"], [])
        self.assertEqual(query(self.reader, "SELECT * FROM coach_v1_plan_parts")["rows"], [])
        self.assertEqual(query(self.reader, "SELECT * FROM coach_v1_plan_state")["rows"], [[0]])

    def test_saved_plan_targets_and_global_version(self) -> None:
        now = datetime(2026, 11, 7, 12, tzinfo=UTC)
        first = {
            "plan_version": 0,
            "why": "Build endurance",
            "sources": [{"url": "https://example.org/study", "type": "trial"}],
            "actions": [
                {
                    "action": "add_session",
                    "item": item,
                    "date": "2026-11-08",
                    "family": "run",
                    "label": "Easy run",
                    "parts": [
                        {"part_kind": "interval", "duration_s": 61, "distance_m": 6001},
                        {"part_kind": "interval", "label": "Cool down"},
                    ],
                }
                for item in ("p1", "p2")
            ],
        }
        save(self.path, first, request_id="first", approved_by="chat", now=now)
        save(
            self.path,
            {
                "plan_version": 1,
                "why": "Rest instead",
                "sources": [],
                "actions": [{"action": "remove_session", "item": "p2"}],
            },
            request_id="second",
            approved_by="chat",
            now=now,
        )
        result = query(
            self.reader,
            "SELECT plan_item_id, version, plan_version, local_date, family, label, goal_ids_json, why, sources_json, status FROM coach_v1_plan_items ORDER BY plan_item_id",
        )
        self.assertEqual(
            result["rows"],
            [
                [
                    "p1",
                    1,
                    1,
                    "2026-11-08",
                    "run",
                    "Easy run",
                    "[]",
                    "Build endurance",
                    '[{"type":"trial","url":"https://example.org/study"}]',
                    "saved",
                ],
                [
                    "p2",
                    2,
                    2,
                    "2026-11-08",
                    "run",
                    "Easy run",
                    "[]",
                    "Rest instead",
                    "[]",
                    "removed",
                ],
            ],
        )
        self.assertEqual(query(self.reader, "SELECT * FROM coach_v1_plan_state")["rows"], [[2]])
        self.assertEqual(
            query(
                self.reader,
                "SELECT * FROM coach_v1_plan_parts WHERE plan_item_id='p1' ORDER BY seq",
            )["rows"],
            [
                ["p1", 1, "interval", None, None, None, 61 / 60, 6.001, None, None],
                ["p1", 2, "interval", "Cool down", None, None, None, None, None, None],
            ],
        )
        self.assertEqual(
            query(self.reader, "SELECT count(*) FROM coach_v1_plan_parts")["rows"], [[4]]
        )
        self.assertEqual(
            query(self.reader, "SELECT count(*) FROM coach_v1_workouts")["rows"], [[2]]
        )
        self.assertEqual(
            query(self.reader, "SELECT count(*) FROM coach_v1_workout_parts")["rows"], [[6]]
        )

    def test_goal_and_limit_views_follow_save(self) -> None:
        meanings = describe(self.reader)
        self.assertEqual(
            meanings["coach_v1_goals"]["columns"], ["goal_id", "type", "local_date", "target_json"]
        )
        self.assertEqual(
            meanings["coach_v1_limits"]["columns"], ["limit_id", "kind", "values_json"]
        )
        self.assertEqual(query(self.reader, "SELECT * FROM coach_v1_goals")["rows"], [])
        self.assertEqual(query(self.reader, "SELECT * FROM coach_v1_limits")["rows"], [])
        now = datetime(2026, 11, 7, tzinfo=UTC)
        proposal = {
            "plan_version": 0,
            "why": "Prepare steadily",
            "sources": [],
            "actions": [
                {
                    "action": "set_goal",
                    "item": "g1",
                    "type": "custom",
                    "date": "2027-04-01",
                    "target": {"description": "Complete the course", "value": 42.195, "unit": "km"},
                },
                {
                    "action": "set_limit",
                    "item": "l1",
                    "kind": "max_session_length",
                    "values": {"minutes": 60},
                },
            ],
        }
        save(self.path, proposal, request_id="goals", approved_by="chat", now=now)
        self.assertEqual(
            query(self.reader, "SELECT * FROM coach_v1_goals")["rows"],
            [
                [
                    "g1",
                    "custom",
                    "2027-04-01",
                    '{"description":"Complete the course","unit":"km","value":42.195}',
                ]
            ],
        )
        self.assertEqual(
            query(self.reader, "SELECT * FROM coach_v1_limits")["rows"],
            [["l1", "max_session_length", '{"minutes":60}']],
        )
        self.assertEqual(query(self.reader, "SELECT * FROM coach_v1_plan_state")["rows"], [[1]])


if __name__ == "__main__":
    unittest.main()
