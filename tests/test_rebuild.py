import copy
import io
import json
from datetime import timedelta, timezone
from pathlib import Path

from tests.test_read_file import NOW, SessionTest
from tfs.importer.raw_store import save_raw
from tfs.importer.session import process_pending, rebuild_all

FIXTURES = Path(__file__).parent / "fixtures"
NZ = timezone(timedelta(hours=13))
TABLE_QUERIES = (
    "SELECT * FROM source_records",
    "SELECT * FROM source_deletes",
    "SELECT * FROM session_parts",
    "SELECT * FROM session_groups",
    "SELECT * FROM session_group_members",
    "SELECT * FROM day_facts",
)
LIVE_FILES = (
    ("hevy", "workouts_page", "json", "hevy/workouts_page.json"),
    ("hevy", "events_page", "json", "hevy/events_page.json"),
    ("health_auto_export", "rest_json", "json", "health_auto_export/metrics.json"),
    ("health_auto_export", "rest_json", "json", "health_auto_export/workouts.json"),
    ("apple_health", "file", "xml", "apple_health/sample_export.xml"),
)


class RebuildAllTest(SessionTest):
    def test_a_revised_sleep_summary_replaces_its_stages(self) -> None:
        # GIVEN an aggregated night that reports REM and core
        night = {
            "sleepStart": "2026-10-20 23:00:00 +0000",
            "sleepEnd": "2026-10-21 06:00:00 +0000",
            "totalSleep": 7,
        }
        for stages in ({"rem": 2, "core": 5}, {"core": 7}):
            # WHEN the same source revises the summary and leaves REM out
            entry = {"name": "sleep_analysis", "units": "hr", "data": [night | stages]}
            self.save_and_process(
                "health_auto_export",
                "rest_json",
                "json",
                json.dumps({"data": {"metrics": [entry]}}).encode(),
            )
        # THEN the stored night has only the stages in the revised summary
        facts = dict(self.conn.execute("SELECT metric, value FROM day_facts"))
        self.assertEqual(facts, {"sleep_asleep": 7, "sleep_core": 7})

    def save_and_process(self, connector: str, fmt: str, ext: str, data: bytes, **kwargs) -> None:
        save_raw(self.conn, self.data_dir, connector, fmt, ext, io.BytesIO(data), now=NOW, **kwargs)
        process_pending(self.conn, self.data_dir, tz=NZ)

    def read_live(self) -> None:
        for connector, fmt, ext, name in LIVE_FILES:
            self.save_and_process(connector, fmt, ext, (FIXTURES / name).read_bytes())

    def snapshot(self) -> dict[str, list[tuple]]:
        return {
            query: sorted((tuple(row) for row in self.conn.execute(query)), key=repr)
            for query in TABLE_QUERIES
        }

    def test_a_rebuild_gives_the_same_rows_as_live_processing(self) -> None:
        self.read_live()
        before = self.snapshot()
        self.assertTrue(all(before.values()))

        self.assertEqual(rebuild_all(self.conn, self.data_dir, tz=NZ), 5)

        self.assertEqual(self.snapshot(), before)

    def test_the_newer_version_of_a_record_still_wins(self) -> None:
        page = json.loads((FIXTURES / "hevy" / "workouts_page.json").read_bytes())
        newer = copy.deepcopy(page)
        newer["workouts"][0]["title"] = "Push A newer"
        older = copy.deepcopy(page)
        older["workouts"][0]["title"] = "Push A older"
        older["workouts"][0]["updated_at"] = "2026-10-19T19:06:12.000Z"
        for version in (newer, older):
            data = json.dumps(version).encode()
            self.save_and_process("hevy", "workouts_page", "json", data)

        def label() -> str:
            return self.conn.execute(
                "SELECT label FROM source_records WHERE key = 'w-001'"
            ).fetchone()[0]

        self.assertEqual(label(), "Push A newer")
        rebuild_all(self.conn, self.data_dir, tz=NZ)
        self.assertEqual(label(), "Push A newer")

    def test_a_rebuild_keeps_the_file_facts_and_refreshes_the_read_facts(self) -> None:
        self.read_live()
        self.save_and_process(
            "hevy", "events_page", "json", b'{"events": []}', cursor_json='{"since": "x"}'
        )
        gone = self.conn.execute(
            "SELECT sha256 FROM files WHERE connector = 'apple_health'"
        ).fetchone()[0]
        read_facts = "SELECT sha256, status, message, reader_version FROM files"
        live = {sha256: tuple(rest) for sha256, *rest in self.conn.execute(read_facts)}
        self.conn.execute("UPDATE files SET status = 'missing' WHERE sha256 = ?", (gone,))
        self.conn.execute("UPDATE files SET reader_version = 'older', message = 'old'")
        facts = "SELECT sha256, arrival_seq, received_at FROM files ORDER BY arrival_seq"
        cursors = "SELECT * FROM sources ORDER BY connector"
        kept = (self.conn.execute(facts).fetchall(), self.conn.execute(cursors).fetchall())

        rebuild_all(self.conn, self.data_dir, tz=NZ)

        self.assertEqual(
            (self.conn.execute(facts).fetchall(), self.conn.execute(cursors).fetchall()), kept
        )
        after = {sha256: tuple(rest) for sha256, *rest in self.conn.execute(read_facts)}
        self.assertEqual(after, {**live, gone: ("missing", "old", "older")})
