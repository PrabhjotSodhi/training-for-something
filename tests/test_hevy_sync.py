import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from unittest import mock

from tests.test_proposals import ADD, proposal
from tests.test_setup import FakeCommands, FakeQuestionary
from tfs import config, password_store
from tfs.cli import main
from tfs.connectors.core import runner
from tfs.connectors.hevy import connector
from tfs.engine import FETCH_INTERVAL, Engine
from tfs.mcp.tool_access import open_reader, query
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.proposals.apply import save

API_KEY = "invented-key-123"
NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
PLAN_TIME = datetime(2026, 11, 19, 12, tzinfo=UTC)
WORKOUT = {
    "id": "w-1",
    "title": "Push A",
    "routine_id": "routine-1",
    "start_time": "2026-11-20T10:00:00+00:00",
    "end_time": "2026-11-20T11:00:00+00:00",
    "updated_at": "2026-11-20T11:01:00.000Z",
    "exercises": [
        {
            "index": 0,
            "title": "Bench Press",
            "exercise_template_id": "t-1",
            "sets": [{"index": 0, "type": "normal", "weight_kg": 60, "reps": 8}],
        }
    ],
}
ROUTINES = [
    {"id": "routine-1", "title": "Push A", "folder_id": 7, "updated_at": "2026-11-18T09:00:00Z"},
    {"id": "routine-2", "title": "Legs", "folder_id": None, "updated_at": "2026-11-19T09:00:00Z"},
]


TEMPLATES = [{"id": "t-chest", "primary_muscle_group": "chest"}, {"id": "t-untagged"}]


def workout_of(*exercises: tuple[str, list[str]]) -> dict:
    """A workout holding one exercise per (template id, set types) pair."""
    return {
        **WORKOUT,
        "exercises": [
            {
                "index": index,
                "title": template,
                "exercise_template_id": template,
                "sets": [{"index": n, "type": kind, "reps": 8} for n, kind in enumerate(kinds)],
            }
            for index, (template, kinds) in enumerate(exercises)
        ],
    }


class FakeHevy:
    """Stands in for the Hevy API and for the connector child process."""

    def __init__(self, *, accepts_key: bool = True) -> None:
        self.accepts_key = accepts_key
        self.fetches = 0
        self.routines = [{**routine, "exercises": []} for routine in ROUTINES]
        self.workouts = [WORKOUT]
        self.deleted: list[str] = []

    def http(self, url: str, headers: dict, timeout_s: float = 30) -> tuple[int, dict, bytes]:
        if not self.accepts_key:
            return 401, {}, b""
        if "/routines?" in url:
            body = {"page": 1, "page_count": 1, "routines": self.routines}
            return 200, {}, json.dumps(body).encode("utf-8")
        if "/exercise_templates?" in url:
            body = {"page": 1, "page_count": 1, "exercise_templates": TEMPLATES}
            return 200, {}, json.dumps(body).encode("utf-8")
        events = [
            {"type": "deleted", "id": key, "deleted_at": "2026-11-20T13:00:00Z"}
            for key in self.deleted
        ]
        body = {"page": 1, "page_count": 1, "workouts": self.workouts, "events": events}
        return 200, {}, json.dumps(body).encode("utf-8")

    def run(self, name: str, op: str, payload: dict, **options) -> dict:
        extra = {"http": self.http} if op == "fetch" else {}
        self.fetches += op == "fetch"
        return getattr(connector, op)(**payload, **extra)


class HevyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = mock.patch.dict(os.environ, {"TFS_HOME": home.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.hevy = FakeHevy()

    def use_hevy(self, hevy: FakeHevy) -> None:
        patch = mock.patch.object(runner, "run", hevy.run)
        patch.start()
        self.addCleanup(patch.stop)

    def sources(self) -> list[tuple]:
        with contextlib.closing(connect(config.db_path())) as conn:
            return [tuple(row) for row in conn.execute("SELECT connector, enabled FROM sources")]


class SetupSyncTest(HevyTestCase):
    def run_setup(self) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)),
            mock.patch("tfs.cli.setup.questionary", FakeQuestionary(["hevy"], API_KEY)),
            mock.patch("tfs.providers.shutil.which", FakeCommands(found=set()).which),
            mock.patch("tfs.cli.setup.webbrowser.open"),
            mock.patch("tfs.cli.setup.start.run", return_value=0),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            main(["setup"])
        return out.getvalue(), err.getvalue()

    def test_given_a_valid_key_then_setup_stores_the_workouts_and_says_how_many(self):
        # GIVEN a Hevy that accepts the key
        self.use_hevy(self.hevy)
        # WHEN setup connects Hevy
        out, _ = self.run_setup()
        # THEN the workout is stored and setup reports it
        self.assertIn("1 record stored", out)
        self.assertEqual(self.sources(), [("hevy", 1)])
        with contextlib.closing(connect(config.db_path())) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 1)

    def test_given_a_rejected_key_then_setup_shows_the_error_and_hevy_is_not_connected(self):
        # GIVEN a Hevy that rejects the key
        self.use_hevy(FakeHevy(accepts_key=False))
        # WHEN setup connects Hevy
        out, err = self.run_setup()
        # THEN setup shows the error and the source is off
        self.assertIn("Reconnect Hevy", err)
        self.assertNotIn("Connected Hevy", out)
        self.assertEqual(self.sources(), [("hevy", 0)])


