import contextlib
import subprocess
import sys
import unittest
from pathlib import Path

from tests import crash_child, test_crash_raw
from tfs.persistence.sqlite import connect

GROUP_DATES = "SELECT local_date FROM session_groups ORDER BY local_date"
FACT_DATES = "SELECT DISTINCT local_date FROM day_facts ORDER BY local_date"
WAITING_DATES = "SELECT local_date FROM rebuild_dates ORDER BY local_date"
GROUPED_RECORDS = "SELECT record_id FROM session_group_members"


class CrashDerivedTest(unittest.TestCase):
    new_folder = test_crash_raw.CrashRawTest.new_folder
    restart = test_crash_raw.CrashRawTest.restart
    snapshot = test_crash_raw.CrashRawTest.snapshot

    def crash_at(self, point: str, scenario: str) -> Path:
        """Run the scenario in a child process that stops at the point, and return its folder."""
        folder = self.new_folder()
        child = subprocess.run(
            [sys.executable, "-m", "tests.crash_child", point, str(folder), scenario],
            cwd=test_crash_raw.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((child.returncode, child.stderr), (1, ""))
        return folder

    def uncrashed(self, scenario: str) -> Path:
        folder = self.new_folder()
        for upload in crash_child.SCENARIOS[scenario]:
            crash_child.operate(folder, *upload)
        return folder

    def column(self, folder: Path, query: str) -> list[str]:
        with contextlib.closing(connect(folder / "tfs.db")) as conn:
            return [row[0] for row in conn.execute(query)]

    def test_a_crash_before_the_rebuild_still_rebuilds_every_changed_date(self) -> None:
        expected = self.uncrashed("one file")
        self.assertEqual(self.column(expected, GROUP_DATES), ["2026-02-28"])
        self.assertEqual(self.column(expected, FACT_DATES), ["2026-02-28"])
        folder = self.crash_at("before_rebuild", "one file")
        self.assertEqual(self.column(folder, GROUP_DATES), [])
        self.assertEqual(self.column(folder, WAITING_DATES), ["2026-02-28"])

        self.restart(folder)

        self.assertEqual(self.snapshot(folder), self.snapshot(expected))
        self.assertEqual(self.column(folder, WAITING_DATES), [])

    def test_a_record_that_moved_dates_before_a_crash_counts_only_on_the_new_date(self) -> None:
        expected = self.uncrashed("moved")
        self.assertEqual(self.column(expected, GROUP_DATES), ["2026-10-20"])
        self.assertEqual(self.column(expected, FACT_DATES), ["2026-10-20"])
        folder = self.crash_at("before_rebuild", "moved")
        self.assertEqual(self.column(folder, GROUP_DATES), ["2026-10-01"])
        self.assertEqual(self.column(folder, WAITING_DATES), ["2026-10-01", "2026-10-20"])

        self.restart(folder)

        self.assertEqual(self.snapshot(folder), self.snapshot(expected))
        self.assertEqual(self.column(folder, WAITING_DATES), [])

    def test_a_delete_before_a_crash_leaves_its_session_in_no_group(self) -> None:
        expected = self.uncrashed("deleted")
        self.assertEqual(self.column(expected, GROUPED_RECORDS), [])
        self.assertEqual(self.column(expected, FACT_DATES), ["2026-10-01"])
        folder = self.crash_at("before_rebuild", "deleted")
        self.assertEqual(len(self.column(folder, GROUPED_RECORDS)), 1)
        self.assertEqual(self.column(folder, WAITING_DATES), ["2026-10-01"])

        self.restart(folder)

        self.assertEqual(self.snapshot(folder), self.snapshot(expected))
        self.assertEqual(self.column(folder, GROUPED_RECORDS), [])
        self.assertEqual(self.column(folder, WAITING_DATES), [])

    def test_a_crash_between_two_months_rebuilds_both(self) -> None:
        expected = self.uncrashed("two months")
        self.assertEqual(self.column(expected, GROUP_DATES), ["2026-08-30", "2026-09-02"])
        self.assertEqual(self.column(expected, FACT_DATES), ["2026-08-30", "2026-09-02"])
        folder = self.crash_at("before_second_month", "two months")
        self.assertEqual(self.column(folder, GROUP_DATES), ["2026-08-30"])
        self.assertEqual(self.column(folder, WAITING_DATES), ["2026-09-02"])

        self.restart(folder)

        self.assertEqual(self.snapshot(folder), self.snapshot(expected))
        self.assertEqual(self.column(folder, WAITING_DATES), [])
