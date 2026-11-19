import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp import Client, ClientSession, StdioServerParameters, stdio_client, types
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS

from tfs.mcp import tool_access
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.proposals.apply import save

SRC = str(Path(__file__).resolve().parent.parent / "src")
NO_DATA = "TFS has no data yet. Run tfs import or tfs start first."
TOOLS = ["describe", "query", "propose", "save"]


def make_database(folder: str) -> Path:
    path = Path(folder) / "tfs.db"
    conn = connect(path)
    migrate(conn)
    conn.execute(
        "INSERT INTO day_facts (local_date, metric, value, unit, source, coverage,"
        " formula_version) VALUES ('2025-01-01', 'steps', 1000, 'count', 'apple_health',"
        " 'full', 'v1')"
    )
    conn.close()
    return path


def plan(**part: object) -> dict:
    return {
        "plan_version": 0,
        "why": "Easy practice",
        "sources": [],
        "actions": [
            {
                "action": "add_session",
                "item": "p1",
                "date": (datetime.now(UTC) + timedelta(days=2)).date().isoformat(),
                "family": "run",
                "label": "café ✓",
                "parts": [{"part_kind": "interval", **part}],
            }
        ],
    }


@asynccontextmanager
async def tfs_mcp(folder: str):
    """Start `tfs mcp` as a subprocess and yield the SDK's streams to it."""
    env = {**os.environ, "TFS_HOME": folder, "PYTHONPATH": SRC}
    command = StdioServerParameters(command=sys.executable, args=["-m", "tfs", "mcp"], env=env)
    with Path(os.devnull).open("w") as server_log:
        async with stdio_client(command, errlog=server_log) as streams:
            yield streams


class McpServerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    async def data(self, client: Client, name: str, arguments: dict) -> dict:
        result = await client.call_tool(name, arguments)
        self.assertFalse(result.is_error, result)
        return result.structured_content

    async def refused(self, client: Client, name: str, arguments: dict) -> str:
        result = await client.call_tool(name, arguments)
        self.assertTrue(result.is_error, result)
        return result.content[0].text

    async def test_initialize_with_an_unsupported_version_gets_a_supported_one(self) -> None:
        async with tfs_mcp(self.folder) as (read, write), ClientSession(read, write) as session:
            result = await session.send_request(
                types.InitializeRequest(
                    params=types.InitializeRequestParams(
                        protocol_version="invented-unsupported-version",
                        capabilities=types.ClientCapabilities(),
                        client_info=types.Implementation(name="test", version="0"),
                    )
                ),
                types.InitializeResult,
            )
        self.assertIn(result.protocol_version, HANDSHAKE_PROTOCOL_VERSIONS)

    async def test_tools_list_names_the_four_tools_and_their_hints(self) -> None:
        async with Client(tfs_mcp(self.folder)) as client:
            tools = {tool.name: tool.annotations for tool in (await client.list_tools()).tools}
        self.assertEqual(list(tools), TOOLS)
        self.assertEqual(
            [name for name, hints in tools.items() if hints.read_only_hint],
            ["describe", "query", "propose"],
        )
        self.assertTrue(tools["save"].destructive_hint)
        self.assertTrue(tools["save"].idempotent_hint)
        self.assertEqual({hints.open_world_hint for hints in tools.values()}, {False})

    async def test_query_reads_the_views_and_no_database_is_an_error(self) -> None:
        sql = "SELECT count(*) FROM coach_v1_day_facts"
        calls = {
            "describe": {},
            "query": {"sql": sql},
            "propose": {"proposal": plan()},
            "save": {"proposal": plan(), "request_id": "s1"},
        }
        async with Client(tfs_mcp(self.folder)) as client:
            for name in TOOLS:
                with self.subTest(name=name):
                    self.assertIn(NO_DATA, await self.refused(client, name, calls[name]))
            self.assertEqual(os.listdir(self.folder), [])

            with closing(tool_access.open_reader(make_database(self.folder))) as conn:
                expected = tool_access.query(conn, sql)
            self.assertEqual(expected["rows"], [[1]])
            self.assertEqual(await self.data(client, "query", {"sql": sql}), expected)
            self.assertIn(
                "Only coach_v1 views can be read.",
                await self.refused(client, "query", {"sql": "SELECT * FROM day_facts"}),
            )
            self.assertIn("limit", await self.refused(client, "query", {"sql": sql, "limit": 5}))

    async def test_propose_refuses_a_wrong_shape_before_any_proposal_code_runs(self) -> None:
        unknown_action = plan()
        unknown_action["actions"][0]["action"] = "erase_session"
        extra_field = plan()
        extra_field["actions"][0]["send_now"] = True
        async with Client(tfs_mcp(self.folder)) as client:
            # There is no database yet, so proposal code would answer NO_DATA.
            for proposal, named in [
                (plan(reps=2.5), "reps"),
                (unknown_action, "erase_session"),
                (extra_field, "send_now"),
            ]:
                with self.subTest(named=named):
                    text = await self.refused(client, "propose", {"proposal": proposal})
                    self.assertIn(named, text)
                    self.assertNotIn(NO_DATA, text)
            make_database(self.folder)
            preview = await self.data(client, "propose", {"proposal": plan(reps=10)})
            self.assertEqual(preview["items"][0]["parts"], [{"part_kind": "interval", "reps": 10}])

    async def test_preview_save_retry_and_query(self) -> None:
        db_path = make_database(self.folder)
        proposal = plan(distance_m=6001)
        proposal["actions"][0]["start_hint"] = "07:30"

        async with Client(tfs_mcp(self.folder)) as client:

            async def rows(sql: str) -> list:
                return (await self.data(client, "query", {"sql": sql}))["rows"]

            preview = await self.data(client, "propose", {"proposal": proposal})
            self.assertEqual(preview["items"][0]["label"], "café ✓")
            self.assertEqual(preview["items"][0]["start_hint"], "07:30")
            self.assertEqual(await rows("SELECT count(*) FROM coach_v1_plan_items"), [[0]])
            for arguments in (
                {"proposal": proposal, "request_id": 1},
                {"proposal": proposal},
            ):
                await self.refused(client, "save", arguments)
            unknown = {"proposal": proposal, "request_id": "s1", "approved": True}
            self.assertIn("approved", await self.refused(client, "save", unknown))
            self.assertEqual(await rows("SELECT * FROM coach_v1_plan_state"), [[0]])

            receipt = await self.data(client, "save", {"proposal": proposal, "request_id": "s1"})
            self.assertEqual(receipt, {"request_id": "s1", "plan_version": 1})
            self.assertEqual(
                await self.data(client, "save", {"proposal": proposal, "request_id": "s1"}),
                receipt,
            )
            now = datetime.now(UTC).astimezone()
            self.assertEqual(
                save(db_path, proposal, request_id="s1", approved_by="chat", now=now), receipt
            )
            self.assertEqual(await rows("SELECT distance_km FROM coach_v1_plan_parts"), [[6.001]])
            self.assertEqual(await rows("SELECT start_hint FROM coach_v1_plan_items"), [["07:30"]])

            self.assertIn(
                "stale",
                await self.refused(client, "save", {"proposal": proposal, "request_id": "s2"}),
            )

        conn = connect(db_path)
        self.addCleanup(conn.close)
        self.assertEqual(
            [tuple(row) for row in conn.execute("SELECT approved_by, format_version FROM journal")],
            [("chat", 5)],
        )


if __name__ == "__main__":
    unittest.main()
