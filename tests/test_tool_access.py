import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tfs.mcp.tool_access import VIEWS, describe, open_reader, query
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect


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

    def test_everything_else_is_refused(self) -> None:
        refused = [
            "SELECT * FROM day_facts",
            "SELECT * FROM sqlite_master",
            "WITH x AS (SELECT * FROM day_facts) SELECT * FROM x",
            "INSERT INTO coach_v1_day_facts VALUES ('2025-01-01', 'a', 1, 'u', 's', 'full')",
            "UPDATE day_facts SET value = 0",
            "DELETE FROM day_facts",
            "CREATE TABLE x (a)",
            "DROP TABLE day_facts",
            "ATTACH DATABASE ':memory:' AS other",
            "PRAGMA table_info(day_facts)",
            "SELECT 1; SELECT 2",
        ]
        for sql in refused:
            with self.subTest(sql=sql):
                self.assertEqual(list(query(self.reader, sql)), ["error"])
        self.assertEqual(
            query(self.reader, "SELECT * FROM day_facts"),
            {"error": "Only coach_v1 views can be read."},
        )

    def test_rows_stop_at_500(self) -> None:
        result = query(self.reader, "SELECT local_date FROM coach_v1_day_facts")
        self.assertEqual(len(result["rows"]), 500)
        self.assertEqual(result["clipped"], "Stopped at 500 rows.")

    def test_bytes_stop_before_200000(self) -> None:
        result = query(
            self.reader, "SELECT replace(hex(zeroblob(1000)), '0', 'a') FROM coach_v1_day_facts"
        )
        self.assertEqual(result["clipped"], "Stopped at 200,000 bytes.")
        self.assertLess(len(json.dumps(result)), 200_000)

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


if __name__ == "__main__":
    unittest.main()
