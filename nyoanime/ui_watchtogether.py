"""
Watch Together TUI screens for NyoAnime.

The room screen owns a live ``WTRClient`` for as long as the room is open. It
polls keys without blocking the event loop so Supabase Realtime can keep
delivering presence and broadcast events while the user reads the screen.

The visual language follows NyoAnime's centered logo + panel + keycap-dock
screens, with the simple host/join flow and member markers from the original
ani-cli-ar Watch Together UI.
"""

import asyncio
import base64
import getpass
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from rich.align import Align
from rich.cells import cell_len
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.box import HEAVY

from . import config as config_module
from .config import (
    COLOR_ACCENT,
    COLOR_BOLT,
    COLOR_BORDER,
    COLOR_ERROR,
    COLOR_HIGHLIGHT_BG,
    COLOR_HIGHLIGHT_FG,
    COLOR_PROMPT,
    COLOR_SECONDARY_TEXT,
    COLOR_SUBTITLE,
    COLOR_SUCCESS,
    COLOR_TITLE,
    BOUNCING_BAR_FRAMES,
)
from .party.client import PartyClientError, WTRClient
from .party.lobby import (
    LobbyMember,
    LobbyOption,
    LobbyState,
    allowed_modes,
    discover_options,
    preferred_fallback_mode,
)
from .party.mpv_controller import MPVController
from .party.protocol import PartyEvent, sanitize_diagnostic
from .party.stream_offsets import (
    canonical_provider_id,
    load_stream_offset,
    save_stream_offset,
)
from .party.supabase_config import SupabaseConfigError
from .party.sync import SyncEngine, SyncState
from .providers.language import language_badge, normalize_language_mode, provider_language_mode
from .utils import RawTerminal, flush_stdin, get_key

logger = logging.getLogger(__name__)

_THEME_COLORS = (
    "COLOR_ACCENT",
    "COLOR_BOLT",
    "COLOR_BORDER",
    "COLOR_ERROR",
    "COLOR_HIGHLIGHT_BG",
    "COLOR_HIGHLIGHT_FG",
    "COLOR_PROMPT",
    "COLOR_SECONDARY_TEXT",
    "COLOR_SUBTITLE",
    "COLOR_SUCCESS",
    "COLOR_TITLE",
)


def _sync_theme_colors() -> None:
    """Pick up theme changes made while the app was running."""
    for name in _THEME_COLORS:
        value = getattr(config_module, name, None)
        if value:
            globals()[name] = value


_CODE_LEN = 6
_STATE_REFRESH_SECONDS = 5.0
_STATUS_POLL_SECONDS = 0.06
_RECONNECT_RETRY_SECONDS = 4.0
_LOBBY_DEADLINE_SECONDS = 20.0
_LEAVE_TIMEOUT_SECONDS = 8.0
_SYNC_APPLY_TIMEOUT_SECONDS = 5.0
_STREAM_OFFSET_STEP_SECONDS = 0.5
_STREAM_OFFSET_OSD_MS = 1400
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _format_stream_offset(seconds: float) -> str:
    value = 0.0 if abs(float(seconds)) < 0.05 else float(seconds)
    if value == 0.0:
        return "Sync Offset: 0.0s"
    return f"Sync Offset: {value:+.1f}s"


