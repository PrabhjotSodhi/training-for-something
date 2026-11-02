import json
import tempfile
import unittest
from pathlib import Path

from tfs.duplicates.workouts import update
from tfs.importer.session import rebuild_pending
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

FILE_SHA256 = "0" * 64
MANIFESTS = {
    "hevy": {"app_ids": ["com.hevyapp.hevy", "Hevy"]},
    "apple_health": {"app_ids": ["com.apple.Health"]},
    "health_auto_export": {"app_ids": []},
}


class DuplicatesTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.conn = connect(Path(folder.name) / "tfs.db")
        self.addCleanup(self.conn.close)
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
            " received_at) VALUES (?, 1, 'hevy', 'json', 'json', 1, '2026-10-01T00:00:00Z')",
            (FILE_SHA256,),
        )

    def add(
        self,
        record_id: str,
        connector: str,
        start: str,
        end: str | None,
        *,
        family: str | None = "strength",
        origin_app: str | None = None,
        local_date: str | None = None,
        values: dict | None = None,
        deleted_at: str | None = None,
        kind: str = "session",
    ) -> None:
        self.conn.execute(
            "INSERT INTO source_records (id, connector, stream, key, kind, revision_at,"
            " arrival_seq, file_sha256, reader_version, start_utc, end_utc, local_date, family,"
            " values_json, origin_app, deleted_at)"
            " VALUES (?, ?, 'workouts', ?, ?, '2026-10-01T00:00:00.000Z', 1, ?, 'r1', ?, ?, ?, ?,"
            " ?, ?, ?)",
            (
                record_id,
                connector,
                record_id,
                kind,
                FILE_SHA256,
                start,
                end,
                local_date or start[:10],
                family,
                json.dumps(values or {}),
                origin_app,
                deleted_at,
            ),
        )

    def reset(self) -> None:
        self.conn.execute("DELETE FROM session_groups")
        self.conn.execute("DELETE FROM source_records")

    def groups(self) -> dict[str, list[str]]:
        rows = self.conn.execute(
            "SELECT group_id, record_id FROM session_group_members ORDER BY group_id, record_id"
        ).fetchall()
        found: dict[str, list[str]] = {}
        for group_id, record_id in rows:
            found.setdefault(group_id, []).append(record_id)
        return found

    def shared(self, first: str, second: str) -> bool:
        return any(first in members and second in members for members in self.groups().values())

    def run_pair(self, offset_s: int, *, length_s: int = 1800, **second) -> bool:
        base = 6 * 3600
        self.add("a", "apple_health", *times(base, base + length_s))
        self.add("b", "hevy", *times(base + offset_s, base + offset_s + length_s), **second)
        update(self.conn, {"2026-10-01"}, MANIFESTS)
        return self.shared("a", "b")

    def test_a_hub_copy_and_the_direct_session_share_a_group(self) -> None:
        self.add("hevy-1", "hevy", "2026-09-30T05:00:00Z", "2026-09-30T06:05:00Z")
        self.add(
            "apple-1",
            "apple_health",
            "2026-09-30T05:00:01Z",
            "2026-09-30T06:05:03Z",
            origin_app="Hevy",
        )

        update(self.conn, {"2026-09-30"}, MANIFESTS)

        self.assertEqual(self.groups(), {"apple-1": ["apple-1", "hevy-1"]})

    def test_an_origin_app_that_a_connector_claims_links_sessions_of_different_families(
        self,
    ) -> None:
        self.add("hevy-1", "hevy", "2026-09-30T05:00:00Z", "2026-09-30T06:00:00Z")
        self.add(
            "apple-1",
            "apple_health",
            "2026-09-30T05:00:01Z",
            "2026-09-30T06:00:01Z",
            family="mixed",
            origin_app="Hevy",
        )
        self.add(
            "apple-2",
            "apple_health",
            "2026-09-30T05:00:02Z",
            "2026-09-30T06:00:02Z",
            family="mixed",
            origin_app="Other Gym App",
        )

        update(self.conn, {"2026-09-30"}, MANIFESTS)

        self.assertTrue(self.shared("hevy-1", "apple-1"))
        self.assertFalse(self.shared("hevy-1", "apple-2"))

    def test_a_shorter_session_inside_a_longer_one_stays_apart(self) -> None:
        self.add("long", "hevy", "2026-10-01T05:00:00Z", "2026-10-01T05:20:00Z")
        self.add("short", "apple_health", "2026-10-01T05:02:00Z", "2026-10-01T05:19:00Z")

        update(self.conn, {"2026-10-01"}, MANIFESTS)

        self.assertEqual(self.groups(), {"long": ["long"], "short": ["short"]})

    def test_starts_and_ends_link_at_180_seconds_apart_and_not_at_181(self) -> None:
        self.assertTrue(self.run_pair(180))
        self.reset()
        self.assertFalse(self.run_pair(181))

    def test_a_session_joins_a_group_only_when_it_agrees_with_every_member(self) -> None:
        for name, connector, minute in (
            ("first", "apple_health", 0),
            ("second", "health_auto_export", 2),
            ("third", "hevy", 4),
        ):
            self.add(
                name,
                connector,
                f"2026-10-01T06:{minute:02d}:00Z",
                f"2026-10-01T06:{40 + minute}:00Z",
                family="run",
            )

        update(self.conn, {"2026-10-01"}, MANIFESTS)

        self.assertEqual(self.groups(), {"first": ["first", "second"], "third": ["third"]})

    def test_a_copy_on_the_next_local_date_joins_the_group_and_the_earliest_date_wins(self) -> None:
        self.add(
            "late",
            "health_auto_export",
            "2026-10-20T11:00:30Z",
            "2026-10-20T11:40:30Z",
            family="run",
            local_date="2026-10-21",
        )
        self.add(
            "early",
            "apple_health",
            "2026-10-20T10:59:00Z",
            "2026-10-20T11:39:00Z",
            family="run",
            local_date="2026-10-20",
        )

        update(self.conn, {"2026-10-21"}, MANIFESTS)

        row = self.conn.execute("SELECT * FROM session_groups").fetchone()
        self.assertEqual(
            tuple(row),
            ("early", "2026-10-20", "run", "2026-10-20T10:59:00Z", "{}"),
        )
        self.assertEqual(self.groups(), {"early": ["early", "late"]})

    def test_the_group_family_is_the_first_one_in_start_order(self) -> None:
        self.add(
            "first", "apple_health", "2026-10-01T06:00:00Z", "2026-10-01T06:30:00Z", family=None
        )
        self.add("second", "hevy", "2026-10-01T06:00:30Z", "2026-10-01T06:30:30Z", family="run")

        update(self.conn, {"2026-10-01"}, MANIFESTS)

        self.assertEqual(
            self.conn.execute("SELECT family FROM session_groups").fetchall()[0][0], "run"
        )

    def test_a_session_with_no_family_needs_a_tighter_match(self) -> None:
        self.assertTrue(self.run_pair(60, family=None))
        self.reset()
        self.assertFalse(self.run_pair(61, family=None))

    def test_a_missing_end_is_the_start_plus_the_duration_and_no_end_at_all_never_links(
        self,
    ) -> None:
        self.add("a", "apple_health", "2026-10-01T06:00:00Z", None, values={"duration_s": 1800})
        self.add("b", "hevy", "2026-10-01T06:00:10Z", "2026-10-01T06:30:10Z")
        self.add("c", "health_auto_export", "2026-10-01T06:00:05Z", None)

        update(self.conn, {"2026-10-01"}, MANIFESTS)

        self.assertEqual(self.groups(), {"a": ["a", "b"], "c": ["c"]})

    def test_a_deleted_record_belongs_to_no_group_and_a_group_of_only_it_goes_away(self) -> None:
        self.add("a", "apple_health", "2026-10-01T06:00:00Z", "2026-10-01T06:30:00Z")
        self.add("b", "hevy", "2026-10-01T06:00:01Z", "2026-10-01T06:30:01Z")
        update(self.conn, {"2026-10-01"}, MANIFESTS)
        self.assertEqual(self.groups(), {"a": ["a", "b"]})

        self.conn.execute(
            "UPDATE source_records SET deleted_at = '2026-10-02T00:00:00Z' WHERE id = 'a'"
        )
        update(self.conn, {"2026-10-01"}, MANIFESTS)
        self.assertEqual(self.groups(), {"b": ["b"]})

        self.conn.execute(
            "UPDATE source_records SET deleted_at = '2026-10-02T00:00:00Z' WHERE id = 'b'"
        )
        update(self.conn, {"2026-10-01"}, MANIFESTS)
        self.assertEqual(self.groups(), {})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_groups").fetchone()[0], 0)

    def test_running_update_twice_gives_identical_rows(self) -> None:
        self.add("a", "apple_health", "2026-10-01T06:00:00Z", "2026-10-01T06:30:00Z")
        self.add("b", "hevy", "2026-10-01T06:00:01Z", "2026-10-01T06:30:01Z")
        self.add("c", "hevy", "2026-10-01T09:00:00Z", "2026-10-01T09:30:00Z")
        self.add("note", "hevy", "2026-10-01T09:00:00Z", None, kind="event")

        self.assertEqual(update(self.conn, {"2026-10-01"}, MANIFESTS), 2)
        first = self.snapshot()
        self.assertEqual(update(self.conn, {"2026-10-01"}, MANIFESTS), 2)

        self.assertEqual(self.snapshot(), first)

    def test_the_groups_do_not_depend_on_the_order_the_records_arrived_in(self) -> None:
        sessions = [
            ("a", "apple_health", "2026-10-01T06:00:00Z", "2026-10-01T06:40:00Z"),
            ("b", "health_auto_export", "2026-10-01T06:02:00Z", "2026-10-01T06:42:00Z"),
            ("c", "hevy", "2026-10-01T06:01:00Z", "2026-10-01T06:41:00Z"),
        ]
        for session in sessions:
            self.add(*session, family="run")
        update(self.conn, {"2026-10-01"}, MANIFESTS)
        expected = self.snapshot()

        self.reset()
        for session in reversed(sessions):
            self.add(*session, family="run")
        update(self.conn, {"2026-10-01"}, MANIFESTS)

        self.assertEqual(self.snapshot(), expected)

    def test_a_group_on_a_date_that_is_not_rebuilt_keeps_its_members(self) -> None:
        self.add("old-a", "apple_health", "2026-09-01T06:00:00Z", "2026-09-01T06:30:00Z")
        self.add("old-b", "hevy", "2026-09-01T06:00:01Z", "2026-09-01T06:30:01Z")
        update(self.conn, {"2026-09-01"}, MANIFESTS)

        update(self.conn, {"2026-10-01"}, MANIFESTS)

        self.assertEqual(self.groups(), {"old-a": ["old-a", "old-b"]})

    def test_rebuilding_pending_dates_groups_the_sessions_and_clears_the_dates(self) -> None:
        self.add(
            "a",
            "apple_health",
            "2026-10-01T06:00:00Z",
            "2026-10-01T06:30:00Z",
            family="mixed",
            origin_app="Hevy",
        )
        self.add("b", "hevy", "2026-10-01T06:00:01Z", "2026-10-01T06:30:01Z")
        self.conn.execute("INSERT INTO rebuild_dates (local_date) VALUES ('2026-10-01')")

        self.assertEqual(rebuild_pending(self.conn), 1)

        self.assertEqual(self.groups(), {"a": ["a", "b"]})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM rebuild_dates").fetchone()[0], 0)

    def snapshot(self) -> tuple[list, list]:
        groups = self.conn.execute("SELECT * FROM session_groups ORDER BY id").fetchall()
        members = self.conn.execute(
            "SELECT * FROM session_group_members ORDER BY group_id, record_id"
        ).fetchall()
        return [tuple(row) for row in groups], [tuple(row) for row in members]


def times(start_s: int, end_s: int) -> tuple[str, str]:
    """Return 2026-10-01 start and end texts for offsets in seconds from midnight UTC."""
    return tuple(
        f"2026-10-01T{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}Z" for s in (start_s, end_s)
    )


if __name__ == "__main__":
    unittest.main()
