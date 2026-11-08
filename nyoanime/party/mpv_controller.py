"""
MPV controller used by Watch Together.

Playback commands are always local. Authority over *shared* playback lives in
SyncEngine; a guest still has to pause/seek its own player to stay in sync.
"""
import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from nyoanime.player import PlayerManager

logger = logging.getLogger(__name__)

# Commands that supersede an identical pending command. A sync burst can
# otherwise pile up seeks while MPV is rebuffering.
_COALESCED_COMMANDS = {
    ("set_property", "pause"): "pause",
    ("set_property", "time-pos"): "time-pos",
    ("set_property", "speed"): "speed",
    ("set_property", "volume"): "volume",
}


class MPVController:
    def __init__(
        self,
        host: bool = False,
        player_manager: Optional[PlayerManager] = None,
        pause_poll_interval: float = 0.15,
        command_timeout: float = 1.5,
    ):
        self.host = host
        self.player_manager = player_manager or PlayerManager()
        self.session: Optional[Dict[str, Any]] = None
        self.on_pause_change: Optional[Callable[[bool, float], Any]] = None
        self.pause_poll_interval = max(0.05, float(pause_poll_interval))
        self.command_timeout = max(0.2, float(command_timeout))
        self._pause_monitor_task: Optional[asyncio.Task] = None
        self._last_known_pause: Optional[bool] = None
        self._pause_suppress_until = 0.0
        # All IPC writes go through one worker so a slow/unresponsive MPV
        # socket never blocks the event loop or a sync callback.
        self._command_task: Optional[asyncio.Task] = None
        self._command_queue: Optional[asyncio.Queue] = None
        self._command_pending: Dict[str, list] = {}
        self._command_wakeup: Optional[asyncio.Event] = None
        self._command_active = False

    @property
    def connected(self) -> bool:
        return self.session is not None

    def set_pause_listener(
        self, callback: Optional[Callable[[bool, float], Any]]
    ) -> None:
        """Notify ``callback(paused, position)`` when MPV's pause property flips.

        Only meaningful on the host: a guest's local pause changes are driven
        by the host and must not echo back into the room.
        """
        self.on_pause_change = callback
        self._ensure_pause_monitor()

    def _ensure_pause_monitor(self) -> None:
        if not self.host or self.on_pause_change is None or self.session is None:
            return
        task = self._pause_monitor_task
        if task is not None and not task.done():
            return
        self._pause_monitor_task = asyncio.create_task(self._monitor_pause())

    async def _monitor_pause(self) -> None:
        try:
            while self.session is not None:
                try:
                    paused = self.is_paused()
                    position = self.get_position()
                except RuntimeError:
                    return
                if self._last_known_pause is None:
                    self._last_known_pause = paused
                elif paused != self._last_known_pause:
                    if time.monotonic() < self._pause_suppress_until:
                        # The player hasn't applied our command yet; the old
                        # value is not a user-driven change.
                        await asyncio.sleep(self.pause_poll_interval)
                        continue
                    self._last_known_pause = paused
                    callback = self.on_pause_change
                    if callback is not None:
                        try:
                            result = callback(paused, position)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            # A failed broadcast must not kill the listener.
                            pass
                await asyncio.sleep(self.pause_poll_interval)
        except asyncio.CancelledError:
            raise

    def allow_command(self, cmd: str) -> bool:
        return cmd in {"play", "pause", "seek", "set_speed", "set_volume", "cycle_sub"}

    async def connect(self, url: str, title: str, **kwargs) -> None:
        if not url:
            raise ValueError("URL is required")
        session = await asyncio.to_thread(
            self.player_manager.play_mpv_session,
            url=url,
            title=title,
            start_time=kwargs.get("start_time", 0.0),
            audio_lang=kwargs.get("audio_lang"),
            headers=kwargs.get("headers"),
            subtitle_url=kwargs.get("subtitle_url"),
            subtitle_lang=kwargs.get("subtitle_lang"),
            audio_url=kwargs.get("audio_url"),
            subtitles_disabled=kwargs.get("subtitles_disabled", False),
            hls_bitrate=kwargs.get("hls_bitrate"),
        )
        if not session:
            raise RuntimeError("Failed to launch MPV")
        self.session = session
        self._command_queue = asyncio.Queue()
        self._command_pending.clear()
        self._ensure_command_worker()
        await self.wait_for_ipc(kwargs.get("ipc_timeout", 10.0))
        self._ensure_pause_monitor()

    async def wait_for_ipc(self, timeout: float = 10.0) -> None:
        if not self.session:
            raise RuntimeError("MPV not connected")
        sock_path = self.session.get("sock_path")
        if os.name == "nt" or not sock_path:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(sock_path):
                return
            proc = self.session.get("process")
            if proc is not None and getattr(proc, "poll", lambda: None)() is not None:
                raise RuntimeError("MPV exited before opening its IPC socket")
            await asyncio.sleep(0.05)
        raise RuntimeError("Timed out waiting for MPV IPC socket")

    async def wait_until_ready(self, timeout: float = 20.0) -> bool:
        """Return True once MPV reports a duration or a non-zero position."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.get_duration() > 0 or self.get_position() > 0:
                    return True
            except RuntimeError:
                return False
            await asyncio.sleep(0.1)
        return False

    async def disconnect(self) -> None:
        monitor, self._pause_monitor_task = self._pause_monitor_task, None
        if monitor is not None and not monitor.done():
            monitor.cancel()
            try:
                await monitor
            except (asyncio.CancelledError, Exception):
                pass
        worker, self._command_task = self._command_task, None
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass
        self._command_queue = None
        self._command_pending.clear()
        self._command_wakeup = None
        self._command_active = False
        self._pause_suppress_until = 0.0
        self._last_known_pause = None
        session = self.session
        if not session:
            return
        sock_path = session.get("sock_path")
        if sock_path:
            try:
                await asyncio.to_thread(PlayerManager.send_mpv_command, sock_path, ["quit"])
            except Exception:
                pass
        proc = session.get("process")
        if proc is not None:
            try:
                proc.terminate()
                await asyncio.to_thread(proc.wait, 5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        tracker = session.get("tracker")
        if tracker and hasattr(tracker, "stop"):
            try:
                # The tracker's socket read can take up to its timeout; keep
                # that wait off the event loop.
                await asyncio.to_thread(tracker.stop)
            except Exception:
                pass
        self.session = None

    def get_position(self) -> float:
        if not self.session:
            raise RuntimeError("MPV not connected")
        value = self.session["tracker"].state.get("time_pos", 0.0) or 0.0
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0

    def get_duration(self) -> float:
        if not self.session:
            raise RuntimeError("MPV not connected")
        value = self.session["tracker"].state.get("duration", 0.0) or 0.0
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0

    def is_paused(self) -> bool:
        if not self.session:
            raise RuntimeError("MPV not connected")
        return bool(self.session["tracker"].state.get("paused", False))

    def get_playback_state(self) -> Dict[str, Any]:
        if not self.session:
            raise RuntimeError("MPV not connected")
        tracker = self.session["tracker"]
        state = dict(getattr(tracker, "state", {}) or {})
        state["position"] = self.get_position()
        state["duration"] = self.get_duration()
        state["paused"] = self.is_paused()
        state["speed"] = float(state.get("speed", 1.0) or 1.0)
        return state

    async def set_pause(self, paused: bool) -> None:
        paused = bool(paused)
        await self._send_command(["set_property", "pause", paused])
        # Optimistic: suppresses an echo when this command is ours. If it
        # fails, the monitor still reports the real value after the grace
        # window on its next tick.
        self._last_known_pause = paused
        self._pause_suppress_until = time.monotonic() + 0.5

    async def play(self) -> None:
        # mpv has no `play` command; resuming is clearing the pause property.
        await self.set_pause(False)

    async def pause(self) -> None:
        await self.set_pause(True)

    async def seek_absolute(self, position: float) -> None:
        position = float(position)
        if position < 0:
            raise ValueError("Position cannot be negative")
        await self._send_command(["set_property", "time-pos", position])

    async def set_speed(self, speed: float) -> None:
        speed = float(speed)
        if speed < 0.5 or speed > 2.0:
            raise ValueError("Speed must be between 0.5 and 2.0")
        await self._send_command(["set_property", "speed", speed])

    async def set_volume(self, volume: int) -> None:
        await self._send_command(["set_property", "volume", max(0, min(130, int(volume)))])

    async def cycle_subtitles(self) -> None:
        await self._send_command(["cycle", "sub"])

    def _ensure_command_worker(self) -> None:
        task = self._command_task
        if task is not None and not task.done():
            return
        if self.session is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._command_queue is None:
            self._command_queue = asyncio.Queue()
        if self._command_wakeup is None:
            self._command_wakeup = asyncio.Event()
        self._command_task = loop.create_task(self._command_loop())

    async def _command_loop(self) -> None:
        try:
            while self.session is not None:
                wakeup = self._command_wakeup
                if wakeup is None:
                    return
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                wakeup.clear()
                await self._drain_commands()
        except asyncio.CancelledError:
            raise
        finally:
            self._command_task = None
            self._command_active = False

    async def _drain_commands(self) -> None:
        queue = self._command_queue
        if queue is not None:
            while True:
                try:
                    cmd = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._run_command(cmd)
        for key in list(self._command_pending):
            cmd = self._command_pending.pop(key, None)
            if cmd is not None:
                await self._run_command(cmd)

    async def _run_command(self, cmd: list) -> None:
        session = self.session
        if session is None:
            return
        sock_path = session.get("sock_path")
        if not sock_path:
            return
        self._command_active = True
        try:
            # The worker owns this await. A wedged socket degrades to a
            # dropped command instead of a stalled sync callback.
            await asyncio.wait_for(
                asyncio.to_thread(
                    PlayerManager.send_mpv_command,
                    sock_path,
                    cmd,
                    None,
                    self.command_timeout,
                ),
                timeout=self.command_timeout + 0.5,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.debug("MPV IPC command timed out: %s", cmd)
        except Exception as exc:
            logger.debug("MPV IPC command dropped (%s): %s", exc, cmd)
        finally:
            self._command_active = False

    async def flush(self, timeout: float = 2.0) -> None:
        """Wait until queued commands were attempted, ignoring failures."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.05, float(timeout))
        while loop.time() < deadline:
            queue_empty = self._command_queue is None or self._command_queue.empty()
            if queue_empty and not self._command_pending and not self._command_active:
                return
            if self._command_task is None or self._command_task.done():
                return
            await asyncio.sleep(0.01)

    async def _send_command(self, cmd: list) -> None:
        if not self.session:
            raise RuntimeError("MPV not connected")
        if not self.session.get("sock_path"):
            raise RuntimeError("MPV IPC socket is unavailable")
        self._ensure_command_worker()
        wakeup = self._command_wakeup
        if wakeup is None or self._command_task is None:
            raise RuntimeError("MPV IPC command queue is unavailable")
        key = _COALESCED_COMMANDS.get((cmd[0], cmd[1])) if len(cmd) >= 2 else None
        if key is not None:
            # Latest value wins; never queue two seeks/speeds/pauses.
            self._command_pending[key] = list(cmd)
        else:
            if self._command_queue is None:
                self._command_queue = asyncio.Queue()
            self._command_queue.put_nowait(list(cmd))
        wakeup.set()

    async def close(self) -> None:
        await self.disconnect()
