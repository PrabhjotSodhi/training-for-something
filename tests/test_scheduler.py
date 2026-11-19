import unittest

from tests.helpers import open_database
from tfs.scheduling.scheduler import run_due


class SchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(self)

    def add_rule(self, rule_id: str, next_due: str, hourly_cap: int = 5) -> None:
        self.conn.execute(
            "INSERT INTO rules(id, revision, period_minutes, next_due, hourly_cap)"
            " VALUES (?, 1, 60, ?, ?)",
            (rule_id, next_due, hourly_cap),
        )

    def run_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM rule_runs").fetchone()[0]

    def next_due(self, rule_id: str) -> str:
        return self.conn.execute("SELECT next_due FROM rules WHERE id = ?", (rule_id,)).fetchone()[
            0
        ]

    def test_wake_after_sleep_runs_once_and_moves_next_due_past_now(self) -> None:
        # GIVEN a rule due at 09:00
        self.add_rule("rule-a", "2026-03-02T09:00:00Z")
        # WHEN the computer wakes at 13:00
        run_due(self.conn, "2026-03-02T13:00:00Z")
        # THEN one run exists and the rule is next due after 13:00
        self.assertEqual(self.run_count(), 1)
        self.assertEqual(self.next_due("rule-a"), "2026-03-02T14:00:00Z")

    def test_restart_does_not_repeat_an_occurrence(self) -> None:
        # GIVEN a run was made and the rule is due again as if the update never saved
        self.add_rule("rule-a", "2026-03-02T09:00:00Z")
        run_due(self.conn, "2026-03-02T09:00:00Z")
        self.conn.execute("UPDATE rules SET next_due = '2026-03-02T09:00:00Z'")
        # WHEN the engine runs the loop again
        run_due(self.conn, "2026-03-02T09:01:00Z")
        # THEN no second run exists
        self.assertEqual(self.run_count(), 1)

    def test_rule_over_hourly_cap_waits_for_next_hour(self) -> None:
        # GIVEN a rule with a cap of 1 that already ran this hour and is due again
        self.add_rule("rule-a", "2026-03-02T09:00:00Z", hourly_cap=1)
        run_due(self.conn, "2026-03-02T09:00:00Z")
        self.conn.execute("UPDATE rules SET next_due = '2026-03-02T09:10:00Z'")
        # WHEN the loop runs again in the same hour
        run_due(self.conn, "2026-03-02T09:20:00Z")
        # THEN it waits
        self.assertEqual(self.run_count(), 1)
        # WHEN the next hour starts
        run_due(self.conn, "2026-03-02T10:00:00Z")
        # THEN it runs
        self.assertEqual(self.run_count(), 2)


if __name__ == "__main__":
    unittest.main()
