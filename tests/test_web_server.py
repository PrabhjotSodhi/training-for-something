import http.client
import json
import socket
import struct
import threading
import unittest

from tfs import config
from tfs.web.server import make_server

# Client side only. A broken server fails a test instead of hanging it.
SAFETY_TIMEOUT_S = 10


def half_push(host: str = "localhost") -> bytes:
    """Build a push to /push/x that promises 5 body bytes and sends 2."""
    return f"POST /push/x HTTP/1.1\r\nHost: {host}\r\nContent-Length: 5\r\n\r\nhe".encode()


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.started = threading.Event()

    def recording_handler(self, name, body, headers):
        self.started.set()
        self.calls.append((name, body.read()))
        return 200, {"ok": True}

    def start(self, push_handler=None, **options) -> None:
        self.server = make_server(
            config.SCREEN_HOST, 0, push_handler=push_handler or self.recording_handler, **options
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    def request(self, method: str, path: str, body=None, headers=None):
        """Send one request. Return the status and the body."""
        host, port = self.server.sock.getsockname()
        client = http.client.HTTPConnection(host, port, timeout=SAFETY_TIMEOUT_S)
        self.addCleanup(client.close)
        client.request(method, path, body, headers or {})
        response = client.getresponse()
        return response.status, response.read()

    def connect(self) -> socket.socket:
        sock = socket.create_connection(self.server.sock.getsockname(), timeout=SAFETY_TIMEOUT_S)
        self.addCleanup(sock.close)
        return sock

    def status_after_close(self, sock: socket.socket) -> int:
        """Read until the server closes the connection. Return the status, or 0 if none came."""
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
        return int(data.split(b" ", 2)[1]) if data else 0


class RoutesTest(ServerTestCase):
    def test_ping_answers_tfs(self):
        # GIVEN a running server
        self.start()
        # WHEN a client asks for /ping
        reply = self.request("GET", "/ping")
        # THEN it answers 200 tfs
        self.assertEqual(reply, (200, b"tfs"))

    def test_other_paths_are_not_found(self):
        # GIVEN a running server
        self.start()
        # WHEN a client asks for a path that is not /ping or /push/<name>
        paths = ("GET", "/"), ("GET", "/api/overview"), ("GET", "/docs"), ("GET", "/openapi.json")
        pushes = ("POST", "/other"), ("POST", "/push/"), ("POST", "/push/a/b")
        # THEN every one gets 404 and no push reaches the handler
        for method, path in (*paths, *pushes):
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path)[0], 404)
        self.assertEqual(self.calls, [])

    def test_a_push_reaches_the_handler_with_its_name_body_and_headers(self):
        # GIVEN a handler that records what it gets
        seen = []

        def handler(name, body, headers):
            seen.append((name, body.read(), headers["Authorization"]))
            return 200, {"ok": True}

        self.start(push_handler=handler)
        # WHEN a client posts a body with a token
        status, body = self.request("POST", "/push/x", b"hello", {"Authorization": "Bearer t"})
        # THEN the handler gets all three and its payload comes back
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(seen, [("x", b"hello", "Bearer t")])

    def test_the_handler_status_and_payload_are_sent_back(self):
        # GIVEN a handler that refuses
        self.start(push_handler=lambda name, body, headers: (401, {"ok": False, "error": "no"}))
        # WHEN a client pushes
        status, body = self.request("POST", "/push/x", b"")
        # THEN the client gets that status and payload
        self.assertEqual((status, json.loads(body)), (401, {"ok": False, "error": "no"}))

    def test_an_empty_body_and_a_body_of_exactly_max_body_are_allowed(self):
        # GIVEN a server with a 5 byte limit
        self.start(max_body=5)
        # WHEN a client pushes 0 bytes, then 5 bytes
        statuses = [self.request("POST", "/push/x", body)[0] for body in (b"", b"hello")]
        # THEN both get 200
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(self.calls, [("x", b""), ("x", b"hello")])

    def test_handler_errors_and_their_causes_never_reach_the_response_or_the_logs(self):
        # GIVEN a handler that fails with a token in its message and in its cause
        token, cause_token = "tok-invented-7f3a", "tok-invented-c91d"

        def handler(name, body, headers):
            raise RuntimeError(f"failed with {token}") from ValueError(f"caused by {cause_token}")

        self.start(push_handler=handler)
        # WHEN a client pushes
        with self.assertLogs(level="DEBUG") as logs:
            status, body = self.request("POST", "/push/x", b"")
            self.assertEqual(self.request("GET", "/ping"), (200, b"tfs"))
        # THEN it gets 500, neither token appears anywhere, and nothing logs a traceback
        self.assertEqual(status, 500)
        for secret in (token, cause_token):
            with self.subTest(secret=secret):
                self.assertNotIn(secret.encode(), body)
                self.assertNotIn(secret, "\n".join(logs.output))
        self.assertEqual([record.exc_info for record in logs.records], [None] * len(logs.records))

    def test_a_handler_that_returns_the_wrong_shape_gives_500(self):
        # GIVEN a handler that returns things that are not (status, payload)
        results = [None, (200, {"ok": object()}), (999, {}), ("200", {})]
        self.start(push_handler=lambda name, body, headers: results.pop(0))
        # WHEN a client pushes four times
        for _ in range(4):
            # THEN each gets 500 and the server keeps answering
            with self.assertLogs("tfs.web.server", level="ERROR"):
                self.assertEqual(self.request("POST", "/push/x", b"")[0], 500)
        self.assertEqual(self.request("GET", "/ping")[0], 200)

    def test_a_request_for_another_host_gets_421_and_reaches_nothing(self):
        # GIVEN a running server
        self.start()
        # WHEN a client sends a Host that is not this computer, or its Tailscale or local name
        for method, path in (("GET", "/ping"), ("POST", "/push/x")):
            with self.subTest(path=path):
                # THEN it gets 421
                self.assertEqual(self.request(method, path, headers={"Host": "evil.test"})[0], 421)
        # AND a Tailscale name and a local name are answered
        for host in ("my-pc.tail1234.ts.net", f"{socket.gethostname()}.local:47823"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/ping", headers={"Host": host})[0], 200)
        self.assertEqual(self.calls, [])

    def test_the_home_wifi_server_answers_pushes_only(self):
        # GIVEN a server for the home Wi-Fi door
        self.start(push_only=True)
        # WHEN a client asks for a screen, an API route or /ping
        for path in ("/", "/api/overview", "/ping", "/docs"):
            # THEN it gets 404
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        # AND a push still works
        self.assertEqual(self.request("POST", "/push/x", b"hi")[0], 200)


class SizeLimitTest(ServerTestCase):
    def test_a_declared_length_over_the_limit_gets_413_before_the_body_is_read(self):
        # GIVEN a server with a 10 byte limit
        self.start(max_body=10)
        # WHEN a client declares 11 bytes
        status, body = self.request("POST", "/push/x", b"x" * 11)
        # THEN it gets 413 with a message that names the limit, and nothing is read
        self.assertEqual(status, 413)
        self.assertIn("10", json.loads(body)["error"])
        self.assertEqual(self.calls, [])

    def test_a_streamed_body_over_the_limit_gets_413(self):
        # GIVEN a server with a 10 byte limit
        self.start(max_body=10)
        # WHEN a client sends 11 bytes with no Content-Length
        status, _ = self.request("POST", "/push/x", iter([b"hello", b"world!"]))
        # THEN it gets 413 and the handler never finishes a read
        self.assertEqual(status, 413)
        self.assertEqual(self.calls, [])


class StalledClientTest(ServerTestCase):
    def test_a_body_that_stops_early_gets_400_after_the_read_timeout(self):
        # GIVEN a server with a short read timeout
        self.start(read_timeout_s=0.2)
        # WHEN a client sends half its body and stops
        sock = self.connect()
        sock.sendall(half_push())
        # THEN the read times out, the client gets 400 and the handler gets no body
        self.assertEqual(self.status_after_close(sock), 400)
        self.assertEqual(self.calls, [])

    def test_stop_waits_for_a_request_in_progress(self):
        # GIVEN a request stuck in the middle of its body
        self.start(read_timeout_s=0.2)
        sock = self.connect()
        sock.sendall(half_push())
        self.assertTrue(self.started.wait(SAFETY_TIMEOUT_S))
        # WHEN the server stops
        stopping = threading.Thread(target=self.server.stop)
        stopping.start()
        stopping.join(SAFETY_TIMEOUT_S)
        # THEN stop returns once the request ends, and the handler got no body
        self.assertFalse(stopping.is_alive())
        self.assertEqual(self.calls, [])

    def test_a_stalled_body_does_not_block_other_requests(self):
        # GIVEN a client stuck in the middle of its body
        self.start(read_timeout_s=SAFETY_TIMEOUT_S * 3)
        self.connect().sendall(half_push())
        self.assertTrue(self.started.wait(SAFETY_TIMEOUT_S))
        # WHEN another client asks for /ping
        # THEN it is answered
        self.assertEqual(self.request("GET", "/ping"), (200, b"tfs"))

    def test_a_client_that_resets_the_connection_mid_body_does_not_crash_the_server(self):
        # GIVEN a running server
        self.start()
        # WHEN a client sends half its body and resets the connection
        sock = self.connect()
        sock.sendall(half_push())
        self.assertTrue(self.started.wait(SAFETY_TIMEOUT_S))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
        # THEN the server keeps answering and the handler got no body
        self.assertEqual(self.request("GET", "/ping"), (200, b"tfs"))
        self.assertEqual(self.calls, [])


class PortTest(ServerTestCase):
    def test_a_second_server_cannot_take_the_same_port(self):
        # GIVEN a running server
        self.start()
        port = self.server.sock.getsockname()[1]
        # WHEN a second server binds the same port
        # THEN it fails
        with self.assertRaises(OSError):
            make_server(config.SCREEN_HOST, port, push_handler=self.recording_handler)

    def test_a_new_server_can_take_the_port_of_one_that_just_served_a_request(self):
        # GIVEN a server that has answered a request
        self.start()
        port = self.server.sock.getsockname()[1]
        self.request("GET", "/ping")
        # WHEN it stops
        self.server.stop()
        # THEN a new server binds the port
        restarted = make_server(config.SCREEN_HOST, port, push_handler=self.recording_handler)
        self.addCleanup(restarted.stop)
        self.assertEqual(restarted.sock.getsockname()[1], port)

    def test_the_server_listens_only_on_the_host_it_was_given(self):
        # GIVEN a server made for the screen host
        self.start()
        # THEN it listens on that host only
        self.assertEqual(self.server.sock.getsockname()[0], config.SCREEN_HOST)


if __name__ == "__main__":
    unittest.main()
