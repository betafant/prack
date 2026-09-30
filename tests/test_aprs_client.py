import socket
import threading

from prack import __version__
from prack.ogn.aprs import AprsClient

BEACON = "FLRDDA5BA>OGFLR,qAS,LFMX:/160829h4415.41N/00600.03E'342/049/A=005524 !W52! id0ADDA5BA -454fpm"


class FastStop(threading.Event):
    """Stop event whose waits return quickly, so the client's reconnect back-off doesn't slow the test."""

    def wait(self, timeout=None):
        return super().wait(0.05)


def test_client_logs_in_reconnects_and_delivers_lines():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    logins = []

    def serve():
        for _session in range(2):  # the server hangs up after each session: the client must reconnect
            conn, _ = server.accept()
            with conn:
                conn.sendall(b"# aprsc 2.1.14-g5e22b37\r\n")
                logins.append(conn.recv(1024).decode())
                # one line split across two TCP packets, a server comment, a second line
                conn.sendall(BEACON[:40].encode())
                conn.sendall((BEACON[40:] + "\r\n# logresp PRACK1 unverified, server GLIDERN1\r\n").encode())
                conn.sendall(f"{BEACON}\r\n".encode())

    threading.Thread(target=serve, daemon=True).start()
    stop = FastStop()
    lines = []

    def on_line(line):
        lines.append(line)
        if len(lines) == 4:
            stop.set()
            client.stop()

    client = AprsClient("127.0.0.1", port, "PRACK1", "a/47.9/5.8/45.7/10.6", on_line)
    runner = threading.Thread(target=client.run, args=(stop,), daemon=True)
    runner.start()
    runner.join(timeout=10)
    server.close()

    assert lines == [BEACON] * 4
    assert logins[0] == f"user PRACK1 pass -1 vers prack {__version__} filter a/47.9/5.8/45.7/10.6\r\n"
    assert client.status.lines == 4
    assert client.status.reconnects >= 1
