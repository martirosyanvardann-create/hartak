import socket
import socketserver
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from pageload.relay import ConnectionRelay


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(65536):
            self.request.sendall(data)


class RelayTest(unittest.TestCase):
    def setUp(self):
        # A deliberately tiny queue, like Chrome's DevTools port.
        socketserver.ThreadingTCPServer.request_queue_size = 2
        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.relay = ConnectionRelay(self.upstream.server_address[1])
        self.port = self.relay.start()

    def tearDown(self):
        self.relay.stop()
        self.upstream.shutdown()
        self.upstream.server_close()

    def roundtrip(self, n: int) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as conn:
            message = f"hello {n}".encode() * 100
            conn.sendall(message)
            received = b""
            while len(received) < len(message):
                received += conn.recv(65536)
            return received

    def test_passes_data_through_unchanged(self):
        self.assertEqual(self.roundtrip(1), b"hello 1" * 100)

    def test_many_connections_at_once(self):
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(self.roundtrip, range(50)))
        self.assertEqual(results, [f"hello {n}".encode() * 100 for n in range(50)])

    def test_stop_is_idempotent(self):
        self.relay.stop()
        self.relay.stop()


if __name__ == "__main__":
    unittest.main()