class EngineFetchTest(HevyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_hevy(self.hevy)
        self.clock = NOW
        self.engine = Engine(config.data_dir(), now_fn=lambda: self.clock)
        config.data_dir().mkdir(parents=True, exist_ok=True)
        password_store.set("hevy:api_key", API_KEY)
        self.conn = connect(config.db_path())
        self.addCleanup(self.conn.close)
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO sources (connector, last_attempt_at) VALUES ('hevy', ?)",
            (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )

    def test_given_tfs_running_when_an_hour_passes_then_hevy_is_fetched_once_and_the_cursor_moves(
        self,
    ):
        # GIVEN Hevy was fetched a moment ago
        self.engine._fetch_due()
        self.assertEqual(self.hevy.fetches, 0)
        # WHEN an hour passes and the engine checks twice
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        self.engine._fetch_due()
        # THEN Hevy is fetched once and the cursor moves
        self.assertEqual(self.hevy.fetches, 1)
        cursor = self.conn.execute("SELECT cursor_json FROM sources").fetchone()[0]
        self.assertEqual(json.loads(cursor), {"since": "2026-11-20T12:55:00Z"})

    def test_given_a_routine_deleted_in_hevy_when_the_next_fetch_runs_then_it_drops_out(self):
        # GIVEN TFS has read two Hevy routines
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        # WHEN one is deleted in Hevy and the next fetch runs
        self.hevy.routines.pop()
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        # THEN the ask TFS tool lists only the routine that is left
        with contextlib.closing(open_reader(config.db_path())) as reader:
            sql = "SELECT connector, remote_id, title, folder, remote_version FROM coach_v1_app_targets"
            rows = query(reader, sql)["rows"]
        first = ROUTINES[0]
        self.assertEqual(
            rows, [["hevy", first["id"], first["title"], first["folder_id"], first["updated_at"]]]
        )

    def sets_by_muscle(self) -> list[list]:
        with contextlib.closing(open_reader(config.db_path())) as reader:
            sql = "SELECT week_start, muscle, sets FROM coach_v1_sets_by_muscle"
            return query(reader, sql)["rows"]

    def test_given_3_working_sets_and_a_warmup_tagged_chest_then_chest_has_3(self):
        # GIVEN a week with 3 working sets of a chest exercise and 1 warmup set
        self.hevy.workouts = [workout_of(("t-chest", ["warmup", "normal", "failure", "dropset"]))]
        # WHEN Hevy is fetched
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        # THEN chest has 3 for that week
        self.assertEqual(self.sets_by_muscle(), [["2026-11-16", "chest", 3]])

    def test_given_a_workout_deleted_in_hevy_when_the_next_sync_runs_then_its_sets_leave_the_count(
        self,
    ):
        # GIVEN two stored workouts, each with one chest set
        second = {**workout_of(("t-chest", ["normal"])), "id": "w-2"}
        self.hevy.workouts = [workout_of(("t-chest", ["normal"])), second]
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        self.assertEqual(self.sets_by_muscle(), [["2026-11-16", "chest", 2]])
        # WHEN one is deleted in Hevy and the next sync runs
        self.hevy.deleted = ["w-1"]
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        # THEN only the other workout's set counts
        self.assertEqual(self.sets_by_muscle(), [["2026-11-16", "chest", 1]])

    def test_given_an_exercise_with_no_muscle_tag_then_its_sets_go_under_other(self):
        # GIVEN a workout with 2 sets of an exercise whose template has no muscle tag
        self.hevy.workouts = [workout_of(("t-untagged", ["normal", "normal"]))]
        # WHEN Hevy is fetched
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        # THEN the sets count under other
        self.assertEqual(self.sets_by_muscle(), [["2026-11-16", "other", 2]])

    def test_given_a_source_that_does_not_fetch_then_the_engine_leaves_it_alone(self):
        # GIVEN an enabled push source that was never fetched
        self.conn.execute("INSERT INTO sources (connector) VALUES ('health_auto_export')")
        # WHEN an hour passes
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        # THEN only Hevy was fetched
        self.assertEqual(self.hevy.fetches, 1)

    def test_given_a_workout_from_a_sent_routine_when_the_next_fetch_runs_then_the_plan_item_is_done(
        self,
    ):
        # GIVEN a saved plan item that TFS sent to Hevy as routine-1
        item = {**ADD, "date": "2026-11-20"}
        value = {**proposal(item), "delivery_targets": {"p1": ["hevy"]}}
        save(config.db_path(), value, request_id="plan", approved_by="chat", now=PLAN_TIME)
        job = self.conn.execute("SELECT id FROM jobs").fetchone()[0]
        self.conn.execute("UPDATE jobs SET status = 'sent'")
        self.conn.execute(
            "INSERT INTO remote_links VALUES ('p1', 'hevy', 'routine-1', 'version-1', ?)", (job,)
        )
        # WHEN the next fetch runs
        self.clock += FETCH_INTERVAL
        self.engine._fetch_due()
        # THEN the plan item is done
        with contextlib.closing(open_reader(config.db_path())) as reader:
            rows = query(reader, "SELECT status FROM coach_v1_plan_items")["rows"]
        self.assertEqual(rows, [["done"]])