class _RoomPlayback:
    """Owns the room's MPV/SyncEngine pair and swaps episodes safely."""

    def __init__(self, role: str, send_callback=None, on_error=None):
        self.role = "host" if str(role).lower() == "host" else "guest"
        self.send_callback = send_callback
        self.on_error = on_error
        self.mpv: Optional[MPVController] = None
        self.sync: Optional[SyncEngine] = None
        self.stream_key: Optional[str] = None
        self.offset_provider = ""
        self.offset_mode = ""
        self.stream_time_offset = 0.0

    @property
    def active(self) -> bool:
        return self.sync is not None

    async def start(self, playback: Dict[str, Any], reference: Dict[str, Any]) -> None:
        if not isinstance(playback, dict):
            raise ValueError("No stream was resolved for this episode")
        url = str(playback.get("url") or playback.get("stream_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("The picked episode has no playable stream URL")

        await self.stop()
        self.mpv = MPVController(host=self.role == "host")
        self.sync = SyncEngine(
            self.mpv,
            role=self.role,
            send_callback=self.send_callback,
            anime_reference=reference,
        )
        self.sync.on_error = self._handle_sync_error

        provider_id = str(
            playback.get("provider_id") or reference.get("provider_id") or ""
        )
        mode = str(playback.get("language_mode") or "")
        if not mode and provider_id:
            mode = provider_language_mode(provider_id)
        if not mode:
            mode = str(reference.get("language_mode") or "")
        self.offset_provider = canonical_provider_id(provider_id)
        self.offset_mode = normalize_language_mode(mode)
        self.stream_time_offset = 0.0
        start_time = float(playback.get("start_time") or 0.0)
        if self.role == "guest":
            saved = load_stream_offset(self.offset_provider, self.offset_mode)
            if saved:
                # Apply before launch so the first host heartbeat doesn't have
                # to yank the stream forward by the whole intro offset.
                self.stream_time_offset = saved
                self.sync.set_stream_time_offset(saved)
                start_time = max(0.0, start_time + saved)

        try:
            await self.mpv.connect(
                url=url,
                title=str(playback.get("title") or reference.get("title") or "Watch Together"),
                start_time=start_time,
                audio_lang=playback.get("audio_lang") or None,
                headers=playback.get("headers") or None,
                subtitle_url=playback.get("subtitle_url") or None,
                subtitle_lang=playback.get("subtitle_lang") or None,
                audio_url=playback.get("audio_url") or None,
                subtitles_disabled=bool(playback.get("subtitles_disabled", False)),
                hls_bitrate=int(playback.get("hls_bitrate") or 0) or None,
            )
            if self.role == "host":
                try:
                    await self.sync.host_start_playback()
                except Exception as exc:
                    # MPV is already open. A dropped control channel must not
                    # close it; the heartbeat loop retries once the room is back.
                    self._handle_sync_error(exc)
            else:
                self.sync.set_state(SyncState.READY)
            self.stream_key = url
        except Exception:
            await self.stop()
            raise

    async def apply_sync(self, payload: Dict[str, Any]) -> None:
        if self.sync is not None:
            await self.sync.apply_host_state(payload)

    async def stop(self) -> None:
        sync, self.sync = self.sync, None
        self.mpv = None
        self.stream_key = None
        self.offset_provider = ""
        self.offset_mode = ""
        self.stream_time_offset = 0.0
        if sync is not None:
            try:
                await sync.stop()
            except Exception:
                pass

    def _handle_sync_error(self, exc: Exception) -> None:
        if self.on_error is not None:
            self.on_error(exc)


class _RoomRuntime:
    """Owns one room's client state beyond the lifetime of its screen.

    The runtime lives on a dedicated asyncio loop. Opening the room view just
    attaches a renderer/key loop to it; backing out to the main menu leaves the
    Supabase subscriptions, MPV session and sync heartbeat running.
    """

    def __init__(
        self,
        owner: "WatchTogetherUI",
        client: WTRClient,
        initial_error: Optional[str] = None,
        technical_error: Optional[str] = None,
        notice: Optional[str] = None,
    ):
        self.owner = owner
        self.client = client
        self.role = str(getattr(client, "role", "guest") or "guest").lower()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._bg_task: Optional[asyncio.Task] = None
        self._state_task: Optional[asyncio.Task] = None
        self._retry_task: Optional[asyncio.Task] = None
        self._playback_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._pending_sync: Optional[Dict[str, Any]] = None
        self._subscriptions: List[Tuple[str, Callable[[Any], None]]] = []
        self._closed = False
        self._stopped = False
        self._foreground = False
        self._dirty = True
        self._status_lock = threading.Lock()
        self._status: Dict[str, Any] = {}

        self.state: Optional[Dict[str, Any]] = None
        if hasattr(client, "local_room_state"):
            try:
                self.state = client.local_room_state()
            except Exception:
                self.state = None
        self.state_error: Optional[str] = None
        self.leave_error: Optional[str] = None
        self.error = initial_error
        self.technical = technical_error or getattr(client, "channel_error", None)
        self.notice = notice or ""
        self.notice_until = time.monotonic() + 4.0 if self.notice else 0.0
        self.show_diagnostics = False
        self.busy = ""
        self.selected_reference: Optional[Dict[str, str]] = None
        self.selected_playback: Optional[Dict[str, Any]] = None
        self.pending_share = False
        self.was_ready = bool(getattr(client, "channels_ready", False))
        self.spinner_frame = 0
        self.left = False
        self.quit_requested = False
        self.next_refresh = time.monotonic() + _STATE_REFRESH_SECONDS
        self.next_retry = time.monotonic() + _RECONNECT_RETRY_SECONDS

        # Lobby state for the current pick. ``option_probe`` is injectable so
        # tests (and future provider integrations) can bypass network probes.
        self.lobby: Optional[LobbyState] = None
        self.option_probe = None
        self._lobby_task: Optional[asyncio.Task] = None
        self._lobby_starting = False
        self._lobby_snapshot_pending = False
        self._lobby_countdown = None
        self._pending_lobby_state = False
        self._pending_lobby_ready = False

        self.playback = _RoomPlayback(
            self.role,
            send_callback=self._send_sync,
            on_error=self._on_playback_error,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if hasattr(self.client, "subscribe"):
            for event, handler in (
                (PartyEvent.EPISODE_CHANGED.value, self._on_episode_changed),
                (PartyEvent.LOBBY_STATE.value, self._on_lobby_state),
                (PartyEvent.LOBBY_READY.value, self._on_lobby_ready),
                (PartyEvent.ROOM_STATE.value, self._on_room_state),
                (PartyEvent.SYNC_PLAYBACK.value, self._on_sync_playback),
            ):
                try:
                    self.client.subscribe(event, handler)
                    self._subscriptions.append((event, handler))
                except Exception:
                    pass
        start_poll = getattr(self.client, "start_playback_poll", None)
        if callable(start_poll):
            try:
                start_poll()
            except Exception:
                pass
        self._update_status()
        self._bg_task = self._loop.create_task(self._background_loop())
        if getattr(self.client, "room_active", False):
            self._state_task = self._loop.create_task(self.client.get_room_state())

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._closed = True
        self._pending_sync = None
        for task in (
            self._bg_task,
            self._state_task,
            self._retry_task,
            self._playback_task,
            self._sync_task,
            self._lobby_task,
        ):
            await self._cancel_task(task)
        self._bg_task = self._state_task = self._retry_task = None
        self._playback_task = self._lobby_task = self._sync_task = None
        self._pending_sync = None
        await self.playback.stop()
        stop_poll = getattr(self.client, "stop_playback_poll", None)
        if callable(stop_poll):
            try:
                stop_poll()
            except Exception:
                pass
        if hasattr(self.client, "unsubscribe"):
            for event, handler in self._subscriptions:
                try:
                    self.client.unsubscribe(event, handler)
                except Exception:
                    pass
        self._subscriptions.clear()

    def configure(
        self,
        initial_error: Optional[str] = None,
        technical_error: Optional[str] = None,
        notice: Optional[str] = None,
    ) -> None:
        if initial_error is not None:
            self.error = initial_error
        if technical_error is not None:
            self.technical = technical_error
        if notice:
            self.notice = notice
            self.notice_until = time.monotonic() + 4.0
        self._dirty = True

    # ------------------------------------------------------------------
    # Background maintenance
    # ------------------------------------------------------------------
    async def _background_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Watch Together background tick failed", exc_info=True)
                await asyncio.sleep(
                    _STATUS_POLL_SECONDS if self._foreground else 0.25
                )
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        now = time.monotonic()
        if self.notice and self.notice_until and now >= self.notice_until:
            self.notice = ""
            self.notice_until = 0.0
            self._dirty = True

        if self._state_task is not None and self._state_task.done():
            try:
                self.state = self._state_task.result()
                self.state_error = None
                if self.notice.startswith(("Refreshing", "We couldn't refresh")):
                    self.notice = ""
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.state_error = self.owner._describe_error(exc)
                if not self.notice:
                    self.notice = (
                        "We couldn't refresh the member list. Press [r] to try again."
                    )
                    self.notice_until = now + 4.0
            self._state_task = None
            self.next_refresh = now + _STATE_REFRESH_SECONDS
            if self.lobby is not None and not self.lobby.started:
                self._seed_lobby_members()
                self._lobby_snapshot_pending = False
            self._dirty = True

        if (
            self._state_task is None
            and getattr(self.client, "channels_ready", False)
            and now >= self.next_refresh
        ):
            self._state_task = self._loop.create_task(self.client.get_room_state())
            self.next_refresh = now + _STATE_REFRESH_SECONDS

        if self._retry_task is not None and self._retry_task.done():
            try:
                self._retry_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._retry_task = None
            self.next_retry = now + _RECONNECT_RETRY_SECONDS
            self._dirty = True

        if self._playback_task is not None and self._playback_task.done():
            failed = False
            try:
                self._playback_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                failed = True
                self.set_notice(self.owner._playback_error_notice(exc))
                if self.lobby is not None and self.lobby.started:
                    # Let the host retry (or [s] again) instead of stranding
                    # the lobby on a failed player launch.
                    self.lobby.started = False
                    self._lobby_starting = False
            self._playback_task = None
            self.busy = ""
            if (
                not failed
                and self.lobby is not None
                and self.lobby.started
                and self.playback.active
            ):
                # Playback is running; the lobby has done its job.
                self.lobby = None
            self._dirty = True

        ready_now = bool(getattr(self.client, "channels_ready", False))
        if (
            self._retry_task is None
            and not ready_now
            and not self.busy
            and getattr(self.client, "room_active", False)
            and now >= self.next_retry
        ):
            self._retry_task = self._loop.create_task(
                self.client.retry_connection()
            )
            self.next_retry = now + _RECONNECT_RETRY_SECONDS

        lobby = self.lobby
        if lobby is not None and not lobby.started:
            self.spinner_frame = int(now * 9)
            countdown = int(lobby.remaining(now) + 0.999)
            if countdown != self._lobby_countdown:
                self._lobby_countdown = countdown
                self._dirty = True
            if self.role == "host":
                self._maybe_auto_start()
        elif self.busy or not ready_now:
            self.spinner_frame = int(now * 9)

        if getattr(self.client, "channels_ready", False) and self.error:
            self.error = None
            self.technical = getattr(self.client, "channel_error", None) or self.technical
            self.set_notice("Connected again.")

        if ready_now and not self.was_ready:
            if self.pending_share:
                await self.share_selection()
            if self._pending_lobby_state and lobby is not None and self.role == "host":
                self._pending_lobby_state = False
                await self._broadcast_lobby_state()
            if self._pending_lobby_ready and lobby is not None and lobby.ready:
                self._pending_lobby_ready = False
                await self._broadcast_lobby_ready()
            if not self.notice:
                self.set_notice("Connected again.")
        self.was_ready = ready_now
        self._update_status()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def _spawn_task(self, coro) -> Optional[asyncio.Task]:
        try:
            if self._loop is not None and self._loop.is_running():
                return self._loop.create_task(coro)
            return asyncio.ensure_future(coro)
        except RuntimeError:
            logger.debug("Watch Together event arrived after the room stopped")
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return None

    def _spawn(self, coro) -> None:
        self._spawn_task(coro)

    def _on_episode_changed(self, message: Any) -> None:
        # Update the visible pick synchronously; only the MPV swap is async.
        reference = self.owner._reference_from_message(message)
        if not reference:
            return
        self.selected_reference = reference
        incoming_playback = self.owner._playback_from_message(message)
        if str(getattr(self.client, "role", "guest") or "guest").lower() != "host":
            lobby = self.lobby
            if lobby is not None and not lobby.started:
                self._apply_lobby_start(lobby, incoming_playback, reference)
                return
            self.set_notice(f"Host picked: {self.owner._selection_label(reference)}")
            if incoming_playback:
                self.selected_playback = incoming_playback
                self.queue_playback(incoming_playback, reference)
            else:
                self.selected_playback = None
                self._spawn(self._stop_guest_playback())
        else:
            if incoming_playback:
                self.selected_playback = incoming_playback
        self._dirty = True

    def _apply_lobby_start(
        self,
        lobby: LobbyState,
        incoming_playback: Optional[Dict[str, Any]],
        reference: Dict[str, Any],
    ) -> None:
        """The host started: use this member's verified pick when it has one."""
        lobby.started = True
        local = lobby.selected or lobby.fallback
        playback = (local.playback if local is not None else None) or incoming_playback
        if local is not None:
            self.selected_playback = local.playback
        elif incoming_playback:
            self.selected_playback = incoming_playback
        if playback:
            self.queue_playback(playback, lobby.reference or reference)
            mode = local.mode_label if local is not None else "host stream"
            self.set_notice(f"Starting with {mode}.")
        else:
            self.selected_playback = None
            self.set_notice("The host started, but no stream was available.")
            self._spawn(self._stop_guest_playback())
        self._dirty = True

    async def _stop_guest_playback(self) -> None:
        self._pending_sync = None
        await self._cancel_task(self._sync_task)
        self._sync_task = None
        await self._cancel_task(self._playback_task)
        self._playback_task = None
        await self.playback.stop()

    def _on_sync_playback(self, message: Any) -> None:
        if self._closed or not self.playback.active:
            return
        payload = (
            message.get("payload")
            if isinstance(message, dict)
            else getattr(message, "payload", None)
        )
        if not isinstance(payload, dict):
            return
        # One pump, latest payload wins: a burst of host updates must not spawn
        # a task per message or queue stale corrections behind a slow player.
        self._pending_sync = payload
        task = self._sync_task
        if task is None or task.done():
            self._sync_task = self._spawn_task(self._drain_sync())

    async def _drain_sync(self) -> None:
        try:
            while self._pending_sync is not None:
                payload = self._pending_sync
                self._pending_sync = None
                if not self.playback.active:
                    continue
                try:
                    await asyncio.wait_for(
                        self.playback.apply_sync(payload),
                        timeout=_SYNC_APPLY_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.debug("Watch Together sync update timed out")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("Watch Together sync update dropped: %s", exc)
        finally:
            self._sync_task = None
            if self._pending_sync is not None and not self._closed:
                self._sync_task = self._spawn_task(self._drain_sync())

    def _on_room_state(self, message: Any) -> None:
        reference = self.owner._reference_from_message(message)
        if reference and reference != self.selected_reference:
            self.selected_reference = reference
            self._dirty = True

    # ------------------------------------------------------------------
    # Lobby
    # ------------------------------------------------------------------
    def _self_key(self) -> str:
        return str(
            getattr(self.client, "participant_id", None)
            or getattr(self.client, "nickname", None)
            or "you"
        )

    def _state_participants(self) -> List[Dict[str, Any]]:
        state = self.state if isinstance(self.state, dict) else {}
        raw = state.get("participants")
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)]
        if isinstance(raw, dict):
            return [row for row in raw.values() if isinstance(row, dict)]
        return []

    def _seed_lobby_members(self) -> None:
        lobby = self.lobby
        if lobby is None or lobby.started:
            return
        state = self.state if isinstance(self.state, dict) else {}
        you = state.get("you") if isinstance(state.get("you"), dict) else {}
        you_pid = str(
            you.get("participant_id")
            or getattr(self.client, "participant_id", None)
            or ""
        )
        you_name = str(
            you.get("nickname") or getattr(self.client, "nickname", None) or "You"
        )
        for row in self._state_participants():
            key = str(
                row.get("id")
                or row.get("participant_id")
                or row.get("user_id")
                or row.get("nickname")
                or ""
            )
            if not key:
                continue
            name = str(row.get("nickname") or row.get("name") or "Guest")
            role = str(row.get("role") or "guest").lower()
            if role not in ("host", "guest"):
                role = "guest"
            is_you = bool(
                (you_pid and key == you_pid)
                or (not you_pid and name == you_name)
            )
            member = lobby.members.get(key)
            if member is None:
                lobby.members[key] = LobbyMember(
                    key=key,
                    name=name,
                    role=role,
                    is_you=is_you,
                )
                continue
            member.name = name
            member.role = role
            member.is_you = member.is_you or is_you
        self._register_self_lobby_member()

    def _register_self_lobby_member(self) -> None:
        lobby = self.lobby
        if lobby is None:
            return
        key = self._self_key()
        name = str(getattr(self.client, "nickname", None) or "You")
        member = lobby.members.get(key)
        if member is None:
            member = LobbyMember(key=key, name=name, role=self.role, is_you=True)
            lobby.members[key] = member
        member.name = name
        member.role = self.role
        member.is_you = True

    def _on_lobby_state(self, message: Any) -> None:
        if self.role == "host":
            # The sender never receives its own broadcast; a second host
            # broadcast in a guest's seat is stale or malicious.
            return
        payload = (
            message.get("payload")
            if isinstance(message, dict)
            else getattr(message, "payload", None)
        )
        if not isinstance(payload, dict):
            return
        reference = self.owner._reference_from_message(message)
        if not reference:
            return
        lobby_id = str(payload.get("lobby_id") or "")
        current = self.lobby
        if current is not None and current.lobby_id == lobby_id:
            return
        remaining = _LOBBY_DEADLINE_SECONDS
        deadline_ms = payload.get("deadline_ms")
        if deadline_ms not in (None, ""):
            try:
                remaining = (
                    int(deadline_ms) - int(time.time() * 1000)
                ) / 1000.0
            except (TypeError, ValueError):
                remaining = _LOBBY_DEADLINE_SECONDS
        remaining = min(max(remaining, 0.0), 60.0)
        self._spawn(
            self.begin_lobby(
                None,
                reference,
                broadcast=False,
                lobby_id=lobby_id or None,
                deadline_seconds=remaining,
            )
        )

    def _on_lobby_ready(self, message: Any) -> None:
        payload = (
            message.get("payload")
            if isinstance(message, dict)
            else getattr(message, "payload", None)
        )
        if not isinstance(payload, dict):
            return
        lobby = self.lobby
        if lobby is None or lobby.started:
            return
        lobby_id = str(payload.get("lobby_id") or "")
        if lobby_id and lobby_id != lobby.lobby_id:
            return
        key = str(
            payload.get("participant_id")
            or payload.get("nickname")
            or ""
        )
        if not key:
            return
        name = str(payload.get("nickname") or "Guest")
        role = str(payload.get("role") or "guest").lower()
        if role not in ("host", "guest"):
            role = "guest"
        member = lobby.members.get(key)
        if member is None:
            member = LobbyMember(key=key, name=name, role=role)
            lobby.members[key] = member
        member.name = name
        member.role = role
        member.ready = bool(payload.get("ready"))
        member.mode = str(payload.get("mode") or "") if member.ready else ""
        member.is_you = key == self._self_key() or member.is_you
        self._dirty = True
        if self.role == "host":
            self._maybe_auto_start()

    async def begin_lobby(
        self,
        playback: Optional[Dict[str, Any]],
        reference: Optional[Dict[str, Any]],
        *,
        broadcast: bool = True,
        lobby_id: Optional[str] = None,
        deadline_seconds: Optional[float] = None,
    ) -> None:
        reference = self.owner._clean_reference(reference)
        if not reference:
            self.set_notice("Couldn't open the lobby without an episode.")
            return
        if self.playback.active:
            await self.playback.stop()
        self.selected_reference = reference
        self.selected_playback = playback if isinstance(playback, dict) else None

        seed_mode = ""
        language_mode = str(reference.get("language_mode") or "")
        if language_mode:
            seed_mode = normalize_language_mode(language_mode)
        elif reference.get("provider_id"):
            seed_mode = provider_language_mode(reference.get("provider_id"))
        seed_playback = (
            dict(playback)
            if isinstance(playback, dict) and playback.get("url")
            else None
        )
        identity = (
            reference.get("anime_id")
            or reference.get("title")
            or "episode"
        )
        lobby = LobbyState(
            reference=reference,
            lobby_id=lobby_id
            or f"{identity}:{reference.get('episode_number', '')}:{int(time.time() * 1000)}",
            deadline=time.monotonic()
            + max(
                0.0,
                _LOBBY_DEADLINE_SECONDS
                if deadline_seconds is None
                else float(deadline_seconds),
            ),
            seed_mode=seed_mode,
            seed_playback=seed_playback,
        )
        if seed_playback and seed_mode:
            seed_option = lobby.seed_option
            if seed_option is not None:
                lobby.options = [seed_option]
                lobby.selected = seed_option
                lobby.choice_index = 0
        self.lobby = lobby
        self._lobby_starting = False
        self._lobby_countdown = int(lobby.remaining() + 0.999)
        self._register_self_lobby_member()
        self._seed_lobby_members()
        self._lobby_snapshot_pending = not bool(self._state_participants())
        self._dirty = True

        ready_now = bool(getattr(self.client, "channels_ready", False))
        if (
            ready_now
            and self._state_task is None
            and getattr(self.client, "room_active", False)
        ):
            self._state_task = self._loop.create_task(self.client.get_room_state())
            self.next_refresh = time.monotonic() + _STATE_REFRESH_SECONDS

        if broadcast and self.role == "host":
            await self._broadcast_lobby_state()
        self._start_lobby_discovery()
        self.set_notice(
            "Choose your language, then press Enter to get ready."
        )

    async def _broadcast_lobby_state(self) -> None:
        lobby = self.lobby
        if lobby is None or lobby.started or self.role != "host":
            return
        if not getattr(self.client, "channels_ready", False):
            self._pending_lobby_state = True
            return
        reference = lobby.reference
        payload: Dict[str, Any] = {
            "lobby_id": lobby.lobby_id,
            "anime_reference": reference,
            "deadline_ms": int(
                time.time() * 1000 + lobby.remaining() * 1000
            ),
            "deadline_seconds": int(lobby.remaining() + 0.999),
            "fallback_mode": preferred_fallback_mode(
                reference.get("anime_type"),
                [mode for mode in allowed_modes(reference.get("anime_type"))],
            ),
            "host_mode": lobby.seed_mode,
        }
        try:
            await self.client.send_event(PartyEvent.LOBBY_STATE.value, payload)
            self._pending_lobby_state = False
        except Exception as exc:
            self._pending_lobby_state = True
            logger.warning("Watch Together lobby broadcast failed: %s", exc)
            self.set_notice("Couldn't share the lobby yet. Retrying when connected.")

    async def _broadcast_lobby_ready(self) -> None:
        lobby = self.lobby
        if lobby is None:
            return
        option = lobby.selected if lobby.ready else None
        payload: Dict[str, Any] = {
            "lobby_id": lobby.lobby_id,
            "participant_id": self._self_key(),
            "nickname": str(getattr(self.client, "nickname", "") or "You"),
            "role": self.role,
            "ready": bool(lobby.ready and option is not None),
            "mode": option.mode if option is not None else "",
            "provider_id": option.provider_id if option is not None else "",
        }
        if not getattr(self.client, "channels_ready", False):
            self._pending_lobby_ready = True
            return
        try:
            await self.client.send_event(PartyEvent.LOBBY_READY.value, payload)
            self._pending_lobby_ready = False
        except Exception as exc:
            self._pending_lobby_ready = True
            logger.warning("Watch Together readiness broadcast failed: %s", exc)

    def _start_lobby_discovery(self) -> None:
        lobby = self.lobby
        if lobby is None:
            return
        if self._lobby_task is not None and not self._lobby_task.done():
            self._lobby_task.cancel()
        self._lobby_task = self._loop.create_task(
            self._discover_lobby_options(lobby)
        )

    async def _discover_lobby_options(self, lobby: LobbyState) -> None:
        try:
            options = await discover_options(
                lobby.reference,
                probe=self.option_probe,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Watch Together lobby probe failed", exc_info=True)
            options = []
        if self.lobby is not lobby or lobby.started:
            return
        seed = lobby.seed_option
        merged: List[LobbyOption] = []
        if seed is not None:
            merged.append(seed)
        for option in options:
            if seed is not None and option.mode == seed.mode:
                continue
            merged.append(option)
        lobby.options = merged
        lobby.discovering = False
        previous = lobby.selected
        match = next(
            (option for option in merged if option == previous), None
        ) if previous is not None else None
        # Keep the host's seed pick pointing at the merged object so identity
        # checks (selection marker, arrow re-selection) stay consistent.
        lobby.selected = match
        if previous is not None and match is None:
            lobby.ready = False
        lobby.choice_index = min(
            lobby.choice_index, max(0, len(merged) - 1)
        )
        if not merged:
            lobby.error = "No playable stream was verified for this episode."
            self.set_notice("No playable stream was verified for this episode.")
        else:
            lobby.error = ""
            self.set_notice(
                f"{len(merged)} language option"
                f"{'' if len(merged) == 1 else 's'} verified."
            )
        self._dirty = True

    def move_lobby_choice(self, delta: int) -> None:
        lobby = self.lobby
        if lobby is None or lobby.started or not lobby.options:
            return
        lobby.choice_index = (
            lobby.choice_index + int(delta)
        ) % len(lobby.options)
        highlighted = lobby.options[lobby.choice_index]
        if lobby.ready and highlighted is not lobby.selected:
            lobby.ready = False
            member = lobby.members.get(self._self_key())
            if member is not None:
                member.ready = False
                member.mode = ""
            self._spawn(self._broadcast_lobby_ready())
        self._dirty = True

    async def confirm_lobby_choice(self) -> None:
        lobby = self.lobby
        if lobby is None or lobby.started:
            return
        if not lobby.options:
            self.set_notice(
                "Still checking for playable streams..."
                if lobby.discovering
                else "No playable stream was verified for this episode."
            )
            return
        lobby.choice_index %= len(lobby.options)
        option = lobby.options[lobby.choice_index]
        lobby.selected = option
        lobby.ready = True
        self.selected_playback = option.playback
        member = lobby.members.get(self._self_key())
        if member is not None:
            member.ready = True
            member.mode = option.mode
        await self._broadcast_lobby_ready()
        self.set_notice(f"Ready: {option.label}.")
        self._dirty = True
        if self.role == "host":
            self._maybe_auto_start()

    def _maybe_auto_start(self) -> None:
        if self.role != "host":
            return
        lobby = self.lobby
        if lobby is None or lobby.started or self._lobby_starting:
            return
        if self._lobby_snapshot_pending or not lobby.all_ready:
            return
        self._lobby_starting = True
        self._spawn(self._host_start())

    async def start_lobby(self, force: bool = False) -> bool:
        lobby = self.lobby
        if self.role != "host" or lobby is None or lobby.started:
            return False
        if not force and not lobby.can_start_anyway():
            return False
        return await self._host_start()

    async def _host_start(self) -> bool:
        lobby = self.lobby
        self._lobby_starting = False
        if lobby is None or lobby.started or self.role != "host":
            return False
        fallback = lobby.fallback
        local = lobby.selected or fallback
        playback = local.playback if local is not None else None
        if not playback:
            self.set_notice(
                "No playable stream was verified. Try another episode."
            )
            return False
        lobby.started = True
        self._dirty = True
        if self._lobby_task is not None and not self._lobby_task.done():
            self._lobby_task.cancel()
        self._lobby_task = None

        # Unready members get the room fallback (English sub first), not the
        # host's personal choice.
        broadcast = fallback or local
        if broadcast is not None:
            safe_playback = self.owner._broadcast_playback(broadcast.playback)
            broadcaster = getattr(self.client, "broadcast_playback", None)
            try:
                if callable(broadcaster) and safe_playback:
                    await broadcaster(safe_playback, lobby.reference)
                else:
                    await self.client.send_event(
                        PartyEvent.EPISODE_CHANGED.value,
                        {
                            "anime_reference": lobby.reference,
                            "playback": safe_playback or broadcast.playback,
                        },
                    )
            except Exception as exc:
                logger.warning(
                    "Watch Together lobby start broadcast failed: %s", exc
                )
                self.set_notice(
                    "Starting locally, but guests may not have received the stream."
                )
        self.selected_playback = playback
        self.queue_playback(playback, lobby.reference)
        return True

    async def _send_sync(self, payload: Dict[str, Any]) -> None:
        await self.client.send_event(PartyEvent.SYNC_PLAYBACK.value, payload)

    def _on_playback_error(self, exc: Exception) -> None:
        self.set_notice(self.owner._playback_error_notice(exc))

    async def adjust_stream_offset(self, delta: float) -> None:
        """Nudge this client's offset without moving any other client's."""
        playback = self.playback
        sync = playback.sync
        if sync is None:
            return
        previous = float(sync.stream_time_offset or 0.0)
        current = sync.adjust_stream_time_offset(delta)
        delta = current - previous
        playback.stream_time_offset = current
        mpv = playback.mpv
        target = None
        if mpv is not None:
            try:
                target = max(0.0, float(mpv.get_position()) + delta)
            except Exception:
                target = None
        if delta and mpv is not None and target is not None:
            if self.role == "host":
                # A host nudge is a real master seek; guests follow on the
                # next heartbeat. Their own offsets remain untouched.
                self._spawn(sync.host_seek(target))
            else:
                try:
                    await mpv.seek_absolute(target)
                except Exception as exc:
                    logger.debug("Watch Together offset seek failed: %s", exc)
        if mpv is not None:
            try:
                await mpv.show_osd(
                    _format_stream_offset(current),
                    duration_ms=_STREAM_OFFSET_OSD_MS,
                )
            except Exception as exc:
                logger.debug("Watch Together offset OSD failed: %s", exc)
        if self.role == "guest":
            save_stream_offset(
                playback.offset_provider,
                playback.offset_mode,
                current,
            )
        self.set_notice(_format_stream_offset(current))
        self._dirty = True

    def set_notice(self, value: str) -> None:
        self.notice = value
        self.notice_until = time.monotonic() + 4.0 if value else 0.0
        self._dirty = True

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------
    def queue_playback(
        self,
        playback: Optional[Dict[str, Any]],
        reference: Optional[Dict[str, Any]],
    ) -> None:
        if not playback or not isinstance(reference, dict):
            return
        if self._playback_task is not None and not self._playback_task.done():
            self._playback_task.cancel()
        self.busy = "Resolving stream & launching MPV..."
        self._playback_task = self._loop.create_task(
            self.playback.start(playback, reference)
        )
        self._dirty = True

    async def play_episode(
        self,
        playback: Dict[str, Any],
        reference: Dict[str, Any],
    ) -> None:
        """Host a stream picked from the normal anime menus.

        The pick opens a lobby first; MPV starts only after the host confirms
        their own language (or overrides the ready check).
        """
        self.selected_reference = reference
        self.selected_playback = playback
        self._dirty = True
        await self.begin_lobby(playback, reference, broadcast=self.role == "host")

    async def share_selection(self) -> bool:
        reference = self.selected_reference
        if (
            reference is None
            or not getattr(self.client, "channels_ready", False)
            or not hasattr(self.client, "send_event")
        ):
            self.pending_share = True
            return False
        label = self.owner._selection_label(reference)
        payload: Dict[str, Any] = {"anime_reference": reference}
        broadcast_playback = self.owner._broadcast_playback(self.selected_playback)
        if broadcast_playback:
            payload["playback"] = broadcast_playback
            payload["stream_url"] = broadcast_playback.get("url")
            payload["title"] = (
                broadcast_playback.get("title") or reference.get("title")
            )
            payload["episode_number"] = (
                broadcast_playback.get("episode_number")
                or reference.get("episode_number")
            )
            if broadcast_playback.get("headers"):
                payload["headers"] = broadcast_playback["headers"]
        try:
            broadcaster = getattr(self.client, "broadcast_playback", None)
            if broadcast_playback and callable(broadcaster):
                await broadcaster(broadcast_playback, reference)
            else:
                await self.client.send_event(
                    PartyEvent.EPISODE_CHANGED.value, payload
                )
        except Exception as exc:
            _, message, _ = self.owner._friendly_error(exc, "room")
            logger.warning("Watch Together selection broadcast failed: %s", message)
            self.pending_share = True
            self.set_notice(
                "Picked it. Will share when the connection returns."
                if self.owner.console.width < 56
                else f"Picked {label}, but couldn't share it yet. "
                "It will be shared automatically when the room reconnects."
            )
            return False
        self.pending_share = False
        self.set_notice(
            f"Shared: {label}"
            if self.owner.console.width < 56
            else f"Room pick shared: {label}."
        )
        return True

    async def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ------------------------------------------------------------------
    # View
    # ------------------------------------------------------------------
    def lobby_model(self) -> Optional[Dict[str, Any]]:
        lobby = self.lobby
        if lobby is None:
            return None
        now = time.monotonic()
        options = []
        for index, option in enumerate(lobby.options):
            options.append({
                "index": index,
                "mode": option.mode,
                "badge": language_badge(option.mode),
                "label": option.mode_label,
                "provider": option.provider_name,
                "highlighted": index == lobby.choice_index,
                "selected": option is lobby.selected,
            })
        members = []
        for member in lobby.members.values():
            members.append({
                "key": member.key,
                "name": member.name,
                "role": member.role,
                "is_you": member.is_you,
                "is_host": member.role == "host",
                "ready": member.ready,
                "mode": member.mode,
                "mode_label": member.mode_label,
                "marker": "✓" if member.ready else "⋯",
            })
        members.sort(key=lambda row: (
            0 if row["is_host"] else 1,
            0 if row["is_you"] else 1,
            row["name"].lower(),
        ))
        remaining = lobby.remaining(now)
        can_start = bool(
            self.role == "host"
            and not lobby.started
            and not self._lobby_snapshot_pending
            and lobby.can_start_anyway(now)
        )
        if lobby.started:
            status = "Starting playback on every player..."
        elif self.role == "host" and lobby.all_ready:
            status = "Everyone is ready. Starting..."
        elif self.role == "host":
            status = (
                "Waiting for all members to select language and get ready..."
            )
        elif lobby.ready:
            status = "You're ready. Waiting for the host to start..."
        elif lobby.discovering and not lobby.options:
            status = "Checking which streams work for this episode..."
        else:
            status = "Choose your language, then press Enter to get ready."
        return {
            "id": lobby.lobby_id,
            "title": lobby.reference.get("title", ""),
            "episode": lobby.reference.get("episode_number", ""),
            "episode_title": lobby.reference.get("episode_title", ""),
            "options": options,
            "members": members,
            "discovering": lobby.discovering,
            "ready": lobby.ready,
            "started": lobby.started,
            "error": lobby.error,
            "remaining": int(remaining + 0.999),
            "all_ready": lobby.all_ready,
            "can_start_anyway": can_start,
            "is_host": self.role == "host",
            "status": status,
        }

    def view_model(self, busy_override: str = "") -> Dict[str, Any]:
        return self.owner.room_view_model(
            self.client,
            state=self.state,
            state_error=self.state_error,
            notice=self.notice,
            leave_error=self.leave_error,
            initial_error=self.error,
            technical_error=getattr(self.client, "channel_error", None) or self.technical,
            show_diagnostics=self.show_diagnostics,
            busy=busy_override or self.busy,
            selection=self.selected_reference,
            spinner_frame=self.spinner_frame,
            lobby=self.lobby_model(),
        )

    def _view(self, busy_override: str = ""):
        return self.owner._build_room_view(
            self.view_model(busy_override), self.show_diagnostics
        )

    def _status_tuple(self) -> Tuple:
        return (
            bool(getattr(self.client, "connected", False)),
            bool(getattr(self.client, "channels_ready", False)),
            getattr(self.client, "channel_error", None),
            self.busy,
            self.show_diagnostics,
            self.error,
            self.leave_error,
            self.notice,
            len((self.state or {}).get("participants") or []),
            self.selected_reference,
            self.spinner_frame,
            self._lobby_signature(),
        )

    def _lobby_signature(self) -> Optional[Tuple]:
        lobby = self.lobby
        if lobby is None:
            return None
        return (
            lobby.lobby_id,
            lobby.discovering,
            lobby.ready,
            lobby.choice_index,
            lobby.started,
            lobby.error,
            tuple(
                (option.mode, option.provider_id)
                for option in lobby.options
            ),
            tuple(sorted(
                (key, member.ready, member.mode)
                for key, member in lobby.members.items()
            )),
            int(lobby.remaining() + 0.999),
            self._lobby_snapshot_pending,
        )

    async def run_ui(self) -> bool:
        """Foreground room screen. Returns True after a confirmed leave."""
        self._foreground = True
        self._dirty = True
        self.left = False
        self.quit_requested = False
        # Drop mouse/arrow bursts queued while menus were moving so they are
        # not replayed as room commands.
        flush_stdin()
        last_status = None
        last_size = (self.owner.console.width, self.owner.console.height)
        try:
            with RawTerminal():
                with Live(
                    self._view(),
                    console=self.owner.console,
                    screen=True,
                    auto_refresh=False,
                    refresh_per_second=10,
                ) as live:
                    while not self._closed:
                        size = (self.owner.console.width, self.owner.console.height)
                        status = self._status_tuple()
                        if self._dirty or status != last_status or size != last_size:
                            last_status = status
                            last_size = size
                            self._dirty = False
                            live.update(self._view(), refresh=True)

                        key = get_key()

                        if key in ("l", "L", "q", "Q"):
                            wants_quit = key in ("q", "Q")
                            self._pending_sync = None
                            await self._cancel_task(self._sync_task)
                            self._sync_task = None
                            await self._cancel_task(self._playback_task)
                            self._playback_task = None
                            self.busy = "Leaving the room..."
                            # Leave and teardown run side by side; neither is
                            # allowed to freeze the key loop on a dead socket.
                            stop_task = asyncio.create_task(self.playback.stop())
                            leave_task = asyncio.create_task(self.client.leave_room())
                            pending = {stop_task, leave_task}
                            timed_out = False
                            deadline = time.monotonic() + _LEAVE_TIMEOUT_SECONDS
                            frame = 0
                            while pending:
                                frame += 1
                                live.update(
                                    self._view("Leaving the room..."),
                                    refresh=True,
                                )
                                _, pending = await asyncio.wait(
                                    pending, timeout=0.08
                                )
                                if pending and time.monotonic() >= deadline:
                                    timed_out = True
                                    for task in pending:
                                        task.cancel()
                                    await asyncio.gather(
                                        *pending, return_exceptions=True
                                    )
                                    pending = set()
                            self.busy = ""
                            if stop_task.done() and not stop_task.cancelled():
                                try:
                                    stop_task.result()
                                except Exception:
                                    pass
                            leave_error = None
                            if leave_task.cancelled():
                                leave_error = (
                                    "The server didn't confirm the leave in time."
                                )
                            else:
                                try:
                                    leave_task.result()
                                except Exception as exc:
                                    _, message, technical = self.owner._friendly_error(
                                        exc, "leave"
                                    )
                                    leave_error = message
                                    self.technical = technical
                            stay = False
                            if leave_error is not None:
                                self.leave_error = leave_error
                                self.set_notice("")
                                self._dirty = True
                                # Quit and timed-out leaves exit locally: a
                                # dead server must never trap the guest.
                                stay = not (wants_quit or timed_out)
                            if not stay:
                                self.left = True
                                self.quit_requested = wants_quit
                                break

                        elif key in ("[", "]") and self.playback.active:
                            await self.adjust_stream_offset(
                                -_STREAM_OFFSET_STEP_SECONDS
                                if key == "["
                                else _STREAM_OFFSET_STEP_SECONDS
                            )

                        elif key == "0" and self.playback.active:
                            sync = self.playback.sync
                            current = float(
                                getattr(sync, "stream_time_offset", 0.0) or 0.0
                            )
                            await self.adjust_stream_offset(-current)

                        elif key in ("b", "B") and self.role == "host":
                            # Deliberately leave the runtime untouched: the room
                            # stays hosted while the main menu is in use.
                            break

                        elif (
                            self.lobby is not None
                            and not self.lobby.started
                            and key in ("UP", "k", "K")
                        ):
                            self.move_lobby_choice(-1)

                        elif (
                            self.lobby is not None
                            and not self.lobby.started
                            and key in ("DOWN", "j", "J")
                        ):
                            self.move_lobby_choice(1)

                        elif (
                            self.lobby is not None
                            and not self.lobby.started
                            and key == "ENTER"
                        ):
                            self._spawn(self.confirm_lobby_choice())

                        elif (
                            key in ("s", "S")
                            and self.lobby is not None
                            and not self.lobby.started
                            and self.role == "host"
                        ):
                            if self.lobby.can_start_anyway():
                                self._spawn(self.start_lobby())
                            else:
                                self.set_notice(
                                    "Start Anyway unlocks when everyone is ready "
                                    f"or in {max(1, int(self.lobby.remaining() + 0.999))}s."
                                )

                        elif key in ("r", "R"):
                            if getattr(self.client, "channels_ready", False):
                                if self._state_task is None:
                                    self.set_notice("Refreshing members...")
                                    self._state_task = asyncio.create_task(
                                        self.client.get_room_state()
                                    )
                                    self.next_refresh = (
                                        time.monotonic() + _STATE_REFRESH_SECONDS
                                    )
                                else:
                                    self.set_notice("Members are already refreshing.")
                            elif self._retry_task is None:
                                self.next_retry = time.monotonic()
                                self.set_notice("Reconnecting...")
                            else:
                                self.set_notice("Already reconnecting.")

                        elif key in ("c", "C"):
                            code = getattr(self.client, "room_code", None)
                            if code:
                                if self.owner._copy_to_clipboard(str(code)):
                                    self.set_notice("Room code copied.")
                                else:
                                    self.set_notice(
                                        "Copy isn't available here. Share the code manually."
                                    )
                            else:
                                self.set_notice("There is no room code to copy yet.")

                        elif key in ("i", "I"):
                            self.show_diagnostics = not self.show_diagnostics
                            self.set_notice("")

                        elif key == "ESC":
                            if self.show_diagnostics:
                                self.show_diagnostics = False
                                self._dirty = True
                            else:
                                if self.lobby is not None and not self.lobby.started:
                                    hint = (
                                        "Use Up/Down to choose a language, then "
                                        "press Enter to get ready."
                                    )
                                else:
                                    hint = (
                                        "Press [ / ] to nudge sync, [l] to leave, "
                                        "[i] for details, or [q] to quit."
                                    )
                                    if self.role == "host":
                                        hint = (
                                            "Press [b] for the main menu, [ / ] to nudge "
                                            "sync, [l] to leave, [i] for details, "
                                            "or [q] to quit."
                                        )
                                self.set_notice(hint)

                        await asyncio.sleep(_STATUS_POLL_SECONDS)
        finally:
            self._foreground = False
            self.owner._quit_requested = self.quit_requested
        return self.left

    # ------------------------------------------------------------------
    # Status snapshot (safe to read from the UI thread)
    # ------------------------------------------------------------------
    def _update_status(self) -> None:
        snapshot = {
            "room_code": getattr(self.client, "room_code", None),
            "room_id": getattr(self.client, "room_id", None),
            "role": str(getattr(self.client, "role", "guest") or "guest").lower(),
            "connected": bool(getattr(self.client, "connected", False)),
            "channels_ready": bool(getattr(self.client, "channels_ready", False)),
            "transport": getattr(self.client, "transport_kind", "unknown"),
        }
        with self._status_lock:
            self._status = snapshot

    def status(self) -> Dict[str, Any]:
        with self._status_lock:
            return dict(self._status)


class WatchTogetherSession:
    """A room runtime kept alive on its own asyncio loop thread."""

    def __init__(self, owner: "WatchTogetherUI", api):
        self.owner = owner
        self.api = api
        self.client: Optional[WTRClient] = None
        self.runtime: Optional[_RoomRuntime] = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="nyo-watchtogether",
            daemon=True,
        )
        self._closed = False
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    def submit(self, coro, timeout: Optional[float] = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    @property
    def active(self) -> bool:
        client = self.client
        return (
            not self._closed
            and self.runtime is not None
            and client is not None
            and bool(getattr(client, "room_active", False))
        )

    @property
    def has_room(self) -> bool:
        client = self.client
        return client is not None and bool(getattr(client, "room_active", False))

    def status(self) -> Dict[str, Any]:
        runtime = self.runtime
        if runtime is None:
            return {}
        return runtime.status()

    def connect(self, room_code: Optional[str] = None) -> Optional[BaseException]:
        try:
            self.submit(self._connect(room_code))
            return None
        except KeyboardInterrupt:
            self.shutdown(emergency=True)
            raise
        except Exception as exc:
            return exc

    async def _connect(self, room_code: Optional[str]) -> None:
        client = self.owner._client_factory(self.owner._nickname())
        self.client = client
        try:
            steps: List[Tuple[str, Callable[[], Awaitable[Any]]]] = [
                ("Connecting to Watch Together...", lambda: client.connect())
            ]
            if room_code:
                steps.append(
                    (
                        f"Joining room {room_code}...",
                        lambda: client.join_room(room_code),
                    )
                )
            else:
                steps.append(("Opening your room...", lambda: client.create_room()))
            await self.owner._run_steps(steps)
        finally:
            if getattr(client, "room_active", False):
                self.runtime = _RoomRuntime(self.owner, client)
                await self.runtime.start()
        if self.runtime is None:
            raise RuntimeError("Room did not open")

    def open_room(self) -> bool:
        runtime = self.runtime
        if runtime is None or self._closed:
            return True
        return bool(self.submit(runtime.run_ui()))

    def play_episode(
        self,
        playback: Dict[str, Any],
        reference: Dict[str, Any],
    ) -> bool:
        runtime = self.runtime
        if not self.active or runtime is None or runtime.role != "host":
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                runtime.play_episode(playback, reference), self._loop
            )
        except RuntimeError:
            return False
        future.add_done_callback(self._log_future)
        return True

    @staticmethod
    def _log_future(future) -> None:
        try:
            future.result()
        except Exception as exc:
            logger.warning("Watch Together playback handoff failed: %s", exc)

    def shutdown(self, emergency: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.submit(self._shutdown(emergency), timeout=25.0)
        except Exception:
            logger.warning("Watch Together shutdown was not clean", exc_info=True)
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        self._thread.join(timeout=5.0)

    async def _shutdown(self, emergency: bool) -> None:
        runtime = self.runtime
        if runtime is not None:
            await runtime.stop()
        client = self.client
        if client is None:
            return
        if getattr(client, "room_active", False):
            try:
                await asyncio.wait_for(
                    client.leave_room(),
                    timeout=6.0 if emergency else 12.0,
                )
            except Exception as exc:
                logger.warning("Watch Together leave during shutdown failed: %s", exc)
        try:
            await client.close()
        except Exception:
            pass


class WatchTogetherUI:
    """UI components and session ownership for Watch Together."""

    def __init__(
        self,
        console: Any,
        client_factory: Optional[Callable[[str], WTRClient]] = None,
        browse_callback: Optional[Callable[[Any], Optional[Dict[str, Any]]]] = None,
    ):
        # Accept either a Console (tests, embedders) or NyoAnime's UIManager so
        # the Watch Together screens can reuse the same logo and theme.
        self.ui = console if hasattr(console, "console") else None
        self.console: Console = getattr(console, "console", console)
        self._client_factory = client_factory or self._default_client_factory
        # The app injects its existing search/anime/episode menus here. The
        # callback receives the provider and returns an anime/episode dict (or
        # None when the user backs out), while this screen keeps the room alive.
        self._browse_callback = browse_callback
        self._api = None
        # Persistent room session. It survives returning to the main menu; only
        # Leave Room / Quit tears it down.
        self._session: Optional[WatchTogetherSession] = None
        self._session_lock = threading.RLock()
        # Set when the room screen is closed with [q].
        self._quit_requested = False
        _sync_theme_colors()

    # ------------------------------------------------------------------
    # Session entry point
    # ------------------------------------------------------------------
    def handle_watch_together(self, api, join_code: Optional[str] = None) -> None:
        """Open the room, or launch the host/join flow if there isn't one."""
        self._api = api
        self._quit_requested = False

        with self._session_lock:
            existing = self._session
            if existing is not None and existing.active:
                self._open_room()
                return
            if existing is not None:
                self._session = None
                existing.shutdown()

        pending_code = (join_code or "").strip() or None
        notice = ""
        while True:
            if pending_code:
                # --join-room: skip the menu and connect with this code.
                choice, code = "join", pending_code
                pending_code = None
            else:
                choice = self.watch_together_menu(notice=notice)
                notice = ""
                if choice not in ("host", "join"):
                    return
                code = None
                if choice == "join":
                    code = self._code_entry_screen()
                    if not code:
                        continue

            session = WatchTogetherSession(self, api)
            error = session.connect(code)
            if error is None:
                self._session = session
                self._open_room(notice=(
                    "You're in. Waiting for the host to start."
                    if code
                    else "Your room is ready. Share the code with friends."
                ))
                return

            action = "join" if code else "create"
            title, message, technical = self._friendly_error(error, action)
            if session.has_room:
                # The room exists; only live updates failed. Keep it viewable
                # so the user can share the code or retry from inside.
                self._session = session
                self._open_room(
                    initial_error=message,
                    technical_error=technical,
                    notice=(
                        "You're in the room."
                        if code
                        else "Your room code is ready. Share it with friends."
                    ),
                )
                return

            session.shutdown(emergency=True)
            logger.warning(
                "Watch Together %s failed: %s",
                action,
                sanitize_diagnostic(str(error)),
            )
            if code and self._is_code_error(error):
                notice = message
                continue
            if self._error_screen(
                title, message, retry=self._can_retry(error)
            ) != "retry":
                return

    # ------------------------------------------------------------------
    # Persistent session bridge
    # ------------------------------------------------------------------
    def room_status(self) -> Optional[Dict[str, Any]]:
        """Snapshot used by the main-menu banner and the return shortcut."""
        session = self._session
        if session is None or not session.active:
            return None
        status = session.status()
        return status if status.get("room_code") else None

    def play_from_menu(
        self,
        playback: Dict[str, Any],
        reference: Dict[str, Any],
    ) -> bool:
        """Link a main-menu episode to the room (host only).

        Opens the lobby immediately: the host has to pick a language and get
        ready just like everyone else before playback can start.
        """
        session = self._session
        if session is None:
            return False
        if not session.play_episode(playback, reference):
            return False
        self._open_room()
        return True

    def shutdown(self) -> None:
        with self._session_lock:
            session, self._session = self._session, None
        if session is not None:
            session.shutdown()

    def _open_room(self, **kwargs) -> None:
        session = self._session
        if session is None or session.runtime is None:
            return
        session.runtime.configure(**kwargs)
        try:
            left = session.open_room()
        except KeyboardInterrupt:
            session.shutdown(emergency=True)
            if self._session is session:
                self._session = None
            raise
        except Exception:
            logger.error("Watch Together room screen failed", exc_info=True)
            session.shutdown(emergency=True)
            if self._session is session:
                self._session = None
            raise
        if left:
            session.shutdown()
            if self._session is session:
                self._session = None

    # ------------------------------------------------------------------
    # Menus and shared rendering
    # ------------------------------------------------------------------
    def watch_together_menu(self, notice: str = "") -> str:
        name = self._nickname()
        options = [
            {
                "label": "Host a room",
                "description": "Create a room and share its code.",
                "action": "host",
            },
            {
                "label": "Join a room",
                "description": "Enter a friend's six-digit code.",
                "action": "join",
            },
            {
                "label": "Back",
                "description": "Return to the main menu.",
                "action": "back",
            },
        ]
        return self._choice_screen(
            badge="Watch Together",
            subtitle="Watch the same episode with friends.",
            options=options,
            notice=notice,
            footer=f"You'll appear as {name}.",
            panel_title="What would you like to do?",
        )

    def _error_screen(self, title: str, message: str, retry: bool = True) -> str:
        options = []
        if retry:
            options.append({
                "label": "Try again",
                "description": "Retry the connection.",
                "action": "retry",
            })
        options.append({
            "label": "Back",
            "description": "Return to Watch Together.",
            "action": "back",
        })
        return self._choice_screen(
            badge="Watch Together",
            subtitle="",
            options=options,
            message=message,
            panel_title=title,
            border_style=COLOR_ERROR,
        )

    def _choice_screen(
        self,
        badge: str,
        subtitle: str,
        options: Sequence[Dict[str, str]],
        *,
        notice: str = "",
        footer: str = "",
        message: str = "",
        panel_title: Optional[str] = None,
        border_style: Optional[str] = None,
        selected: int = 0,
    ) -> str:
        if not options:
            return "back"

        selected = max(0, min(selected, len(options) - 1))
        border_style = border_style or COLOR_BORDER

        def generate_renderable():
            box_width = self._panel_width(72 if message else 64)
            inner_width = max(16, box_width - 6)
            compact = inner_width < 48

            # Align.center propagates its justify into nested panels; force
            # left alignment so menu rows read like a list, not centered pills.
            content = Text(justify="left")
            if message:
                content.append(message.strip() + "\n\n", style="white")
            for idx, option in enumerate(options):
                if idx:
                    content.append("\n")
                content.append_text(
                    self._option_row(
                        option, idx == selected, inner_width, compact
                    )
                )

            title_text = (
                self._truncate(panel_title, max(8, box_width - 8))
                if panel_title else None
            )
            panel = Panel(
                content,
                title=(
                    f"[bold {border_style}]{title_text}[/bold {border_style}]"
                    if title_text else None
                ),
                box=HEAVY,
                border_style=border_style,
                padding=(1, 2),
                width=box_width,
            )

            notice_text = (
                Text(f"› {notice}", style=f"bold {COLOR_TITLE}", justify="center")
                if notice else None
            )
            footer_text = (
                Text(footer, style=f"dim {COLOR_SECONDARY_TEXT}", justify="center")
                if footer else None
            )
            narrow = self.console.width < 52
            dock = self._dock(
                [
                    ("↑↓" if narrow else "↑↓/jk", "move", COLOR_TITLE),
                    ("↵", "select", COLOR_TITLE),
                    ("b", "back", COLOR_ACCENT),
                ],
                separator=" · " if narrow else "   •   ",
            )

            return self._stack(
                self._header(badge, subtitle),
                None,
                panel,
                None,
                notice_text,
                footer_text,
                None,
                dock,
            )

        self.clear()
        with RawTerminal():
            with Live(
                generate_renderable(),
                console=self.console,
                screen=True,
                auto_refresh=False,
                refresh_per_second=10,
            ) as live:
                while True:
                    key = get_key()
                    if key is None:
                        time.sleep(0.02)
                        continue

                    if key in ("UP", "k", "K") and selected > 0:
                        selected -= 1
                        live.update(generate_renderable(), refresh=True)
                    elif key in ("DOWN", "j", "J") and selected < len(options) - 1:
                        selected += 1
                        live.update(generate_renderable(), refresh=True)
                    elif key == "ENTER":
                        return options[selected].get("action", "back")
                    elif key in ("b", "B", "q", "Q", "ESC", "LEFT"):
                        return "back"
                    elif key and key.isdigit():
                        index = int(key) - 1
                        if 0 <= index < len(options):
                            return options[index].get("action", "back")

    def _option_row(
        self,
        option: Dict[str, str],
        selected: bool,
        width: int,
        compact: bool,
    ) -> Text:
        label = self._truncate(option.get("label", ""), width - 3)
        description = option.get("description", "") if not compact else ""
        lines = [f"▶  {label}" if selected else f"   {label}"]
        if description:
            lines.append(f"   {self._truncate(description, width - 3)}")

        row = Text()
        for idx, line in enumerate(lines):
            if idx:
                row.append("\n")
            padded = line + " " * max(0, width - cell_len(line))
            if selected:
                row.append(padded, style=f"bold {COLOR_HIGHLIGHT_FG} on {COLOR_HIGHLIGHT_BG}")
            elif idx == 0:
                row.append(padded, style="white")
            else:
                row.append(padded, style=f"dim {COLOR_SECONDARY_TEXT}")
        return row

    def _header(self, badge: str, subtitle: str = ""):
        table = Table.grid(expand=False)
        table.add_column(justify="center")
        if self.console.height >= 30:
            table.add_row(Align.center(self._logo()))
            table.add_row(Text(""))

        badge_text = Text()
        badge_text.append("[ ", style=f"bold {COLOR_BORDER}")
        badge_text.append(badge, style="bold white")
        badge_text.append(" ]", style=f"bold {COLOR_BORDER}")
        table.add_row(Align.center(badge_text))

        if subtitle:
            subtitle = self._truncate(subtitle, max(10, self.console.width - 4))
            table.add_row(Align.center(Text(
                subtitle, style=f"dim {COLOR_SECONDARY_TEXT}", justify="center"
            )))
        return table

    def _logo(self, frame_idx: int = 0):
        if self.ui is not None and hasattr(self.ui, "_get_logo"):
            try:
                logo = self.ui._get_logo(frame_idx)
                if self.console.measure(logo).maximum <= self.console.width - 2:
                    return logo
            except Exception:
                pass
        return Text("NyoAnime", style=f"bold {COLOR_TITLE}")

    def _stack(self, *items):
        table = Table.grid(expand=False)
        table.add_column(justify="center")
        for item in items:
            if item is None:
                table.add_row(Text(""))
            else:
                table.add_row(Align.center(item))
        return Align.center(table, vertical="middle", height=self.console.height)

    def _dock(
        self,
        entries: Sequence[Tuple[str, str, str]],
        separator: str = "   •   ",
    ) -> Text:
        dock = Text()
        line_width = 0
        limit = max(16, self.console.width - 4)
        for idx, (key, label, color) in enumerate(entries):
            segment_len = cell_len(f"[{key}] {label}")
            if idx:
                if line_width + cell_len(separator) + segment_len > limit:
                    dock.append("\n")
                    line_width = 0
                else:
                    dock.append(separator, style=f"bold {COLOR_BORDER}")
                    line_width += cell_len(separator)
            dock.append("[", style=f"bold {COLOR_BORDER}")
            dock.append(key, style=f"bold {color}")
            dock.append("]", style=f"bold {COLOR_BORDER}")
            dock.append(f" {label}", style="white")
            line_width += segment_len
        return dock

    def _panel_width(self, maximum: int = 72) -> int:
        return max(24, min(maximum, self.console.width - 4))

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if limit <= 0:
            return ""
        text = str(text)
        if cell_len(text) <= limit:
            return text
        if limit <= 1:
            return "…"
        out = ""
        for ch in text:
            if cell_len(out + ch) > limit - 1:
                break
            out += ch
        return out + "…"

    # ------------------------------------------------------------------
    # Identity and client construction
    # ------------------------------------------------------------------
    def _nickname(self) -> str:
        try:
            raw = os.environ.get("NYOANIME_WATCH_TOGETHER_NAME") or getpass.getuser()
        except Exception:
            raw = ""
        name = " ".join(str(raw or "").split())
        name = re.sub(r"[\x00-\x1f]", "", name).strip()
        if len(name) > 32:
            name = name[:32].rstrip()
        return name or "Guest"

    def _default_client_factory(self, nickname: str) -> WTRClient:
        return WTRClient(self._get_relay_url(), nickname=nickname)

    def _get_relay_url(self) -> str:
        # Prefer Supabase Realtime WebSocket if configured, otherwise fallback to a public relay
        from .party.supabase_config import use_supabase_transport
        if use_supabase_transport():
            # When using Supabase transport, we should be connected via the Supabase client
            # This method is called for the WebSocket relay fallback, so we need to handle this
            # appropriately. For now, return a placeholder that will be overridden when
            # Supabase transport is used, but we need to ensure this doesn't cause issues.
            # We return a placeholder that indicates Supabase transport should be used
            return "wss://mxacanxpjkqeudlwzhwr.supabase.co/realtime/v1/ws"
        # For backward compatibility, default to local WebSocket relay
        return "ws://localhost:8765"

    # ------------------------------------------------------------------
    # Join code entry
    # ------------------------------------------------------------------
    def _code_entry_screen(self, initial_code: str = "", notice: str = "") -> Optional[str]:
        code = "".join(ch for ch in initial_code if ch.isdigit())[:_CODE_LEN]
        message = notice
        name = self._nickname()

        def generate_renderable():
            box_width = self._panel_width(54)
            inner_width = max(16, box_width - 6)
            gap = "   " if inner_width >= 34 else "  " if inner_width >= 26 else " "
            rule_width = min(
                3, max(1, (inner_width - 5 * cell_len(gap)) // _CODE_LEN)
            )
            rule = "─" * rule_width
            digits = Text(justify="center")
            rules = Text(justify="center")
            for idx in range(_CODE_LEN):
                if idx:
                    digits.append(gap)
                    rules.append(gap)
                if idx < len(code):
                    digits.append(code[idx], style=f"bold {COLOR_TITLE}")
                else:
                    digits.append("·", style=f"dim {COLOR_SECONDARY_TEXT}")
                rules.append(rule, style=f"dim {COLOR_BORDER}")

            body = Text(justify="center")
            body.append("ROOM CODE\n\n", style=f"bold {COLOR_SUBTITLE}")
            body.append_text(digits)
            body.append("\n")
            body.append_text(rules)
            body.append("\n\n")

            if message:
                body.append(message, style=f"bold {COLOR_ERROR}")
            elif len(code) == _CODE_LEN:
                body.append(
                    "Press Enter to join.",
                    style=f"bold {COLOR_SUCCESS}",
                )
            else:
                body.append(
                    "Type the host's six-digit code.",
                    style=f"dim {COLOR_SECONDARY_TEXT}",
                )

            panel = Panel(
                body,
                title=f"[bold {COLOR_TITLE}]Join a room[/bold {COLOR_TITLE}]",
                box=HEAVY,
                border_style=COLOR_BORDER,
                padding=(0, 2) if self.console.height < 24 else (1, 2),
                width=box_width,
            )
            dock = self._dock(
                [
                    ("0-9", "type", COLOR_TITLE),
                    ("⌫", "delete", COLOR_TITLE),
                    ("↵", "join", COLOR_TITLE),
                    ("b", "back", COLOR_ACCENT),
                ],
                separator=" · " if self.console.width < 70 else "   •   ",
            )
            return self._stack(
                self._header(
                    "Watch Together",
                    "Enter the host's six-digit code.",
                ),
                None,
                panel,
                None,
                Text(f"You'll join as {name}.", style=f"dim {COLOR_SECONDARY_TEXT}", justify="center"),
                None,
                dock,
            )

        self.clear()
        with RawTerminal():
            with Live(
                generate_renderable(),
                console=self.console,
                screen=True,
                auto_refresh=False,
                refresh_per_second=10,
            ) as live:
                while True:
                    key = get_key()
                    if key is None:
                        time.sleep(0.02)
                        continue

                    changed = False
                    if key in ("ESC", "b", "B", "q", "Q", "LEFT"):
                        return None
                    if key == "ENTER":
                        if len(code) == _CODE_LEN:
                            return code
                        message = "Room codes are six digits."
                        changed = True
                    elif key == "BACKSPACE":
                        if code:
                            code = code[:-1]
                        message = ""
                        changed = True
                    elif key:
                        digits = "".join(ch for ch in str(key) if ch.isdigit())
                        if digits:
                            code = (code + digits)[:_CODE_LEN]
                            message = ""
                            changed = True

                    if changed:
                        live.update(generate_renderable(), refresh=True)

    # ------------------------------------------------------------------
    # Create / join flows
    # ------------------------------------------------------------------
    async def _guard_room_screen(
        self,
        client: WTRClient,
        initial_error: Optional[str] = None,
        technical_error: Optional[str] = None,
        notice: Optional[str] = None,
    ) -> bool:
        """Always attempt a server-side leave before dropping the client."""
        try:
            return await self._room_screen(
                client,
                initial_error=initial_error,
                technical_error=technical_error,
                notice=notice,
            )
        except KeyboardInterrupt:
            await self._emergency_leave(client)
            raise
        except Exception:
            await self._emergency_leave(client)
            raise
        finally:
            await self._safe_close(client)

    async def _safe_close(self, client: WTRClient) -> None:
        try:
            await client.close()
        except Exception:
            pass

    async def _emergency_leave(self, client: WTRClient) -> None:
        if not getattr(client, "room_active", False):
            return
        try:
            await asyncio.wait_for(client.leave_room(), timeout=12.0)
        except Exception as exc:
            logger.warning("Emergency leave failed: %s", exc)
            body = Text(justify="center")
            body.append(
                "We couldn't confirm that you left the room.\n\n",
                style="bold white",
            )
            body.append(
                "Open Watch Together again and use Leave room to try again.",
                style="white",
            )
            self.clear()
            self.console.print(self._stack(
                self._header("Watch Together"),
                None,
                Panel(
                    body,
                    title=f"[bold {COLOR_ERROR}]Room may still be open[/bold {COLOR_ERROR}]",
                    box=HEAVY,
                    border_style=COLOR_ERROR,
                    padding=(1, 2),
                    width=self._panel_width(60),
                ),
            ))
            self._pause("Press any key to continue...")

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    async def _run_steps(
        self,
        steps: Sequence[Tuple[str, Callable[[], Awaitable[Any]]]],
    ) -> Any:
        if not steps:
            return None

        index = 0
        frame = 0
        task = asyncio.ensure_future(steps[0][1]())
        try:
            with Live(
                self._loading_view(steps[0][0], frame),
                console=self.console,
                screen=True,
                refresh_per_second=15,
                transient=True,
            ) as live:
                while True:
                    if task.done():
                        result = task.result()
                        index += 1
                        if index >= len(steps):
                            return result
                        task = asyncio.ensure_future(steps[index][1]())

                    frame += 1
                    live.update(
                        self._loading_view(steps[index][0], frame),
                        refresh=True,
                    )
                    await asyncio.sleep(0.07)
        except BaseException:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        finally:
            self.console.clear()

    def _loading_view(self, message: str, frame: int):
        frames = BOUNCING_BAR_FRAMES or ["[...]"]
        bar = Text(frames[frame % len(frames)], style=f"bold {COLOR_BOLT}")

        body = Table.grid(expand=False)
        body.add_column(justify="center")
        body.add_row(Align.center(bar))
        body.add_row(Text(""))
        body.add_row(Align.center(Text(message, style="bold white")))
        body.add_row(Align.center(Text(
            "This only takes a moment.",
            style=f"dim {COLOR_SECONDARY_TEXT}",
        )))

        panel = Panel(
            body,
            title=f"[bold {COLOR_TITLE}]Watch Together[/bold {COLOR_TITLE}]",
            box=HEAVY,
            border_style=COLOR_BORDER,
            padding=(1, 4),
            width=self._panel_width(58),
        )
        return self._stack(
            self._header("Watch Together", "Watch the same episode with friends."),
            None,
            panel,
        )

    # ------------------------------------------------------------------
    # Persistent room screen
    # ------------------------------------------------------------------
    async def _room_screen(
        self,
        client: WTRClient,
        initial_error: Optional[str] = None,
        technical_error: Optional[str] = None,
        notice: Optional[str] = None,
        runtime: Optional[_RoomRuntime] = None,
    ) -> bool:
        """Render the room. ``False`` means "back to menu" without leaving."""
        owns_runtime = runtime is None
        if runtime is None:
            runtime = _RoomRuntime(
                self,
                client,
                initial_error=initial_error,
                technical_error=technical_error,
                notice=notice,
            )
            await runtime.start()
        else:
            runtime.configure(
                initial_error=initial_error,
                technical_error=technical_error,
                notice=notice,
            )
        try:
            return await runtime.run_ui()
        finally:
            if owns_runtime:
                await runtime.stop()

    # ------------------------------------------------------------------
    # Room view model
    # ------------------------------------------------------------------
    def room_view_model(
        self,
        client: Any,
        state: Optional[Dict[str, Any]] = None,
        state_error: Optional[str] = None,
        notice: str = "",
        leave_error: Optional[str] = None,
        initial_error: Optional[str] = None,
        technical_error: Optional[str] = None,
        show_diagnostics: bool = False,
        busy: str = "",
        selection: Optional[Dict[str, Any]] = None,
        spinner_frame: int = 0,
        lobby: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        role = str(getattr(client, "role", "guest") or "guest").lower()
        role_label = "HOST" if role == "host" else "GUEST"
        room_code = getattr(client, "room_code", None) or "—"
        room_id = getattr(client, "room_id", None)
        transport_kind = getattr(client, "transport_kind", "unknown")
        connected = bool(getattr(client, "connected", False))
        channels_ready = bool(getattr(client, "channels_ready", False))
        channels = getattr(client, "channel_status", {}) or {}
        channel_states = getattr(client, "channel_states", {}) or {}
        channel_error = getattr(client, "channel_error", None)
        participant_rows = self._participant_rows(client, state)
        participants = [
            f"{row['name']} ({row['role']})" for row in participant_rows
        ]

        reconnecting = False
        if busy:
            status = "CONNECTING"
            status_label = busy
            status_kind = "pending"
        elif channels_ready:
            status = "CONNECTED"
            status_label = "Connected"
            status_kind = "ok"
        elif room_code != "—" or room_id:
            # A dropped socket is recoverable: show a quiet spinner instead of
            # taking over the screen or repainting the whole panel as an error.
            status = "RECONNECTING"
            status_label = "Reconnecting…"
            status_kind = "pending"
            reconnecting = True
        else:
            status = "NOT CONNECTED"
            status_label = "Not connected"
            status_kind = "error"

        error = leave_error or initial_error
        you = next((row for row in participant_rows if row["is_you"]), None)
        you_name = you["name"] if you else getattr(client, "nickname", "")
        host_name = next(
            (row["name"] for row in participant_rows if row["role"] == "host"),
            "",
        )
        if not host_name and role == "host":
            host_name = you_name
        code_display = (
            " ".join(str(room_code)) if str(room_code).isdigit() else str(room_code)
        )
        share_hint = (
            "Share this code so friends can join."
            if room_code != "—"
            else "Waiting for the room code..."
        )

        chosen = selection if isinstance(selection, dict) else {}
        chosen_title = str(chosen.get("title") or "").strip()
        chosen_episode = str(chosen.get("episode_number") or "").strip()
        chosen_episode_title = str(chosen.get("episode_title") or "").strip()

        diagnostics = {
            "transport": transport_kind,
            "room_id": room_id,
            "channels": dict(channels) if isinstance(channels, dict) else {},
            "channel_states": (
                dict(channel_states) if isinstance(channel_states, dict) else {}
            ),
            "channel_error": channel_error,
            "state_error": state_error,
            "technical_error": technical_error,
            "room_state_loaded": bool(state and state.get("participants")),
        }

        return {
            "role": role_label,
            "role_text": "You're hosting" if role == "host" else "You're a guest",
            "is_host": role == "host",
            "room_code": room_code,
            "room_code_display": code_display,
            "room_id": room_id,
            "transport_kind": transport_kind,
            "connected": connected,
            "channels_ready": channels_ready,
            "channels": diagnostics["channels"],
            "channel_error": channel_error,
            "status": status,
            "status_label": status_label,
            "status_kind": status_kind,
            "reconnecting": reconnecting,
            "spinner": _SPINNER_FRAMES[spinner_frame % len(_SPINNER_FRAMES)],
            "participants": participants,
            "participant_rows": participant_rows,
            "participant_count": len(participant_rows),
            "host_name": host_name,
            "you_name": you_name,
            "share_hint": share_hint,
            "state_error": state_error,
            "notice": notice,
            "leave_error": leave_error,
            "initial_error": initial_error,
            "error": error,
            "technical_error": technical_error,
            "show_diagnostics": show_diagnostics,
            "diagnostics": diagnostics,
            "busy": busy,
            "selection": chosen or None,
            "selection_title": chosen_title,
            "selection_episode": chosen_episode,
            "selection_episode_title": chosen_episode_title,
            "lobby": lobby,
        }

    def _participant_rows(
        self, client: Any, state: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        you: Dict[str, Any] = {}

        if isinstance(state, dict):
            raw_you = state.get("you")
            if isinstance(raw_you, dict):
                you = raw_you
            raw = state.get("participants")
            if isinstance(raw, list):
                candidates = [row for row in raw if isinstance(row, dict)]
            elif isinstance(raw, dict):
                for value in raw.values():
                    if isinstance(value, dict):
                        candidates.append(value)
                    elif isinstance(value, list):
                        candidates.extend(
                            item for item in value if isinstance(item, dict)
                        )

        if not you and hasattr(client, "local_room_state"):
            try:
                local = client.local_room_state()
                if isinstance(local, dict) and isinstance(local.get("you"), dict):
                    you = local["you"]
                    if not candidates and isinstance(local.get("participants"), list):
                        candidates = [
                            row for row in local["participants"]
                            if isinstance(row, dict)
                        ]
            except Exception:
                pass

        you_pid = str(you.get("participant_id") or getattr(client, "participant_id", "") or "")
        you_uid = str(you.get("user_id") or "")
        you_name = str(
            you.get("nickname") or getattr(client, "nickname", "") or "You"
        )
        you_role = str(
            you.get("role") or getattr(client, "role", "guest") or "guest"
        ).lower()
        if you_role not in ("host", "guest"):
            you_role = "guest"

        rows: List[Dict[str, Any]] = []
        seen_you = False
        for row in candidates:
            name = str(row.get("nickname") or row.get("name") or "Guest")
            role = str(row.get("role") or "guest").lower()
            if role not in ("host", "guest"):
                role = "guest"
            row_pid = str(row.get("id") or row.get("participant_id") or "")
            row_uid = str(row.get("user_id") or "")
            is_you = bool(
                (you_pid and row_pid == you_pid)
                or (you_uid and row_uid == you_uid)
                or (not you_pid and not you_uid and name == you_name and not seen_you)
            )
            seen_you = seen_you or is_you
            rows.append({
                "name": name,
                "role": role,
                "is_you": is_you,
                "is_host": role == "host",
            })

        if not any(row["is_you"] for row in rows):
            rows.append({
                "name": you_name,
                "role": you_role,
                "is_you": True,
                "is_host": you_role == "host",
            })

        rows.sort(key=lambda row: (
            0 if row["role"] == "host" else 1,
            0 if row["is_you"] else 1,
            row["name"].lower(),
        ))
        return rows

    # ------------------------------------------------------------------
    # Room rendering
    # ------------------------------------------------------------------
    def _build_room_view(self, model: Dict[str, Any], show_diagnostics: bool = False):
        narrow = self.console.width < 56
        very_tight = self.console.height < 24
        if model["busy"]:
            subtitle = ""
        elif model["status_kind"] != "ok":
            subtitle = (
                "Reconnecting. You can leave."
                if narrow
                else "Trying to reconnect. You can leave at any time."
            )
        else:
            subtitle = (
                "Keep this screen open."
                if narrow
                else "Keep this screen open to stay in the room."
            )
        if very_tight:
            subtitle = ""
        if self.console.width < 36:
            footer_text = "Stay here to keep it open."
        elif narrow:
            footer_text = "Stay here to keep the room open."
        else:
            footer_text = "Room stays open while this screen is open."
        footer = Text(
            footer_text,
            style=f"dim {COLOR_SECONDARY_TEXT}",
            justify="center",
        )
        header = self._header("Watch Together", subtitle)
        panel = self._build_room_panel(model, show_diagnostics=show_diagnostics)
        if very_tight:
            return self._stack(header, panel)
        if self.console.height < 32:
            return self._stack(header, panel, footer)
        return self._stack(header, None, panel, None, footer)

    def _build_room_panel(
        self, model: Dict[str, Any], show_diagnostics: bool = False
    ) -> Panel:
        box_width = self._panel_width(76)
        inner_width = max(20, box_width - 6)
        narrow = self.console.width < 56
        compact = self.console.height < 24
        content = Table.grid(expand=True)
        content.add_column(justify="left")

        # Role and connection status on one calm line.
        left = Text()
        if model["is_host"]:
            left.append("● ", style=f"bold {COLOR_SUCCESS}")
            left.append(model["role_text"], style="bold white")
        else:
            left.append("○ ", style=f"bold {COLOR_PROMPT}")
            left.append(model["role_text"], style="bold white")

        right = Text()
        status_color = {
            "ok": COLOR_SUCCESS,
            "pending": COLOR_TITLE,
            "error": COLOR_ERROR,
        }.get(model["status_kind"], COLOR_SECONDARY_TEXT)
        marker = (
            model.get("spinner", "◌")
            if model.get("reconnecting")
            else ("●" if model["status_kind"] == "ok" else "◌")
        )
        right.append(f"{marker} ", style=f"bold {status_color}")
        right.append(model["status_label"], style=f"bold {status_color}")

        status_row = Table.grid(expand=True)
        if inner_width >= 44:
            status_row.add_column(justify="left", ratio=1)
            status_row.add_column(justify="right")
            status_row.add_row(left, right)
        else:
            # Two columns squeeze the role on narrow terminals; stack instead.
            status_row.add_column(justify="left")
            status_row.add_row(left)
            status_row.add_row(right)
        content.add_row(status_row)

        if not (compact and model["selection_title"]):
            content.add_row(Text(""))
        content.add_row(Text(
            "ROOM CODE",
            style=f"bold {COLOR_SUBTITLE}",
            justify="center",
        ))
        content.add_row(Text(
            model["room_code_display"],
            style=f"bold {COLOR_TITLE}",
            justify="center",
        ))

        host_line = Text(justify="center")
        host_line.append("HOST  ", style=f"bold {COLOR_SUBTITLE}")
        host_line.append(
            model["host_name"] or "Waiting for the host",
            style="bold white",
        )
        content.add_row(host_line)

        if not compact:
            share_hint = model["share_hint"]
            if self.console.width < 36 and model["room_code"] != "—":
                share_hint = "Share this code."
            elif narrow and model["room_code"] != "—":
                share_hint = "Share this code with friends."
            content.add_row(Text(
                share_hint,
                style=f"dim {COLOR_SECONDARY_TEXT}",
                justify="center",
            ))
            content.add_row(Text(""))

        if model["selection_title"]:
            bar = Text()
            bar.append("▎ ", style=f"bold {COLOR_ACCENT}")
            bar.append("NOW WATCHING", style=f"bold {COLOR_SUBTITLE}")
            content.add_row(bar)
            content.add_row(Text(
                self._truncate(model["selection_title"], max(10, inner_width)),
                style="bold white",
            ))
            detail = ""
            if model["selection_episode"]:
                detail = f"Episode {model['selection_episode']}"
            if model["selection_episode_title"]:
                suffix = self._truncate(
                    model["selection_episode_title"], max(8, inner_width - len(detail) - 3)
                )
                detail = f"{detail} — {suffix}" if detail else suffix
            if detail:
                content.add_row(Text(detail, style=f"bold {COLOR_TITLE}"))
            if not model["is_host"] and not compact:
                content.add_row(Text(
                    "Chosen by the host.",
                    style=f"dim {COLOR_SECONDARY_TEXT}",
                ))
            if not compact and self.console.height >= 32:
                content.add_row(Text(""))
        elif not model["is_host"] and not compact:
            content.add_row(Text(
                "The host hasn't picked an episode yet.",
                style=f"dim {COLOR_SECONDARY_TEXT}",
            ))
            content.add_row(Text(""))

        content.add_row(self._divider(inner_width))

        if show_diagnostics:
            content.add_row(self._diagnostics_view(model, inner_width))
        elif model.get("lobby"):
            content.add_row(
                self._lobby_view(model, model["lobby"], inner_width, compact=compact)
            )
        else:
            content.add_row(
                self._members_view(model, inner_width, compact=compact)
            )

        content.add_row(self._divider(inner_width))

        if model["error"]:
            content.add_row(Text(
                f"! {model['error']}",
                style=f"bold {COLOR_ERROR}",
            ))
            content.add_row(Text(""))
        if model["notice"]:
            content.add_row(Text(
                f"› {model['notice']}",
                style=f"bold {COLOR_TITLE}",
            ))
            if self.console.height >= 32:
                content.add_row(Text(""))

        content.add_row(self._actions_text(model, inner_width, show_diagnostics))

        border = COLOR_ERROR if model["error"] else COLOR_BORDER
        title = "HOSTING ROOM" if model["is_host"] else "JOINED ROOM"
        title_color = border if model["error"] else COLOR_TITLE
        return Panel(
            content,
            title=f"[bold {title_color}]{title}[/bold {title_color}]",
            box=HEAVY,
            border_style=border,
            padding=(0, 2) if self.console.height < 32 else (1, 2),
            width=box_width,
        )

    def _lobby_view(
        self,
        model: Dict[str, Any],
        lobby: Dict[str, Any],
        width: int,
        compact: bool = False,
    ) -> Table:
        table = Table.grid(expand=True)
        table.add_column(justify="left")

        header = Text()
        header.append("LOBBY", style=f"bold {COLOR_SUBTITLE}")
        if lobby["discovering"]:
            header.append("   ")
            header.append(
                f"{model.get('spinner', '◌')} checking streams…",
                style=f"bold {COLOR_TITLE}",
            )
        table.add_row(header)
        table.add_row(Text(lobby["status"], style="bold white"))

        if lobby["started"]:
            table.add_row(Text(""))
            return table

        options = lobby["options"]
        if not options and lobby["discovering"]:
            table.add_row(Text(
                "Looking for playable streams on this device...",
                style=f"dim {COLOR_SECONDARY_TEXT}",
            ))
        elif not options:
            table.add_row(Text(
                "No playable stream was verified for this episode.",
                style=f"bold {COLOR_ERROR}",
            ))
        else:
            table.add_row(Text(""))
            for option in options:
                line = Text()
                line.append(
                    "› " if option["highlighted"] else "  ",
                    style=f"bold {COLOR_ACCENT}",
                )
                line.append(
                    f"[{option['badge']}] ",
                    style=f"bold {COLOR_TITLE}",
                )
                line.append(
                    self._truncate(
                        option["label"], max(10, width - len(option["provider"]) - 16)
                    ),
                    style="bold white" if option["highlighted"] else "white",
                )
                if option["provider"] and option["provider"] not in option["label"]:
                    line.append(
                        f"  {option['provider']}",
                        style=f"dim {COLOR_SECONDARY_TEXT}",
                    )
                if option["selected"]:
                    line.append("  ✓", style=f"bold {COLOR_SUCCESS}")
                table.add_row(line)

        if not compact:
            table.add_row(Text(""))
        table.add_row(Text("READY CHECK", style=f"bold {COLOR_SUBTITLE}"))
        if not lobby["members"]:
            table.add_row(Text(
                "Loading members...",
                style=f"dim {COLOR_SECONDARY_TEXT}",
            ))
        for member in lobby["members"]:
            line = Text()
            line.append(
                f"[{member['marker']}] ",
                style=f"bold {COLOR_SUCCESS if member['ready'] else COLOR_TITLE}",
            )
            line.append(
                self._truncate(member["name"], max(8, width - 30)),
                style="bold white" if member["is_you"] else "white",
            )
            if member["is_you"]:
                line.append("  You", style=f"bold {COLOR_TITLE}")
            if member["is_host"]:
                line.append("  [Host]", style=f"bold {COLOR_SUCCESS}")
            if member["ready"]:
                line.append(
                    f"  ({member['mode_label'] or 'Ready'})",
                    style=f"dim {COLOR_SECONDARY_TEXT}",
                )
            else:
                line.append(
                    "  (Selecting...)",
                    style=f"dim {COLOR_SECONDARY_TEXT}",
                )
            table.add_row(line)

        if model["is_host"] and not lobby["all_ready"]:
            if lobby["can_start_anyway"]:
                table.add_row(Text(
                    "Everyone isn't ready yet. Press [s] to start anyway.",
                    style=f"bold {COLOR_TITLE}",
                ))
            else:
                table.add_row(Text(
                    "Start Anyway unlocks in "
                    f"{lobby['remaining']}s or when everyone is ready.",
                    style=f"dim {COLOR_SECONDARY_TEXT}",
                ))
        elif not model["is_host"] and lobby["ready"]:
            table.add_row(Text(
                "Waiting for the host to start...",
                style=f"dim {COLOR_SECONDARY_TEXT}",
            ))
        return table

    def _members_view(
        self, model: Dict[str, Any], width: int, compact: bool = False
    ) -> Table:
        table = Table.grid(expand=True)
        table.add_column(justify="left", ratio=1)
        table.add_column(justify="right")

        header = Text("IN THE ROOM", style=f"bold {COLOR_SUBTITLE}")
        count = Text(
            str(model["participant_count"]),
            style=f"bold {COLOR_TITLE}",
        )
        table.add_row(header, count)

        rows = model["participant_rows"]
        if not rows:
            table.add_row(Text(
                "Loading members...",
                style=f"dim {COLOR_SECONDARY_TEXT}",
            ), Text(""))
            return table

        for row in rows:
            line = Text()
            if row["role"] == "host":
                line.append("● ", style=f"bold {COLOR_SUCCESS}")
            else:
                line.append("○ ", style=f"bold {COLOR_PROMPT}")
            line.append(
                self._truncate(row["name"], max(8, width - 26)),
                style="bold white" if row["is_you"] else "white",
            )
            if row["is_you"] and row["name"].strip().lower() != "you":
                line.append("  You", style=f"bold {COLOR_TITLE}")
            if row["role"] == "host":
                line.append("  [Host]", style=f"bold {COLOR_SUCCESS}")
            else:
                line.append("  [Guest]", style=f"bold {COLOR_PROMPT}")
            table.add_row(line, Text(""))

        if (
            model["participant_count"] == 1
            and not compact
            and (not model["selection_title"] or self.console.height >= 32)
        ):
            hint = (
                "Invite friends with the code."
                if width < 40
                else "You're the only one here. Share the code to invite friends."
            )
            table.add_row(Text(hint, style=f"dim {COLOR_SECONDARY_TEXT}"), Text(""))
        return table

    def _diagnostics_view(self, model: Dict[str, Any], width: int) -> Table:
        diagnostics = model["diagnostics"]
        table = Table.grid(expand=True)
        table.add_column(width=18, justify="left")
        table.add_column(justify="left")

        table.add_row(
            Text("CONNECTION DETAILS", style=f"bold {COLOR_SUBTITLE}"),
            Text(""),
        )

        channels = diagnostics.get("channels") or {}
        channel_text = (
            ", ".join(
                f"{name} {'joined' if joined else 'not joined'}"
                for name, joined in channels.items()
            )
            if channels else "—"
        )
        states = diagnostics.get("channel_states") or {}
        states_text = (
            ", ".join(f"{name} {state}" for name, state in states.items())
            if states else "—"
        )
        raw_error = (
            diagnostics.get("channel_error")
            or diagnostics.get("state_error")
            or diagnostics.get("technical_error")
            or "—"
        )
        error = sanitize_diagnostic(raw_error) if raw_error != "—" else "—"

        rows = [
            ("Transport", str(diagnostics.get("transport") or "—")),
            ("Room ID", str(diagnostics.get("room_id") or "—")),
            ("Channels", channel_text),
            ("Channel states", states_text),
            ("Room state", "loaded" if diagnostics.get("room_state_loaded") else "not loaded"),
            ("Last error", error),
        ]
        for label, value in rows:
            table.add_row(
                Text(label, style=f"dim {COLOR_SECONDARY_TEXT}"),
                Text(self._truncate(value, max(8, width - 20)), style="white"),
            )
        return table

    def _actions_text(
        self, model: Dict[str, Any], width: int, show_diagnostics: bool
    ) -> Text:
        entries: List[Tuple[str, str, str]] = []
        lobby = model.get("lobby") or {}
        if lobby and not lobby.get("started"):
            entries.append(("↑↓", "Choose", COLOR_PROMPT))
            entries.append((
                "ENTER",
                "Update" if lobby.get("ready") else "Ready",
                COLOR_SUCCESS,
            ))
            if lobby.get("can_start_anyway") and model["is_host"]:
                entries.append(("s", "Start Anyway", COLOR_ACCENT))
        if model["is_host"]:
            # Guests stay on the room screen; only the host can detach the
            # room view and drive playback from the main menus.
            entries.append(("b", "Back to Main Menu", COLOR_ACCENT))
        entries.append(("l", "Leave Room", COLOR_ERROR))
        entries.append((
            "i",
            "Hide Details" if show_diagnostics else "Details",
            COLOR_SUBTITLE,
        ))
        entries.append(("q", "Quit", COLOR_TITLE))

        if width < 30:
            short = {
                "Back to Main Menu": "Menu",
                "Leave Room": "Leave",
                "Hide Details": "Hide",
            }
            entries = [
                (key, short.get(label, label), color)
                for key, label, color in entries
            ]

        separator = " • "
        rows: List[List[Tuple[str, str, str]]] = []
        current: List[Tuple[str, str, str]] = []
        current_len = 0
        for entry in entries:
            segment_len = cell_len(f"[{entry[0]}] {entry[1]}")
            next_len = current_len + (
                cell_len(separator) if current else 0
            ) + segment_len
            if current and next_len > width:
                rows.append(current)
                current = []
                current_len = 0
                next_len = segment_len
            current.append(entry)
            current_len = next_len
        if current:
            rows.append(current)

        combined = Text()
        for row_idx, chunk in enumerate(rows):
            if row_idx:
                combined.append("\n")
            combined.append_text(self._keycap_row(chunk, separator=separator))
        return combined

    def _keycap_row(
        self,
        entries: Sequence[Tuple[str, str, str]],
        separator: str = "    ",
    ) -> Text:
        row = Text()
        for idx, (key, label, color) in enumerate(entries):
            if idx:
                row.append(separator, style=f"bold {COLOR_BORDER}")
            row.append("[", style=f"bold {COLOR_BORDER}")
            row.append(key, style=f"bold {color}")
            row.append("]", style=f"bold {COLOR_BORDER}")
            row.append(f" {label}", style="white")
        return row

    @staticmethod
    def _divider(width: int) -> Text:
        return Text("─" * max(10, width), style=f"dim {COLOR_BORDER}")

    # ------------------------------------------------------------------
    # Shared anime selection
    # ------------------------------------------------------------------
    _PLAYBACK_SCALAR_KEYS = (
        "title",
        "episode_number",
        "episode_title",
        "start_time",
        "audio_lang",
        "subtitle_url",
        "subtitle_lang",
        "audio_url",
        "subtitles_disabled",
        "hls_bitrate",
        "quality",
        "provider_id",
        "anime_id",
    )

    @classmethod
    def _clean_playback(cls, playback: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(playback, dict):
            return None
        url = str(playback.get("url") or playback.get("stream_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        clean: Dict[str, Any] = {"url": url}
        for key in cls._PLAYBACK_SCALAR_KEYS:
            value = playback.get(key)
            if value is None or value == "":
                continue
            clean[key] = value
        headers = playback.get("headers")
        if isinstance(headers, dict):
            clean["headers"] = {
                str(name): str(value)
                for name, value in headers.items()
                if str(name).strip() and str(value).strip()
            }
        return clean

    @classmethod
    def _playback_from_selection(cls, selection: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(selection, dict):
            return None
        playback = selection.get("playback")
        if not isinstance(playback, dict):
            playback = selection.get("stream")
        if isinstance(playback, dict):
            return cls._clean_playback(playback)
        if selection.get("stream_url"):
            return cls._clean_playback(selection)
        return None

    @classmethod
    def _playback_from_message(cls, message: Any) -> Optional[Dict[str, Any]]:
        payload = (
            message.get("payload")
            if isinstance(message, dict)
            else getattr(message, "payload", None)
        )
        if not isinstance(payload, dict):
            return None
        playback = payload.get("playback")
        if not isinstance(playback, dict):
            playback = payload.get("stream")
        if isinstance(playback, dict):
            return cls._clean_playback(playback)
        if payload.get("stream_url"):
            return cls._clean_playback(payload)
        return None

    @staticmethod
    def _broadcast_playback(playback: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(playback, dict):
            return None
        clean = dict(playback)
        headers = clean.get("headers")
        if isinstance(headers, dict):
            safe_headers = {
                str(name): str(value)
                for name, value in headers.items()
                if str(name).strip().lower()
                in {
                    "referer",
                    "origin",
                    "user-agent",
                    "accept",
                    "accept-language",
                    "range",
                }
            }
            if safe_headers:
                clean["headers"] = safe_headers
            else:
                clean.pop("headers", None)
        return clean

    @staticmethod
    def _playback_error_notice(exc: BaseException) -> str:
        raw = sanitize_diagnostic(str(exc))
        lowered = raw.lower()
        if isinstance(exc, FileNotFoundError) or "mpv not found" in lowered or (
            "no video players" in lowered
        ):
            return "MPV isn't installed. Install mpv to watch together."
        if "timed out waiting for mpv" in lowered:
            return "MPV didn't respond. Close it and try another episode."
        if "not connected" in lowered or "not_connected" in lowered:
            return "Playback is local for now. Sync resumes when the room reconnects."
        if "no playable stream" in lowered or "no stream" in lowered:
            return "That episode has no playable stream. Try another one."
        return f"Couldn't start playback: {raw[:120]}" if raw else (
            "Couldn't start playback."
        )

    _REFERENCE_KEYS = (
        "title",
        "provider_id",
        "anime_id",
        "episode_number",
        "episode_title",
        "language_mode",
        "poster",
        "anime_type",
    )

    @classmethod
    def _clean_reference(cls, reference: Any) -> Optional[Dict[str, str]]:
        if not isinstance(reference, dict):
            return None
        clean: Dict[str, str] = {}
        for key in cls._REFERENCE_KEYS:
            value = reference.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                clean[key] = text[:300]
        return clean or None

    @classmethod
    def _selection_reference(cls, selection: Any) -> Optional[Dict[str, str]]:
        if not isinstance(selection, dict):
            return None
        embedded = selection.get("anime_reference")
        if isinstance(embedded, dict):
            return cls._clean_reference(embedded)

        anime = selection.get("anime")
        episode = selection.get("episode")

        def pick(container: Any, *names: str) -> str:
            for name in names:
                value = (
                    container.get(name)
                    if isinstance(container, dict)
                    else getattr(container, name, None)
                )
                if value not in (None, ""):
                    return str(value)
            return ""

        reference = {
            "title": selection.get("title") or pick(anime, "title_en", "title", "title_ar"),
            "provider_id": selection.get("provider_id") or "",
            "anime_id": selection.get("anime_id") or pick(anime, "id", "anime_id"),
            "episode_number": selection.get("episode_number")
            or pick(episode, "display_num", "number"),
            "episode_title": selection.get("episode_title") or pick(episode, "title"),
            "language_mode": selection.get("language_mode") or "",
            "poster": selection.get("poster") or pick(anime, "thumbnail"),
            "anime_type": selection.get("anime_type") or pick(anime, "type"),
        }
        return cls._clean_reference(reference)

    @classmethod
    def _reference_from_message(cls, message: Any) -> Optional[Dict[str, str]]:
        payload = (
            message.get("payload")
            if isinstance(message, dict)
            else getattr(message, "payload", None)
        )
        if not isinstance(payload, dict):
            return None
        reference = payload.get("anime_reference")
        if not isinstance(reference, dict):
            room = payload.get("room")
            if isinstance(room, dict):
                reference = room.get("anime_reference")
        if not isinstance(reference, dict):
            reference = payload
        return cls._clean_reference(reference)

    @staticmethod
    def _selection_label(reference: Dict[str, Any]) -> str:
        title = str(reference.get("title") or "the host's pick")
        episode = str(reference.get("episode_number") or "").strip()
        return f"{title} — Episode {episode}" if episode else title

    # ------------------------------------------------------------------
    # Friendly errors and clipboard
    # ------------------------------------------------------------------
    @staticmethod
    def _is_code_error(exc: BaseException) -> bool:
        # Only these two are worth re-entering a code for.
        return getattr(exc, "code", "") in {
            "invalid_room_code",
            "room_not_found",
        }

    @staticmethod
    def _can_retry(exc: BaseException) -> bool:
        return getattr(exc, "code", "") not in {
            "room_full",
            "room_expired",
            "too_many_rooms",
            "invalid_nickname",
        }

    def _friendly_error(
        self, exc: BaseException, context: str
    ) -> Tuple[str, str, str]:
        code = str(getattr(exc, "code", "") or "")
        raw = str(getattr(exc, "description", "") or exc or type(exc).__name__)
        technical = f"{code}: {raw}" if code else raw
        lowered = raw.lower()

        if code == "invalid_supabase_config" or "invalid api key" in lowered or (
            "publishable key" in lowered and "invalid" in lowered
        ):
            return (
                "Watch Together needs setup",
                "The Supabase publishable key is missing or invalid. Run "
                "`python main.py --setup-supabase` and paste the project's "
                "publishable (anon) key.",
                technical,
            )
        if code == "invalid_room_code":
            return (
                "That code didn't work",
                "Room codes are six digits. Check the code with the host and try again.",
                technical,
            )
        if code == "room_not_found":
            return (
                "We couldn't find that room",
                "Check the code with the host. Rooms close after a while.",
                technical,
            )
        if code == "room_full":
            return (
                "This room is full",
                "Ask the host to make space, or try another room.",
                technical,
            )
        if code == "room_expired":
            return (
                "This room has ended",
                "Ask the host to open a new room.",
                technical,
            )
        if code == "too_many_rooms":
            return (
                "You already have rooms open",
                "Wait for an old room to expire, then try again.",
                technical,
            )
        if code in {
            "room_code_secret_missing",
            "room_code_generation_failed",
            "server_misconfigured",
        }:
            return (
                "Watch Together isn't ready",
                "The service is being set up. Please try again later.",
                technical,
            )
        if code == "invalid_nickname":
            return (
                "That name won't work",
                "Try a shorter name.",
                technical,
            )
        if code in {
            "channel_subscribe_failed",
            "realtime_error",
            "not_joined",
        }:
            return (
                "Live updates aren't connected",
                "The room is open, but live updates aren't connected. Try again, or leave the room.",
                technical,
            )
        if code == "leave_failed":
            return (
                "We couldn't leave the room",
                "Check your connection and try again.",
                technical,
            )
        if code in {
            "not_authenticated",
            "missing_authorization",
            "invalid_token",
            "unauthorized",
            "401",
            "403",
        } or "jwt" in lowered or "unauthorized" in lowered:
            return (
                "We couldn't sign you in",
                "Check your internet connection and try again.",
                technical,
            )
        if code in {"connection_failed", "network_error", "timeout"} or any(
            token in lowered
            for token in ("timed out", "timeout", "connection", "network", "unreachable")
        ):
            return (
                "We couldn't connect",
                "Check your internet connection and try again.",
                technical,
            )

        if context == "leave":
            return (
                "We couldn't leave the room",
                "Check your connection and try again.",
                technical,
            )
        if context == "join":
            return (
                "We couldn't join the room",
                "Check the code and your connection, then try again.",
                technical,
            )
        if context == "room":
            return (
                "Live updates aren't connected",
                "Try again, or leave the room and rejoin.",
                technical,
            )
        return (
            "We couldn't open the room",
            "Check your internet connection and try again.",
            technical,
        )

    @staticmethod
    def _copy_to_clipboard(text: str) -> bool:
        if not text or not sys.stdout.isatty():
            return False
        try:
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            sys.stdout.write(f"\033]52;c;{payload}\a")
            sys.stdout.flush()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def clear(self) -> None:
        self.console.clear()
        try:
            self.console.show_cursor(False)
        except Exception:
            pass

    def _pause(self, message: str = "Press any key to continue...") -> None:
        try:
            self.console.print(f"[dim]{message}[/dim]", justify="center")
            with RawTerminal():
                while get_key() is None:
                    time.sleep(0.02)
        except (EOFError, KeyboardInterrupt):
            pass

    @staticmethod
    def _describe_error(exc: BaseException) -> str:
        if isinstance(exc, PartyClientError):
            return f"{exc.code}: {exc.description}"
        return str(exc) or exc.__class__.__name__

    # ------------------------------------------------------------------
    # Legacy screens kept for embedders; the redesigned flow does not use them.
    # ------------------------------------------------------------------
    def lobby_screen(self, room_data: Dict[str, Any]) -> Dict[str, Any]:
        room_code = room_data.get("room_code", "UNKNOWN")
        host_nickname = room_data.get("host_nickname", "Host")
        participants = room_data.get("participants", [])
        ready_count = room_data.get("ready_count", 0)
        total_count = room_data.get("total_count", 0)

        body = Text()
        body.append(f"Room  {room_code}\n", style=f"bold {COLOR_TITLE}")
        body.append(f"Host  {host_nickname}\n\n", style="white")
        body.append(f"Participants  {ready_count}/{total_count} ready\n", style="white")
        body.append(str(participants), style=f"dim {COLOR_SECONDARY_TEXT}")

        self.clear()
        self.console.print(self._stack(
            self._header("Watch Together", "Room lobby"),
            None,
            Panel(
                body,
                title=f"[bold {COLOR_TITLE}]Lobby[/bold {COLOR_TITLE}]",
                box=HEAVY,
                border_style=COLOR_BORDER,
                padding=(1, 2),
                width=self._panel_width(60),
            ),
        ))
        self._pause("Press any key to continue...")

        return {
            "action": "lobby",
            "room_code": room_code,
            "host_nickname": host_nickname,
            "participants": participants,
            "ready_count": ready_count,
            "total_count": total_count,
        }

    async def watch_party_screen(self, room_data: Dict[str, Any]) -> Dict[str, Any]:
        room_code = room_data.get("room_code", "UNKNOWN")
        anime_title = room_data.get("anime_title", "No anime selected")
        episode_info = room_data.get("episode_info", "No episode selected")
        playback_position = room_data.get("position", 0)
        duration = room_data.get("duration", 0)
        is_paused = room_data.get("paused", False)

        body = Text()
        body.append(f"{anime_title}\n", style="bold white")
        body.append(f"{episode_info}\n\n", style=f"dim {COLOR_SECONDARY_TEXT}")
        body.append(
            f"{playback_position:.1f}s / {duration:.1f}s  •  "
            f"{'Paused' if is_paused else 'Playing'}\n\n",
            style="white",
        )
        body.append(f"Room  {room_code}", style=f"bold {COLOR_TITLE}")

        self.clear()
        self.console.print(self._stack(
            self._header("Watch Together", "Watching together"),
            None,
            Panel(
                body,
                title=f"[bold {COLOR_TITLE}]Now playing[/bold {COLOR_TITLE}]",
                box=HEAVY,
                border_style=COLOR_BORDER,
                padding=(1, 2),
                width=self._panel_width(64),
            ),
        ))
        self._pause("Press any key to continue...")

        return {
            "action": "watch_party",
            "room_code": room_code,
            "anime_title": anime_title,
            "episode_info": episode_info,
            "position": playback_position,
            "duration": duration,
            "paused": is_paused,
        }

    def display_message(self, title: str, message: str, style: str = "info") -> None:
        border = COLOR_ERROR if style == "error" else COLOR_BORDER
        body = Text(message, style="white", justify="center")
        self.clear()
        self.console.print(self._stack(
            self._header("Watch Together"),
            None,
            Panel(
                body,
                title=f"[bold {border}]{title}[/bold {border}]",
                box=HEAVY,
                border_style=border,
                padding=(1, 2),
                width=self._panel_width(60),
            ),
        ))
        self._pause("Press any key to continue...")
