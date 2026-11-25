import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tfs.mcp.tool_access import open_reader, query
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.proposals.apply import save

PLATE = {"goal_id": "g1", "colour": "orange", "rows": ["readiness", "load_cardio"]}


def overview(*cards: dict, layout: bool = True) -> dict:
    return {
        "plan_version": 0,
        "why": "Show what matters",
        "sources": [],
        "actions": [
            {
                "action": "set_overview",
                "item": "overview",
                "layout": {"plate": PLATE, "cards": list(cards)} if layout else None,
            }
        ],
    }


class OverviewLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "tfs.db"
        connection = connect(self.path)
        self.addCleanup(connection.close)
        migrate(connection)
        self.version = 0

    def save(self, value: dict) -> None:
        value["plan_version"] = self.version
        self.version = save(
            self.path,
            value,
            request_id=f"save-{self.version}",
            approved_by="chat",
            now=datetime(2026, 11, 7, 12, tzinfo=UTC),
        )["plan_version"]

    def cards(self) -> list:
        reader = open_reader(self.path)
        self.addCleanup(reader.close)
        rows = query(reader, "SELECT json_extract(layout_json, '$.cards') FROM coach_v1_overview")
        return rows["rows"]

    def test_the_coach_reads_the_saved_layout_and_the_newest_replaces_it(self) -> None:
        # GIVEN a layout with one resting heart rate trend card
        # WHEN the person approves it
        self.save(overview({"type": "trend", "metric": "resting_hr"}))
        # THEN the view returns that layout
        self.assertEqual(self.cards(), [['[{"metric":"resting_hr","type":"trend"}]']])
        # WHEN a second layout is approved
        self.save(overview({"type": "lift", "exercise": "press-a"}))
        # THEN the view returns the second
        self.assertEqual(self.cards(), [['[{"exercise":"press-a","type":"lift"}]']])

    def test_null_returns_to_the_default_and_an_unknown_metric_is_refused(self) -> None:
        # GIVEN a saved layout
        self.save(overview({"type": "week"}))
        # WHEN set_overview with null is approved THEN the view returns no row
        self.save(overview(layout=False))
        self.assertEqual(self.cards(), [])
        # WHEN a card names a metric outside the catalogue THEN Save refuses and names it
        with self.assertRaisesRegex(ValueError, "squat_top_set"):
            self.save(overview({"type": "trend", "metric": "squat_top_set"}))
