"""
Fake MPV process for Watch Together integration testing.

This script runs as an independent asyncio server that mimics the MPV JSON‑IPC protocol.
It maintains shared playback state across all connections to the same socket.

Usage:
    python fake_mpv.py <socket_path>
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# Global state for the fake MPV instance
class FakeMPV:
    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self.server: Optional[asyncio.Server] = None
        self.clients = 0
        self.lock = asyncio.Lock()
        self.state = {
            "time_pos": 0.0,
            "duration": 120.0,
            "paused": False,
            "speed": 1.0,
            "start_time": time.monotonic(),
            "target_position": 0.0,
        }

    async def start(self):
        """Start the fake MPV server."""
        self.server = await asyncio.start_unix_server(
            self.handle_client,
            path=self.sock_path,
            limit=65536,
        )
        print(f"[Fake MPV] listening on {self.sock_path}", file=sys.stderr)

    async def stop(self):
        """Stop the server cleanly."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def handle_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter):
        """Handle a single IPC connection."""
        self.clients += 1
        request_id = 0

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.decode(errors="ignore").strip()
                if not line:
                    continue

                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    error = {"request_id": None, "error": "Invalid JSON"}
                    writer.write((json.dumps(error) + "\n").encode())
                    await writer.drain()
                    continue

                cmd = req.get("command", [])
                rid = req.get("request_id")
                if rid is None:
                    rid = request_id
                    request_id += 1

                # Process commands
                if cmd[0] == "get_property":
                    prop = cmd[1]
                    async with self.lock:
                        if prop == "time-pos":
                            data = self.state["time_pos"]
                        elif prop == "duration":
                            data = self.state["duration"]
                        elif prop == "pause":
                            data = self.state["paused"]
                        elif prop == "speed":
                            data = self.state["speed"]
                        else:
                            data = None
                    resp = {"request_id": rid, "data": data}

                elif cmd[0] == "set_property":
                    prop = cmd[1]
                    val = cmd[2]
                    async with self.lock:
                        if prop == "time-pos":
                            self.state["target_position"] = max(
                                0.0, min(val, self.state["duration"])
                            )
                            self.state["time_pos"] = self.state["target_position"]
                            self.state["start_time"] = time.monotonic()
                        elif prop == "pause":
                            self.state["paused"] = bool(val)
                            if not self.state["paused"]:
                                self.state["start_time"] = time.monotonic()
                        elif prop == "speed":
                            self.state["speed"] = max(0.5, min(val, 2.0))
                    resp = {"request_id": rid}

                elif cmd[0] == "play":
                    async with self.lock:
                        self.state["paused"] = False
                        self.state["start_time"] = time.monotonic()
                    resp = {"request_id": rid}

                elif cmd[0] == "pause":
                    async with self.lock:
                        self.state["paused"] = True
                    resp = {"request_id": rid}

                elif cmd[0] == "seek":
                    target = cmd[1] if len(cmd) > 1 else 0.0
                    async with self.lock:
                        self.state["target_position"] = max(
                            0.0, min(target, self.state["duration"])
                        )
                        self.state["time_pos"] = self.state["target_position"]
                        self.state["start_time"] = time.monotonic()
                    resp = {"request_id": rid}

                elif cmd[0] == "quit":
                    break
                else:
                    resp = {"request_id": rid, "error": "Unknown command"}

                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
                # Schedule position update
                asyncio.create_task(self._tick())

        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients -= 1
            writer.close()
            await writer.wait_closed()

    async def _tick(self):
        """Update playback position based on elapsed time (10 Hz)."""
        await asyncio.sleep(0.1)
        async with self.lock:
            if self.state["paused"]:
                return
            elapsed = time.monotonic() - self.state["start_time"]
            new_pos = self.state["target_position"] + elapsed * self.state["speed"]
            if new_pos >= self.state["duration"]:
                self.state["time_pos"] = self.state["duration"]
                self.state["paused"] = True
            else:
                self.state["time_pos"] = new_pos

    async def run(self):
        """Start the fake MPV server and keep it alive."""
        await self.start()
        try:
            await asyncio.Future()  # block forever
        except asyncio.CancelledError:
            await self.stop()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <socket_path>")
        sys.exit(1)

    sock_path = sys.argv[1]
    # Remove existing socket if present
    Path(sock_path).unlink(missing_ok=True)

    fake_mpv = FakeMPV(sock_path)
    try:
        asyncio.run(fake_mpv.run())
    finally:
        Path(sock_path).unlink(missing_ok=True)