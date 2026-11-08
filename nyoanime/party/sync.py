"""
Client-side host-authoritative synchronization.

Timing model
------------
Every host payload carries a wall-clock ``timestamp_ms``. A guest keeps the
minimum observed ``local_now - timestamp`` as the clock offset (the minimum is
used because network latency only ever inflates the sample). Estimated host
position is therefore::

    host_position + max(0, (now - offset - timestamp_ms) / 1000) * speed

A guest's ``stream_time_offset`` shifts only its own target, never the host
timeline published to the room. It compensates for version differences such as
an extra studio intro on a dub:

    target = host_position + stream_time_offset

Drift thresholds are configured as ``(gentle, seek)``:
    |drift| < gentle          -> leave the player alone
    gentle <= |drift| < seek  -> temporary speed nudge, restored to 1.0
    |drift| >= seek           -> absolute seek

No correction is applied while the guest is buffering or while the host is
paused; the last host payload is remembered and applied when buffering ends.
"""
import asyncio
import time
from enum import Enum
from typing import Any, Callable, Dict, Optional

from .mpv_controller import MPVController


class SyncState(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    LOBBY = 2
    RESOLVING = 3
    BUFFERING = 4
    READY = 5
    PLAYING = 6
    PAUSED = 7
    SEEKING = 8
    RECONNECTING = 9
    ENDED = 10
    ERROR = 11


ACTIVE_STATES = (SyncState.PLAYING, SyncState.PAUSED, SyncState.SEEKING)


class SyncEngine:
    def __init__(
        self,
        mpv_controller: MPVController,
        role: str = "guest",
        heartbeat_interval: float = 1.5,
        drift_thresholds: tuple = (0.5, 1.5),
        host_url: Optional[str] = None,
        host_title: Optional[str] = None,
        send_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        anime_reference: Optional[Dict[str, Any]] = None,
        speed_restore_seconds: float = 2.0,
    ):
        self.mpv = mpv_controller
        self.role = role
        self.heartbeat_interval = heartbeat_interval
        self.drift_thresholds = drift_thresholds
        self.host_url = host_url
        self.host_title = host_title
        self.send_callback = send_callback
        self.anime_reference = anime_reference or {}
        self.speed_restore_seconds = speed_restore_seconds

        self.state = SyncState.DISCONNECTED
        self.host_playback: Optional[Dict[str, Any]] = None
        self.local_position = 0.0
        self.stream_time_offset = 0.0
        self.sequence = 0
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.last_correction_time = 0.0
        self.correction_count = 0
        self.last_drift = 0.0
        self.clock_offset_ms: Optional[float] = None
        self.last_host_update: Optional[float] = None
        self._pending_host: Optional[Dict[str, Any]] = None
        self._speed_restore_at: Optional[float] = None
        self._apply_lock = asyncio.Lock()

        self.on_state_change: Callable[[SyncState], None] = lambda s: None
        self.on_playback_update: Callable[[Dict[str, Any]], None] = lambda p: None
        self.on_error: Callable[[Exception], None] = lambda e: None
        self.on_drift: Callable[[float], None] = lambda d: None

        if self.role == "host":
            listener = getattr(self.mpv, "set_pause_listener", None)
            if callable(listener):
                listener(self._handle_local_pause_change)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._set_state(SyncState.CONNECTING)
        if self.role == "host" and self.host_url and self.host_title:
            await self.mpv.connect(
                url=self.host_url,
                title=self.host_title,
                start_time=0.0,
            )
        self._set_state(SyncState.LOBBY)

    async def stop(self) -> None:
        await self._cancel_heartbeat()
        try:
            await self.mpv.disconnect()
        except Exception:
            pass
        self._set_state(SyncState.DISCONNECTED)

    async def _cancel_heartbeat(self) -> None:
        task = self.heartbeat_task
        self.heartbeat_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _set_state(self, state: SyncState) -> None:
        if self.state != state:
            self.state = state
            self.on_state_change(state)

    def set_state(self, state: SyncState) -> None:
        self._set_state(state)

    def set_stream_time_offset(self, seconds: float) -> float:
        """Set this client's offset from the shared timeline, in seconds."""
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            value = 0.0
        if value != value:  # NaN
            value = 0.0
        self.stream_time_offset = max(-120.0, min(120.0, value))
        return self.stream_time_offset

    def adjust_stream_time_offset(self, delta: float) -> float:
        try:
            step = float(delta)
        except (TypeError, ValueError):
            step = 0.0
        return self.set_stream_time_offset(self.stream_time_offset + step)

    # ------------------------------------------------------------------
    # Host
    # ------------------------------------------------------------------
    def _build_host_payload(self) -> Dict[str, Any]:
        try:
            playback = self.mpv.get_playback_state()
        except Exception:
            playback = {
                "position": self.local_position,
                "duration": 0.0,
                "paused": self.state == SyncState.PAUSED,
                "speed": 1.0,
            }
        self.sequence += 1
        self.local_position = float(playback.get("position", 0.0) or 0.0)
        return {
            "position": self.local_position,
            "duration": float(playback.get("duration", 0.0) or 0.0),
            "paused": bool(playback.get("paused", False)),
            "speed": float(playback.get("speed", 1.0) or 1.0),
            "anime_reference": self.anime_reference,
            "host": True,
            "sequence": self.sequence,
            "timestamp_ms": int(time.time() * 1000),
        }

    async def host_publish_heartbeat(self) -> None:
        await self._publish_host_state()

    async def _publish_host_state(
        self,
        paused: Optional[bool] = None,
        position: Optional[float] = None,
    ) -> None:
        if self.role != "host":
            return
        if self.state not in ACTIVE_STATES:
            return
        payload = self._build_host_payload()
        if paused is not None:
            payload["paused"] = bool(paused)
        if position is not None:
            payload["position"] = max(0.0, float(position))
            self.local_position = payload["position"]
        self.host_playback = payload
        if self.send_callback is not None:
            result = self.send_callback(payload)
            if asyncio.iscoroutine(result):
                await result
        self.on_playback_update(payload)
        self._notify_ui()

    async def _handle_local_pause_change(
        self, paused: bool, position: float = 0.0
    ) -> None:
        """Publish an MPV pause flip the moment the tracker sees it."""
        if self.role != "host" or self.state not in ACTIVE_STATES:
            return
        self._set_state(SyncState.PAUSED if paused else SyncState.PLAYING)
        self._ensure_heartbeat()
        await self._publish_host_state(paused=bool(paused), position=position)

    def _notify_ui(self) -> None:
        pass

    async def _heartbeat_loop(self) -> None:
        try:
            while self.state in ACTIVE_STATES:
                try:
                    await self.host_publish_heartbeat()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A dropped socket must not permanently stop sync; retry on
                    # the next tick once the room reconnects.
                    self.on_error(exc)
                await asyncio.sleep(self.heartbeat_interval)
        except asyncio.CancelledError:
            raise
        finally:
            if self.state in ACTIVE_STATES:
                self._set_state(SyncState.LOBBY)

    def _ensure_heartbeat(self) -> None:
        if self.heartbeat_task is None or self.heartbeat_task.done():
            self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def host_start_playback(self) -> None:
        if self.role != "host":
            raise RuntimeError("Only host may start playback")
        await self.mpv.play()
        self._set_state(SyncState.PLAYING)
        self._ensure_heartbeat()
        await self.host_publish_heartbeat()

    async def host_pause(self) -> None:
        if self.role != "host":
            raise RuntimeError("Only host may pause playback")
        await self.mpv.pause()
        self._set_state(SyncState.PAUSED)
        await self.host_publish_heartbeat()

    async def host_seek(self, position: float) -> None:
        if self.role != "host":
            raise RuntimeError("Only host may seek playback")
        await self.mpv.seek_absolute(position)
        self._set_state(SyncState.SEEKING)
        self._ensure_heartbeat()
        await self.host_publish_heartbeat()

    async def host_set_reference(self, reference: Dict[str, Any]) -> None:
        if self.role != "host":
            raise RuntimeError("Only host may change the episode")
        self.anime_reference = reference
        if self.send_callback is not None:
            result = self.send_callback({"anime_reference": reference})
            if asyncio.iscoroutine(result):
                await result

    # ------------------------------------------------------------------
    # Guest
    # ------------------------------------------------------------------
    async def apply_host_state(self, payload: Dict[str, Any]) -> None:
        if self.role != "guest":
            return
        if not payload.get("host"):
            return
        try:
            host_seq = int(payload.get("sequence", 0))
        except (TypeError, ValueError):
            return
        if host_seq <= self.sequence:
            return
        self.sequence = host_seq
        self.host_playback = payload
        self.last_host_update = time.monotonic()
        self.on_playback_update(payload)

        if self.state in (SyncState.BUFFERING, SyncState.RESOLVING):
            self._pending_host = payload
            # A resume must not wait for the buffer to clear: an unpaused
            # player can finish buffering, whereas a stuck paused one cannot.
            if not bool(payload.get("paused", False)):
                try:
                    await self._set_local_pause(False)
                except Exception as exc:
                    self.on_error(exc)
            return
        await self._apply_payload(payload)

    # Backwards-compatible name used by the original unit tests.
    async def guest_process_incoming(self, payload: Dict[str, Any]) -> None:
        await self.apply_host_state(payload)

    async def _apply_payload(self, payload: Dict[str, Any]) -> None:
        # Pause and resume can arrive back-to-back; serializing corrections
        # keeps an older payload from overwriting a newer one.
        async with self._apply_lock:
            await self._apply_payload_unlocked(payload)

    async def _apply_payload_unlocked(self, payload: Dict[str, Any]) -> None:
        try:
            host_pos = float(payload.get("position", 0.0) or 0.0)
            host_speed = float(payload.get("speed", 1.0) or 1.0)
            host_paused = bool(payload.get("paused", False))
            timestamp_ms = float(payload.get("timestamp_ms", 0) or 0)
        except (TypeError, ValueError):
            return

        now_ms = time.time() * 1000
        sample = now_ms - timestamp_ms
        if timestamp_ms <= 0:
            sample = 0.0
        if self.clock_offset_ms is None or sample < self.clock_offset_ms:
            self.clock_offset_ms = max(0.0, sample)
        elapsed = max(0.0, (now_ms - self.clock_offset_ms - timestamp_ms) / 1000.0)
        estimated_host = host_pos + (0.0 if host_paused else elapsed * host_speed)
        # Per-client only: the published host timeline stays the absolute
        # reference, so one guest's version offset cannot move anyone else.
        target = max(0.0, estimated_host + self.stream_time_offset)

        try:
            local_pos = self.mpv.get_position()
            local_paused = self.mpv.is_paused()
        except RuntimeError:
            return

        self.local_position = local_pos
        drift = target - local_pos
        self.last_drift = drift
        self.on_drift(drift)

        # Restore speed before considering a new nudge.
        if self._speed_restore_at is not None and time.monotonic() >= self._speed_restore_at:
            try:
                await self.mpv.set_speed(1.0)
            except Exception:
                pass
            self._speed_restore_at = None

        if host_paused:
            if abs(drift) >= self.drift_thresholds[0]:
                try:
                    await self.mpv.seek_absolute(target)
                except Exception as exc:
                    self.on_error(exc)
            if not local_paused:
                try:
                    await self._set_local_pause(True)
                except Exception as exc:
                    self.on_error(exc)
            self._set_state(SyncState.PAUSED)
            return

        # Always drive the player to the host's state. A guest that paused
        # locally (or whose player was left paused by buffering) must resume
        # on the next host update instead of staying stuck.
        if local_paused:
            try:
                await self._set_local_pause(False)
            except Exception as exc:
                self.on_error(exc)

        gentle, seek = self.drift_thresholds
        if abs(drift) >= seek:
            try:
                await self.mpv.seek_absolute(target)
                self.last_correction_time = time.monotonic()
                self.correction_count += 1
                self._speed_restore_at = None
            except Exception as exc:
                self.on_error(exc)
        elif abs(drift) >= gentle:
            nudge = max(0.95, min(1.05, 1.0 + (drift / 10.0)))
            if abs(nudge - 1.0) >= 0.005:
                try:
                    await self.mpv.set_speed(nudge)
                    self.last_correction_time = time.monotonic()
                    self.correction_count += 1
                    self._speed_restore_at = time.monotonic() + self.speed_restore_seconds
                except Exception as exc:
                    self.on_error(exc)
        else:
            # Close enough: make sure a previous nudge is gone.
            if self._speed_restore_at is not None:
                try:
                    await self.mpv.set_speed(1.0)
                except Exception:
                    pass
                self._speed_restore_at = None

        if self.state in (
            SyncState.LOBBY,
            SyncState.CONNECTING,
            SyncState.READY,
            SyncState.RECONNECTING,
            SyncState.PAUSED,
        ):
            self._set_state(SyncState.PLAYING)

    async def _set_local_pause(self, paused: bool) -> None:
        setter = getattr(self.mpv, "set_pause", None)
        if callable(setter):
            result = setter(bool(paused))
            if asyncio.iscoroutine(result):
                await result
            return
        if paused:
            await self.mpv.pause()
        else:
            await self.mpv.play()

    async def set_buffering(self, buffering: bool) -> None:
        if buffering:
            self._set_state(SyncState.BUFFERING)
            return
        self._set_state(SyncState.READY)
        pending = self._pending_host
        self._pending_host = None
        if pending:
            await self._apply_payload(pending)

    async def set_ready(self, ready: bool = True) -> None:
        self._set_state(SyncState.READY if ready else SyncState.BUFFERING)

    async def apply_host_reference(self, reference: Dict[str, Any]) -> None:
        self.anime_reference = reference or {}
