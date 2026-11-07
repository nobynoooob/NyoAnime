import json
import os
import socket
import threading
import time

import pytest

from nyoanime.player import PlayerManager

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Unix domain sockets only"
)


class _Server:
    def __init__(self, sock_path, reply=None, delay=0.0):
        self.sock_path = str(sock_path)
        self.reply = reply
        self.delay = delay
        self.received = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.sock_path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            self.received.append(json.loads(data.decode("utf-8")))
            if self.delay:
                time.sleep(self.delay)
            if self.reply is not None:
                try:
                    conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)


def test_send_mpv_command_writes_without_waiting_for_a_reply(tmp_path):
    server = _Server(tmp_path / "mpv.sock")
    try:
        result = PlayerManager.send_mpv_command(
            server.sock_path, ["set_property", "time-pos", 120.0]
        )

        assert result is None
        server._thread.join(timeout=1.0)
        assert server.received == [
            {"command": ["set_property", "time-pos", 120.0], "request_id": None}
        ]
    finally:
        server.close()


def test_send_mpv_command_returns_the_matching_reply(tmp_path):
    reply = b'{"request_id": 7, "error": "success", "data": null}\n'
    server = _Server(tmp_path / "mpv.sock", reply=reply)
    try:
        result = PlayerManager.send_mpv_command(
            server.sock_path,
            ["get_property", "pause"],
            request_id=7,
        )

        assert result is not None
        assert result["request_id"] == 7
        assert result["error"] == "success"
    finally:
        server.close()


def test_send_mpv_command_gives_up_on_a_slow_player(tmp_path):
    server = _Server(
        tmp_path / "mpv.sock",
        reply=b'{"request_id": 9, "error": "success"}\n',
        delay=0.8,
    )
    try:
        started = time.monotonic()
        result = PlayerManager.send_mpv_command(
            server.sock_path,
            ["get_property", "time-pos"],
            request_id=9,
            timeout=0.25,
        )
        elapsed = time.monotonic() - started

        assert result is None
        assert elapsed < 0.7
    finally:
        server.close()


def test_send_mpv_command_raises_when_the_socket_is_unreachable(tmp_path):
    with pytest.raises(RuntimeError):
        PlayerManager.send_mpv_command(
            str(tmp_path / "missing.sock"),
            ["set_property", "pause", True],
        )
