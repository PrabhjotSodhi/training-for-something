import json
import socket
import struct
import threading
import unittest
from http.server import ThreadingHTTPServer

from tfs import config
from tfs.web.server import IncompleteBodyError, make_server

# Client side only. A broken server fails a test instead of hanging it.
SAFETY_TIMEOUT_S = 10


def raw(first_line: str, *header_lines: str, body: bytes = b"") -> bytes:
    """Build the bytes of one request."""
    lines = [first_line, "Host: tfs.test", *header_lines]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def push(*header_lines: str, body: bytes = b"") -> bytes:
    """Build a push request to /push/x."""
    return raw("POST /push/x HTTP/1.1", *header_lines, body=body)


def read_response(sock: socket.socket) -> tuple[int, bytes]:
    """Read until the server closes the connection. Return the status and body."""
    data = b""
    while chunk := sock.recv(65536):
        data += chunk
    if not data:
        return 0, b""
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


def reset(sock: socket.socket) -> None:
    """Close with a reset instead of a polite goodbye."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.incomplete: list[str] = []
        self.unhandled: list[object] = []
        self.started = threading.Event()
        self.server: ThreadingHTTPServer | None = None

    def recording_handler(self, name, body, headers):
        self.started.set()
        try:
            data = body.read()
        except IncompleteBodyError:
            self.incomplete.append(name)
            raise
        self.calls.append((name, data))
        return 200, {"ok": True}

    def start(self, push_handler=None, **options) -> None:
        server = make_server(
            config.SCREEN_HOST,
            0,
            push_handler=push_handler or self.recording_handler,
            **options,
        )
        server.handle_error = lambda request, client_address: self.unhandled.append(client_address)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.server = server
        self.addCleanup(self.stop, server, thread)

    def stop(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join()

    def connect(self) -> socket.socket:
        assert self.server is not None
        sock = socket.create_connection(self.server.server_address, timeout=SAFETY_TIMEOUT_S)
        self.addCleanup(sock.close)
        return sock

    def ask(self, request: bytes) -> tuple[int, bytes]:
        sock = self.connect()
        sock.sendall(request)
        return read_response(sock)

    def check_ping(self) -> None:
        self.assertEqual(self.ask(raw("GET /ping HTTP/1.1")), (200, b"tfs"))

    def finish(self) -> None:
        """Wait for every request in progress to end."""
        assert self.server is not None
        self.server.shutdown()
        self.server.server_close()


class RoutesTest(ServerTestCase):
    def test_make_server_returns_a_threading_http_server(self):
        self.start()
        self.assertIsInstance(self.server, ThreadingHTTPServer)

    def test_ping_answers_tfs(self):
        self.start()
        self.assertEqual(self.ask(raw("GET /ping HTTP/1.1")), (200, b"tfs"))

    def test_other_paths_are_not_found(self):
        self.start()
        for request in (
            raw("GET /other HTTP/1.1"),
            raw("GET /ping/ HTTP/1.1"),
            raw("GET /push/x HTTP/1.1"),
            raw("POST /ping HTTP/1.1", "Content-Length: 0"),
            raw("POST /other HTTP/1.1", "Content-Length: 0"),
            raw("POST /push/ HTTP/1.1", "Content-Length: 0"),
            raw("POST /push/a/b HTTP/1.1", "Content-Length: 0"),
            raw("POST /push/.. HTTP/1.1", "Content-Length: 0"),
            raw("POST /push/%2e%2e HTTP/1.1", "Content-Length: 0"),
        ):
            with self.subTest(request=request):
                self.assertEqual(self.ask(request)[0], 404)
        self.finish()
        self.assertEqual(self.calls, [])

    def test_other_methods_are_refused(self):
        self.start()
        self.assertEqual(self.ask(raw("PUT /push/x HTTP/1.1", "Content-Length: 0"))[0], 501)
        self.assertEqual(self.ask(raw("DELETE /ping HTTP/1.1"))[0], 501)
        self.finish()
        self.assertEqual(self.calls, [])

    def test_a_push_reaches_the_handler_with_its_name_and_body(self):
        self.start()
        status, body = self.ask(push("Content-Length: 5", body=b"hello"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.finish()
        self.assertEqual(self.calls, [("x", b"hello")])

    def test_handler_gets_the_request_headers(self):
        seen = []

        def handler(name, body, headers):
            seen.append((headers["Authorization"], headers["Content-Length"]))
            body.read()
            return 200, {"ok": True}

        self.start(push_handler=handler)
        self.ask(push("Authorization: Bearer test-token", "Content-Length: 2", body=b"hi"))
        self.finish()
        self.assertEqual(seen, [("Bearer test-token", "2")])

    def test_handler_status_and_payload_are_sent_back(self):
        def handler(name, body, headers):
            return 401, {"ok": False, "error": "no"}

        self.start(push_handler=handler)
        status, body = self.ask(push("Content-Length: 0"))
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "no"})

    def test_body_yields_exactly_content_length_bytes(self):
        seen = []

        def handler(name, body, headers):
            seen.append([body.read(2), body.read(), body.read(), body.read(1)])
            return 200, {"ok": True}

        self.start(push_handler=handler)
        self.ask(push("Content-Length: 5", body=b"hello" + b"EXTRA"))
        self.finish()
        self.assertEqual(seen, [[b"he", b"llo", b"", b""]])

    def test_an_empty_body_is_allowed(self):
        self.start()
        self.assertEqual(self.ask(push("Content-Length: 0"))[0], 200)
        self.finish()
        self.assertEqual(self.calls, [("x", b"")])

    def test_leading_zeros_and_repeated_equal_lengths_are_one_length(self):
        self.start()
        self.assertEqual(self.ask(push("Content-Length: 005", body=b"hello"))[0], 200)
        request = push("Content-Length: 5", "Content-Length: 5", body=b"hello")
        self.assertEqual(self.ask(request)[0], 200)
        self.finish()
        self.assertEqual(self.calls, [("x", b"hello"), ("x", b"hello")])

    def test_a_body_of_exactly_max_body_is_allowed(self):
        self.start(max_body=5)
        self.assertEqual(self.ask(push("Content-Length: 5", body=b"hello"))[0], 200)

    def test_handler_failure_gives_500_and_the_server_keeps_serving(self):
        def handler(name, body, headers):
            raise RuntimeError("secret-detail")

        self.start(push_handler=handler)
        with self.assertLogs("tfs.web.server", level="ERROR"):
            status, body = self.ask(push("Content-Length: 0"))
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret-detail", body)
        self.check_ping()

    def test_a_handler_that_returns_the_wrong_shape_gives_500(self):
        def handler(name, body, headers):
            return 200, {"ok": object()}

        self.start(push_handler=handler)
        with self.assertLogs("tfs.web.server", level="ERROR"):
            self.assertEqual(self.ask(push("Content-Length: 0"))[0], 500)
        self.check_ping()


class FramingTest(ServerTestCase):
    def refused(self, request: bytes, expected_status: int) -> None:
        status, _ = self.ask(request)
        self.assertEqual(status, expected_status)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.incomplete, [])

    def test_chunked_gets_411(self):
        self.start()
        self.refused(push("Transfer-Encoding: chunked"), 411)

    def test_chunked_with_a_length_gets_411(self):
        self.start()
        self.refused(push("Transfer-Encoding: chunked", "Content-Length: 5"), 411)

    def test_any_transfer_encoding_gets_411(self):
        self.start()
        self.refused(push("Transfer-Encoding: identity", "Content-Length: 5"), 411)

    def test_missing_length_gets_411(self):
        self.start()
        self.refused(push(), 411)

    def test_bad_lengths_get_400(self):
        self.start()
        for value in ("-1", "abc", "", "+5", "5.0", "0x5", "5 5", "5,5", "\xb2", "1e3"):
            with self.subTest(value=value):
                self.refused(push(f"Content-Length: {value}"), 400)

    def test_two_different_lengths_get_400(self):
        self.start()
        self.refused(push("Content-Length: 5", "Content-Length: 6"), 400)

    def test_a_length_above_max_body_gets_413_before_the_body_is_read(self):
        self.start(max_body=10)
        self.refused(push("Content-Length: 11"), 413)

    def test_huge_lengths_get_413(self):
        self.start(max_body=10)
        for value in ("9" * 19, "9" * 30, "9" * 5000, "0" * 5000 + "11"):
            with self.subTest(digits=len(value)):
                self.refused(push(f"Content-Length: {value}"), 413)

    def test_a_refusal_is_a_json_message_the_user_can_act_on(self):
        self.start(max_body=10)
        status, body = self.ask(push("Content-Length: 11"))
        message = json.loads(body)
        self.assertEqual(status, 413)
        self.assertIs(message["ok"], False)
        self.assertIn("10", message["error"])


class StalledClientTest(ServerTestCase):
    def test_a_body_that_stops_early_is_closed_after_the_read_timeout(self):
        self.start(read_timeout_s=0.2)
        sock = self.connect()
        sock.sendall(push("Content-Length: 5", body=b"he"))
        status, _ = read_response(sock)
        self.assertEqual(status, 400)
        self.assertEqual(self.incomplete, ["x"])
        self.assertEqual(self.calls, [])

    def test_a_silent_client_is_closed_after_the_read_timeout(self):
        self.start(read_timeout_s=0.2)
        sock = self.connect()
        sock.sendall(b"POST /push/x HTTP/1.1\r\nContent-Le")
        self.assertEqual(read_response(sock), (0, b""))
        self.finish()
        self.assertEqual(self.calls, [])

    def test_a_connection_that_sends_nothing_is_closed_after_the_read_timeout(self):
        self.start(read_timeout_s=0.2)
        self.assertEqual(read_response(self.connect()), (0, b""))

    def test_a_client_that_closes_its_sending_side_early_gets_400(self):
        self.start()
        sock = self.connect()
        sock.sendall(push("Content-Length: 5", body=b"he"))
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(read_response(sock)[0], 400)
        self.assertEqual(self.calls, [])

    def test_shutdown_and_server_close_wait_for_a_request_in_progress(self):
        self.start(read_timeout_s=0.2)
        sock = self.connect()
        sock.sendall(push("Content-Length: 5", body=b"he"))
        self.assertTrue(self.started.wait(SAFETY_TIMEOUT_S))
        self.finish()
        self.assertEqual(self.incomplete, ["x"])
        self.assertEqual(self.calls, [])


class ManyClientsTest(ServerTestCase):
    def test_a_silent_client_does_not_block_others(self):
        self.start(read_timeout_s=SAFETY_TIMEOUT_S * 3)
        silent = self.connect()
        silent.sendall(b"GET /ping HTT")
        self.check_ping()
        self.assertEqual(self.ask(push("Content-Length: 5", body=b"hello"))[0], 200)
        silent.close()

    def test_a_stalled_body_does_not_block_others(self):
        self.start(read_timeout_s=SAFETY_TIMEOUT_S * 3)
        stalled = self.connect()
        stalled.sendall(push("Content-Length: 5", body=b"he"))
        self.assertTrue(self.started.wait(SAFETY_TIMEOUT_S))
        self.check_ping()
        stalled.close()

    def test_several_pushes_run_their_handlers_at_the_same_time(self):
        clients = 4
        barrier = threading.Barrier(clients, timeout=SAFETY_TIMEOUT_S)

        def handler(name, body, headers):
            barrier.wait()
            body.read()
            return 200, {"ok": True}

        self.start(push_handler=handler)
        statuses = []

        def post() -> None:
            statuses.append(self.ask(push("Content-Length: 5", body=b"hello"))[0])

        threads = [threading.Thread(target=post) for _ in range(clients)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(statuses, [200] * clients)


class DisconnectTest(ServerTestCase):
    def test_a_client_that_resets_the_connection_does_not_crash_the_server(self):
        cases = {
            "half a request line": b"POST /push",
            "half the headers": b"POST /push/x HTTP/1.1\r\nContent-Le",
            "half the body": push("Content-Length: 5", body=b"he"),
            "a whole request, no wait for the answer": push("Content-Length: 5", body=b"hello"),
        }
        self.start()
        for name, request in cases.items():
            with self.subTest(name):
                sock = self.connect()
                sock.sendall(request)
                reset(sock)
                self.check_ping()
        self.finish()
        self.assertEqual(self.unhandled, [])


class SetupTest(ServerTestCase):
    def test_a_second_server_cannot_take_the_same_port(self):
        self.start()
        port = self.server.server_address[1]
        with self.assertRaises(OSError):
            make_server(config.SCREEN_HOST, port, push_handler=self.recording_handler)

    def test_the_server_listens_only_on_the_host_it_was_given(self):
        self.start()
        self.assertEqual(self.server.server_address[0], config.SCREEN_HOST)

    def test_read_timeout_must_be_above_zero(self):
        for value in (0, -1, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                make_server(
                    config.SCREEN_HOST,
                    0,
                    push_handler=self.recording_handler,
                    read_timeout_s=value,
                )

    def test_max_body_cannot_be_negative(self):
        with self.assertRaises(ValueError):
            make_server(config.SCREEN_HOST, 0, push_handler=self.recording_handler, max_body=-1)


if __name__ == "__main__":
    unittest.main()
