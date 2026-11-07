import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import tfs
from tfs.mcp.server import NO_DATA, serve
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

SRC = str(Path(__file__).resolve().parent.parent / "src")


def request(id, method, params=None):
    message = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


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


class McpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.db_path = Path(folder.name) / "tfs.db"

    def talk(self, *lines: str) -> list[dict]:
        stdout = io.StringIO()
        serve(io.StringIO("\n".join(lines) + "\n"), stdout, self.db_path)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        params = {"name": name, "arguments": arguments or {}}
        return self.talk(request(1, "tools/call", params))[0]["result"]

    def test_initialize_echoes_the_clients_protocol_version(self) -> None:
        [reply] = self.talk(request(7, "initialize", {"protocolVersion": "2024-11-05"}))
        self.assertEqual(
            reply,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "tfs", "version": tfs.__version__},
                },
            },
        )

    def test_tools_list_describes_read_and_proposal_tools(self) -> None:
        [reply] = self.talk(request(1, "tools/list"))
        describe, query, propose, save, undo = reply["result"]["tools"]
        self.assertEqual([propose["name"], save["name"], undo["name"]], ["propose", "save", "undo"])
        self.assertTrue(propose["annotations"]["readOnlyHint"])
        for tool in (save, undo):
            self.assertFalse(tool["annotations"]["readOnlyHint"])
            self.assertTrue(tool["annotations"]["destructiveHint"])
            self.assertTrue(tool["annotations"]["idempotentHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])
        self.assertEqual(describe["name"], "describe")
        self.assertEqual(describe["inputSchema"], {"type": "object", "properties": {}})
        self.assertEqual(query["name"], "query")
        self.assertEqual(
            query["inputSchema"],
            {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        )
        for phrase in ("one read-only SELECT", "coach_v1 views", "describe first", "never"):
            self.assertIn(phrase, query["description"])

    def test_tool_results_come_back_as_json_text(self) -> None:
        make_database(self.folder)
        result = self.call("query", {"sql": "SELECT metric, value FROM coach_v1_day_facts"})
        self.assertFalse(result["isError"])
        [item] = result["content"]
        self.assertEqual(item["type"], "text")
        self.assertEqual(
            json.loads(item["text"]),
            {"columns": ["metric", "value"], "rows": [["steps", 1000.0]], "clipped": None},
        )
        described = json.loads(self.call("describe")["content"][0]["text"])
        self.assertEqual(
            described["coach_v1_day_facts"],
            {
                "meaning": (
                    "One row per day and metric: the day's value, unit and source."
                    " Coverage is full, partial or importing."
                ),
                "columns": ["local_date", "metric", "value", "unit", "source", "coverage"],
            },
        )

    def test_a_reader_error_sets_is_error(self) -> None:
        make_database(self.folder)
        result = self.call("query", {"sql": "SELECT * FROM day_facts"})
        self.assertTrue(result["isError"])
        self.assertEqual(
            json.loads(result["content"][0]["text"]),
            {"error": "Only coach_v1 views can be read."},
        )

    def test_an_exception_in_the_reader_becomes_an_error_result(self) -> None:
        make_database(self.folder)
        replies = self.talk(
            request(1, "tools/call", {"name": "query", "arguments": {"sql": "SELECT '\ud800'"}}),
            request(2, "ping"),
        )
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertEqual(replies[1]["result"], {})

    def test_a_message_without_an_id_gets_no_reply(self) -> None:
        self.assertEqual(
            self.talk(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})),
            [],
        )

    def test_ping_and_errors_keep_the_server_running(self) -> None:
        replies = self.talk(
            request(1, "ping"),
            request(2, "nope"),
            request(3, "tools/call", {"name": "drop", "arguments": {}}),
            request(4, "tools/call", {"name": "query", "arguments": {"sql": 5}}),
            "not json",
            "[1]",
            request(5, "ping"),
        )
        codes = [reply.get("error", {}).get("code") for reply in replies]
        self.assertEqual(codes, [None, -32601, -32602, -32602, -32700, -32600, None])
        self.assertEqual(replies[0]["result"], {})
        self.assertEqual([reply["id"] for reply in replies[4:6]], [None, None])
        self.assertEqual(replies[6], {"jsonrpc": "2.0", "id": 5, "result": {}})

    def test_without_a_database_a_tool_call_says_so_and_creates_nothing(self) -> None:
        result = self.call("describe")
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), {"error": NO_DATA})
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_database_made_after_start_is_found(self) -> None:
        self.assertTrue(self.call("describe")["isError"])
        make_database(self.folder)
        self.assertFalse(self.call("describe")["isError"])

    def test_the_command_answers_over_a_pipe(self) -> None:
        make_database(self.folder)
        lines = [
            request(1, "initialize", {"protocolVersion": "2025-06-18"}),
            request(2, "tools/list"),
            request(
                3, "tools/call", {"name": "query", "arguments": {"sql": "SELECT 'café ✓' AS x"}}
            ),
        ]
        env = {**os.environ, "TFS_HOME": self.folder, "PYTHONPATH": SRC, "PYTHONUTF8": "1"}
        done = subprocess.run(
            [sys.executable, "-m", "tfs", "mcp"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "tfs")
        self.assertEqual(
            [tool["name"] for tool in replies[1]["result"]["tools"]],
            ["describe", "query", "propose", "save", "undo"],
        )
        rows = json.loads(replies[2]["result"]["content"][0]["text"])["rows"]
        self.assertEqual(rows, [["café ✓"]])

    def plan(self) -> dict:
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
                    "parts": [{"part_kind": "interval", "distance_m": 6001}],
                }
            ],
        }

    def tool_data(self, name: str, arguments: dict) -> dict:
        result = self.call(name, arguments)
        self.assertFalse(result["isError"], result)
        return json.loads(result["content"][0]["text"])

    def test_preview_save_retry_query_and_undo(self) -> None:
        make_database(self.folder)
        proposal = self.plan()
        preview = self.tool_data("propose", {"proposal": proposal})
        self.assertEqual(preview["items"][0]["label"], "café ✓")
        self.assertEqual(
            self.tool_data("query", {"sql": "SELECT count(*) FROM coach_v1_plan_items"})["rows"],
            [[0]],
        )
        receipt = self.tool_data("save", {"proposal": proposal, "request_id": "s1"})
        self.assertEqual(receipt, {"request_id": "s1", "plan_version": 1})
        self.assertEqual(
            self.tool_data("save", {"proposal": proposal, "request_id": "s1"}), receipt
        )
        self.assertEqual(
            self.tool_data("query", {"sql": "SELECT distance_km FROM coach_v1_plan_parts"})["rows"],
            [[6.001]],
        )
        stale = self.call("save", {"proposal": proposal, "request_id": "s2"})
        self.assertTrue(stale["isError"])
        self.assertIn("stale", stale["content"][0]["text"])
        self.assertEqual(
            self.tool_data("undo", {"target_version": 1, "request_id": "u1"}),
            {"request_id": "u1", "plan_version": 2},
        )
        self.assertEqual(
            self.tool_data("query", {"sql": "SELECT status FROM coach_v1_plan_items"})["rows"],
            [["removed"]],
        )
        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(
            [tuple(row) for row in conn.execute("SELECT approved_by, format_version FROM journal")],
            [("chat", 2), ("chat", 2)],
        )

    def test_write_notifications_and_malformed_arguments_do_not_save(self) -> None:
        make_database(self.folder)
        proposal = self.plan()
        notification = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "save", "arguments": {"proposal": proposal, "request_id": "n1"}},
            }
        )
        self.assertEqual(self.talk(notification), [])
        for name, arguments in [
            ("save", {"proposal": proposal, "request_id": "s1", "approved": True}),
            ("save", {"proposal": proposal}),
            ("propose", {"proposal": []}),
            ("undo", {"target_version": 0}),
        ]:
            with self.subTest(name=name, arguments=arguments):
                reply = self.talk(request(1, "tools/call", {"name": name, "arguments": arguments}))[
                    0
                ]
                self.assertEqual(reply["error"]["code"], -32602)
        self.assertTrue(self.call("save", {"proposal": proposal, "request_id": 1})["isError"])
        self.assertTrue(self.call("undo", {"target_version": True, "request_id": "u1"})["isError"])
        self.assertEqual(
            self.tool_data("query", {"sql": "SELECT * FROM coach_v1_plan_state"})["rows"], [[0]]
        )


if __name__ == "__main__":
    unittest.main()
