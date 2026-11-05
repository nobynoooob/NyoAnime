"""
Watch Together transport abstraction layer.

This module provides an abstract interface for different transport implementations
(primarily Supabase Realtime and WebSocket relay) while maintaining the existing
WTRClient API for backward compatibility.
"""
import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from .protocol import PartyEvent, PartyMessage, sanitize_diagnostic
from .supabase_auth import ensure_authenticated_session

logger = logging.getLogger(__name__)

# Keys whose values must never leave the local machine. Playback payloads only
# need positions, flags and small references, so dropping is safer than trying
# to scrub individual values.
_SENSITIVE_KEYS = frozenset({
    "url", "uri", "stream", "stream_url", "video_url", "playback_url",
    "source_url", "subtitle_url", "headers", "header", "cookie", "cookies",
    "authorization", "auth", "token", "access_token", "refresh_token",
    "api_key", "apikey", "secret", "password", "credentials",
    "playback_credentials", "mpv_socket", "socket_path", "ipc_path",
    "ipc_socket", "input_ipc_path", "output_ipc_path", "stream_headers",
    "key", "service_role_key", "service_role", "anon_key", "auth_header",
})
_SENSITIVE_SUFFIXES = (
    "_url", "_uri", "_token", "_secret", "_cookie", "_cookies",
    "_password", "_socket", "_socket_path", "_ipc", "_ipc_path",
    "_credentials", "_key",
)
_URL_RE = re.compile(r"(?i)\b(?:https?|wss?|ftp|file)://\S+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_IPC_RE = re.compile(r"(?i)(?:^|\s)(?:/[^\s\"']+)+\.sock\b")

# Episode changes intentionally carry a playable source to the room. Only the
# known playback fields are allowed through, and headers are allowlisted so a
# provider cookie or bearer token can never ride along.
_PLAYBACK_URL_KEYS = frozenset({
    "url", "stream_url", "video_url", "playback_url", "source_url",
    "master_url", "subtitle_url", "audio_url",
})
_PLAYBACK_SCALAR_KEYS = frozenset({
    "title", "episode_number", "episode_title", "start_time", "audio_lang",
    "subtitle_lang", "subtitles_disabled", "hls_bitrate", "quality",
    "provider_id", "anime_id", "ext",
})
_PLAYBACK_HEADER_KEYS = frozenset({
    "referer", "origin", "user-agent", "accept", "accept-language", "range",
})
_PLAYBACK_CONTAINERS = frozenset({"playback", "stream", "media"})


def _is_sensitive_key(key: str) -> bool:
    if key in _SENSITIVE_KEYS:
        return True
    if key.endswith(_SENSITIVE_SUFFIXES):
        return True
    return "authorization" in key or "cookie" in key or "credential" in key


def _clean_media_text(value: Any, limit: int = 4000) -> str:
    """Keep a URL intact while still stripping terminal/control bytes."""
    text = str(value or "").replace("\x1b", "").replace("\r", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text.strip()[:limit]


def _sanitize_headers(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    clean: Dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str):
            continue
        if name.strip().lower() not in _PLAYBACK_HEADER_KEYS:
            continue
        text = _clean_media_text(item, 1000)
        if text:
            clean[name] = text
    return clean


def _sanitize_playback(value: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or depth > 6:
        return None
    clean: Dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        lowered = key.strip().lower()
        if lowered in _PLAYBACK_URL_KEYS:
            text = _clean_media_text(item)
            if text:
                clean[key] = text
        elif lowered in {"headers", "stream_headers", "video_headers"}:
            headers = _sanitize_headers(item)
            if headers:
                clean[key] = headers
        elif lowered in _PLAYBACK_SCALAR_KEYS:
            sanitized = sanitize_payload(item, depth + 1)
            if sanitized is not None:
                clean[key] = sanitized
    return clean or None


def sanitize_payload(
    value: Any, depth: int = 0, *, allow_playback: bool = False
) -> Any:
    """Strip anything that must never be broadcast to other participants.

    ``allow_playback`` is only set for episode changes; everything else keeps
    the strict URL/header stripping.
    """
    if depth > 8:
        return None
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            lowered = key.strip().lower()
            if allow_playback and lowered in _PLAYBACK_CONTAINERS:
                playback = _sanitize_playback(item, depth + 1)
                if playback is not None:
                    clean[key] = playback
                continue
            if allow_playback and lowered in _PLAYBACK_URL_KEYS:
                text = _clean_media_text(item)
                if text:
                    clean[key] = text
                continue
            if allow_playback and lowered in {
                "headers", "stream_headers", "video_headers"
            }:
                headers = _sanitize_headers(item)
                if headers:
                    clean[key] = headers
                continue
            if _is_sensitive_key(lowered):
                continue
            sanitized = sanitize_payload(item, depth + 1, allow_playback=allow_playback)
            if sanitized is not None:
                clean[key] = sanitized
        return clean
    if isinstance(value, (list, tuple, set)):
        return [
            item for item in (
                sanitize_payload(entry, depth + 1, allow_playback=allow_playback)
                for entry in value
            ) if item is not None
        ]
    if isinstance(value, str):
        text = value.replace("\x1b", "").replace("\r", " ")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        text = _URL_RE.sub("[link removed]", text)
        text = _BEARER_RE.sub("[redacted]", text)
        text = _IPC_RE.sub(" [socket removed]", text)
        return text[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return "[binary omitted]"
    return str(value)[:2000]


class RealtimeTransportError(Exception):
    """Base exception for transport errors."""
    def __init__(self, code: str, description: str):
        super().__init__(description or code)
        self.code = code
        self.description = description or code


class RealtimeTransport(ABC):
    """Abstract interface for real-time transports."""

    @abstractmethod
    async def connect(self, room_code: str, token: str, participant_id: str = "") -> None:
        """Connect to a room.
        
        Args:
            room_code: The room code to join
            token: Authentication token
            participant_id: Participant identifier (for reconnection)
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from current room."""
        pass

    @abstractmethod
    async def create_room(self, nickname: str, language_mode: str = "ar_sub") -> Dict[str, Any]:
        """Create a new room.
        
        Returns:
            Dict with room_code, token, participant_id
        """
        pass

    @abstractmethod
    async def join_room(
        self, room_code: str, nickname: str, token: str = "", participant_id: str = ""
    ) -> Dict[str, Any]:
        """Join an existing room.
        
        Returns:
            Dict with success, participant_id, nickname, role, room_code
        """
        pass

    @abstractmethod
    async def leave_room(self, room_code: str, user_id: str) -> bool:
        """Leave a room.
        
        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def send_message(self, msg_type: str, payload: Dict[str, Any]) -> None:
        """Send a message to the room."""
        pass

    @abstractmethod
    def subscribe(self, event: str, callback: Callable) -> None:
        """Subscribe to events."""
        pass

    @abstractmethod
    def unsubscribe(self, event: str, callback: Callable) -> None:
        """Unsubscribe from events."""
        pass

    @abstractmethod
    async def get_room_state(self, room_code: str) -> Dict[str, Any]:
        """Get room state information."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if transport is connected."""
        pass

    async def get_playback_state(self) -> Optional[Dict[str, Any]]:
        """Latest persisted playback state, when the transport supports it."""
        return None


class WebSocketRelayTransport(RealtimeTransport):
    """WebSocket relay transport (existing implementation)."""

    def __init__(self, relay_url: str):
        self.relay_url = relay_url
        self.websocket = None
        self._receive_task: Optional[asyncio.Task] = None
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._pending: Dict[str, asyncio.Future] = {}
        self._message_seq = 0
        self._connected = False
        self._room_code = None
        self._room_id = None
        self._participant_id = None
        self._token = None
        self._nickname = ""
        self.on_disconnect: Optional[Callable[[], Any]] = None

    @property
    def room_code(self) -> Optional[str]:
        return self._room_code

    @property
    def room_id(self) -> Optional[str]:
        return self._room_id

    def _envelope(self, msg_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """The relay rejects frames without the routing fields; always send them."""
        self._message_seq += 1
        return {
            "type": msg_type,
            "room_id": self._room_id or self._room_code or "",
            "sender": self._participant_id or "",
            "seq": self._message_seq,
            "timestamp_ms": int(time.time() * 1000),
            "protocol_version": 1,
            "payload": payload,
        }

    @property
    def participant_id(self) -> Optional[str]:
        return self._participant_id

    @property
    def channels_ready(self) -> bool:
        return self._connected

    @property
    def channel_status(self) -> Dict[str, bool]:
        return {"relay": self._connected}

    @property
    def channel_error(self) -> Optional[str]:
        return None

    async def connect(self, room_code: str, token: str, participant_id: str = "") -> None:
        if self._connected:
            await self.disconnect()

        self._room_code = room_code
        self._token = token
        self._participant_id = participant_id

        try:
            import websockets
            from websockets.exceptions import ConnectionClosed

            self.websocket = await websockets.connect(self.relay_url, max_size=2 ** 20)
        except Exception as exc:
            raise RealtimeTransportError("connection_failed", f"Failed to connect to relay: {exc}") from exc

        self._connected = True
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        self._connected = False
        if self._receive_task:
            task = self._receive_task
            self._receive_task = None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        if self.websocket:
            ws = self.websocket
            self.websocket = None
            try:
                await ws.close()
            except Exception:
                pass

        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._room_id = None

    async def create_room(self, nickname: str, language_mode: str = "ar_sub") -> Dict[str, Any]:
        if not self._connected:
            raise RealtimeTransportError("not_connected", "Not connected to relay")

        self._nickname = nickname
        state = await self._request_state(self._envelope(
            PartyEvent.CREATE_ROOM.value,
            {
                "nickname": nickname,
                "language_mode": language_mode,
            },
        ))
        room_code = state.get("room_code")
        if not room_code:
            raise RealtimeTransportError("room_create_failed", "Relay did not return a room code")

        you = state.get("you", {})
        self._room_code = room_code
        self._room_id = state.get("room_id") or room_code
        self._participant_id = you.get("participant_id") or self._participant_id
        self._token = you.get("token") or self._token

        return {
            "room_code": room_code,
            "room_id": self._room_id,
            "token": self._token or "",
            "participant_id": self._participant_id or "",
            "role": you.get("role", "host"),
        }

    async def join_room(
        self, room_code: str, nickname: str, token: str = "", participant_id: str = ""
    ) -> Dict[str, Any]:
        if not self._connected:
            raise RealtimeTransportError("not_connected", "Not connected to relay")

        room_code = str(room_code).strip().upper()
        self._nickname = nickname

        state = await self._request_state(self._envelope(
            PartyEvent.JOIN_ROOM.value,
            {
                "room_code": room_code,
                "nickname": nickname,
                "token": token or "",
                "participant_id": participant_id or "",
            },
        ))
        you = state.get("you", {})
        self._room_code = str(state.get("room_code") or room_code)
        self._room_id = str(state.get("room_id") or self._room_code)
        self._participant_id = you.get("participant_id") or self._participant_id
        self._token = you.get("token") or self._token

        return {
            "success": True,
            "participant_id": self._participant_id,
            "nickname": you.get("nickname", self._nickname),
            "role": you.get("role", "guest"),
            "room_code": self._room_code,
            "room_id": self._room_id,
            "token": self._token or "",
        }

    async def leave_room(self, room_code: str, user_id: str) -> bool:
        if not self._connected or not self._room_code:
            return True

        try:
            await self._send(self._envelope(PartyEvent.LEAVE_ROOM.value, {}))
        except Exception:
            pass

        self._room_code = None
        self._room_id = None
        self._token = None
        self._participant_id = None
        return True

    async def send_message(self, msg_type: str, payload: Dict[str, Any]) -> None:
        if not self._connected:
            raise RealtimeTransportError("not_connected", "Not connected")

        await self._send(self._envelope(
            msg_type,
            sanitize_payload(
                payload,
                allow_playback=msg_type == PartyEvent.EPISODE_CHANGED.value,
            ),
        ))

    def subscribe(self, event: str, callback: Callable) -> None:
        self._event_handlers.setdefault(event, []).append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        handlers = self._event_handlers.get(event, [])
        if callback in handlers:
            handlers.remove(callback)

    async def get_room_state(self, room_code: str) -> Dict[str, Any]:
        if not self._connected:
            raise RealtimeTransportError("not_connected", "Not connected")

        return await self._request_state(self._envelope(
            PartyEvent.ROOM_STATE.value,
            {"room_code": room_code},
        ))

    def is_connected(self) -> bool:
        return self._connected

    async def _request_state(
        self, message: Dict[str, Any], timeout: float = 10.0
    ) -> Dict[str, Any]:
        """Register the waiter before sending so a fast reply cannot be lost."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[PartyEvent.ROOM_STATE.value] = future
        try:
            await self._send(message)
            reply = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise RealtimeTransportError("timeout", "Relay did not answer in time")
        finally:
            self._pending.pop(PartyEvent.ROOM_STATE.value, None)
        if isinstance(reply, dict):
            payload = reply.get("payload", {})
        else:
            payload = getattr(reply, "payload", {}) or {}
        return payload if isinstance(payload, dict) else {}

    async def _send(self, message: Dict[str, Any]) -> None:
        if not self.websocket:
            raise RealtimeTransportError("not_connected", "Not connected")

        await self.websocket.send(json.dumps(message))

    async def _receive_loop(self) -> None:
        try:
            import websockets
            from websockets.exceptions import ConnectionClosed

            async for raw in self.websocket:
                try:
                    data = json.loads(raw)
                    msg = PartyMessage.from_dict(data)
                except Exception as exc:
                    logger.warning("Dropping malformed relay message: %s", exc)
                    continue

                raw_payload = msg.payload if isinstance(msg.payload, dict) else {}

                # Capture the session token before sanitization strips it; only
                # the local client may hold it.
                if msg.type == PartyEvent.ROOM_STATE.value:
                    you = raw_payload.get("you", {}) or {}
                    if you:
                        self._participant_id = you.get("participant_id") or self._participant_id
                        self._token = you.get("token") or self._token
                        self._nickname = you.get("nickname", self._nickname)

                msg.payload = sanitize_payload(
                    raw_payload,
                    allow_playback=msg.type == PartyEvent.EPISODE_CHANGED.value,
                )

                if msg.type == PartyEvent.ROOM_STATE.value:
                    future = self._pending.get(PartyEvent.ROOM_STATE.value)
                    if future and not future.done():
                        future.set_result(msg)

                elif msg.type == PartyEvent.ERROR.value:
                    code = str(msg.payload.get("code", "error"))
                    description = str(msg.payload.get("description", code))
                    future = self._pending.get(PartyEvent.ROOM_STATE.value)
                    if future and not future.done():
                        future.set_exception(RealtimeTransportError(code, description))

                for handler in list(self._event_handlers.get(msg.type, [])):
                    try:
                        result = handler(msg)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        logger.warning("Event handler for %s failed: %s", msg.type, exc)

        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.warning("Receive loop error: %s", exc)
        finally:
            self._connected = False
            if self.on_disconnect is not None:
                try:
                    result = self.on_disconnect()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.warning("Disconnect handler failed: %s", exc)


class SupabaseTransport(RealtimeTransport):
    """Supabase Realtime transport built on one private room channel.

    ``watch-room:<room_id>`` carries chat, presence and authoritative playback.
    Every member may chat; row level security on ``realtime.messages`` only lets
    the current host publish playback events. One socket subscription avoids the
    split-channel ordering races and the extra join round-trip.
    """

    HOST_EVENTS = frozenset({
        PartyEvent.PLAY.value,
        PartyEvent.PAUSE.value,
        PartyEvent.SEEK.value,
        PartyEvent.SYNC_PLAYBACK.value,
        PartyEvent.EPISODE_CHANGED.value,
        PartyEvent.ROOM_STATE.value,
        PartyEvent.END_ROOM.value,
        PartyEvent.LOCK_ROOM.value,
        PartyEvent.KICK.value,
    })

    def __init__(self, supabase_client, user_id: str):
        self.supabase = supabase_client
        self.user_id = user_id
        self._publishable_key = str(
            getattr(supabase_client, "supabase_key", "") or ""
        )
        self.channel = None
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._connected = False
        self._room_code: Optional[str] = None
        self._room_id: Optional[str] = None
        self._participant_id: Optional[str] = None
        self._nickname = ""
        self._role = "guest"
        self._token = ""
        self._seq = 0
        self._channel_error: Optional[str] = None
        self._channel_errors: Dict[str, str] = {}
        self._channel_states: Dict[str, str] = {}
        self._realtime_token: Optional[str] = None
        self._auth_sync_task: Optional[asyncio.Task] = None
        self._subscribing = False
        self._playback_columns_missing = False
        self.on_disconnect: Optional[Callable[[], Any]] = None

    @property
    def room_id(self) -> Optional[str]:
        return self._room_id

    @property
    def room_code(self) -> Optional[str]:
        return self._room_code

    @property
    def participant_id(self) -> Optional[str]:
        return self._participant_id

    @property
    def role(self) -> str:
        return self._role

    @property
    def channel_error(self) -> Optional[str]:
        if self._channel_errors:
            return " | ".join(
                f"{name}: {detail}" for name, detail in self._channel_errors.items()
            )
        return self._channel_error

    @property
    def channels_ready(self) -> bool:
        return bool(
            self._connected
            and self.channel is not None
            and self.channel.is_joined
        )

    @property
    def channel_status(self) -> Dict[str, bool]:
        return {"room": bool(self.channel and self.channel.is_joined)}

    @property
    def channel_states(self) -> Dict[str, str]:
        states = dict(self._channel_states)
        if self.channel is not None:
            states.setdefault(
                "room", self._state_name(getattr(self.channel, "state", None))
            )
        return states

    @staticmethod
    def _state_name(state: Any) -> str:
        value = getattr(state, "value", state)
        text = str(value or "unknown").strip().upper()
        return text.split(".")[-1] or "UNKNOWN"

    @classmethod
    def _describe_channel_failure(cls, state: Any, error: Any) -> str:
        """Return ``STATE: detail`` with credentials scrubbed."""
        state_name = cls._state_name(state)
        detail = ""
        if error is not None:
            raw = str(error).strip()
            parsed = None
            if raw.startswith(("{", "[")):
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    parsed = None
            if isinstance(parsed, dict):
                for key in ("message", "reason", "error_description", "error"):
                    value = parsed.get(key)
                    if isinstance(value, dict):
                        value = (
                            value.get("message")
                            or value.get("description")
                            or value.get("code")
                        )
                    if value:
                        detail = str(value)
                        break
                if not detail:
                    detail = json.dumps(parsed, sort_keys=True)
            else:
                detail = raw
        detail = sanitize_diagnostic(detail)
        return f"{state_name}: {detail}" if detail else state_name

    def _record_channel_state(self, name: str, state: Any, error: Any = None) -> None:
        state_name = self._state_name(state)
        self._channel_states[name] = state_name
        if state_name in ("SUBSCRIBED", "JOINED"):
            self._channel_errors.pop(name, None)
            if not self._channel_errors:
                self._channel_error = None
            logger.info("Supabase %s channel state: %s", name, state_name)
            return

        detail = self._describe_channel_failure(state, error)
        self._channel_errors[name] = detail
        logger.warning("Supabase %s channel state: %s", name, detail)
        if state_name in ("CHANNEL_ERROR", "TIMED_OUT") and not self._subscribing:
            self._schedule_auth_sync()

    async def _sync_realtime_auth(self) -> Optional[str]:
        """Keep the Realtime socket on the auth client's newest access token.

        supabase-auth rotates tokens on a timer; without this the socket (and
        every rejoin payload) keeps the token that was valid at join time and
        channels start failing with ``token has expired``.
        """
        try:
            session = await self.supabase.auth.get_session()
        except Exception as exc:
            logger.warning(
                "Could not refresh Realtime credentials: %s", type(exc).__name__
            )
            return None
        token = getattr(session, "access_token", None)
        if not token:
            return None
        if token != self._realtime_token:
            try:
                await self.supabase.realtime.set_auth(token)
            except Exception as exc:
                logger.warning(
                    "Could not update Realtime credentials: %s", type(exc).__name__
                )
                return None
            self._realtime_token = token
        return token

    def _schedule_auth_sync(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._auth_sync_task is not None and not self._auth_sync_task.done():
            return
        self._auth_sync_task = loop.create_task(self._sync_realtime_auth())

    async def connect(self, room_code: str, token: str, participant_id: str = "") -> None:
        if self._connected:
            await self.disconnect()

        session = await ensure_authenticated_session(self.supabase)
        token = str(getattr(session, "access_token", "") or "")
        if not token:
            raise RealtimeTransportError(
                "not_authenticated",
                "Supabase session is missing; anonymous auth did not complete",
            )
        user = getattr(session, "user", None)
        user_id = getattr(user, "id", None)
        if user_id:
            self.user_id = str(user_id)
        # Refreshed tokens must reach the Realtime socket before the join push.
        await self.supabase.realtime.set_auth(token)
        self._realtime_token = token

        self._room_code = room_code or None
        self._token = token or ""
        self._participant_id = participant_id or None
        self._connected = True
        self._playback_columns_missing = False

    async def disconnect(self) -> None:
        sync_task = self._auth_sync_task
        self._auth_sync_task = None
        if sync_task is not None and not sync_task.done():
            sync_task.cancel()
            try:
                await sync_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._teardown_channel()
        self._connected = False
        self._room_code = None
        self._room_id = None
        self._participant_id = None
        self._role = "guest"
        self._channel_error = None
        self._channel_errors.clear()
        self._channel_states.clear()
        self._realtime_token = None
        self._playback_columns_missing = False

    async def create_room(self, nickname: str, language_mode: str = "ar_sub") -> Dict[str, Any]:
        self._require_connected()
        result = await self._invoke("create-room", {
            "nickname": nickname,
            "language_mode": language_mode,
        })
        room = self._first(result)
        room_id = room.get("room_id")
        participant_id = room.get("participant_id")
        room_code = room.get("room_code")
        if not room_id or not participant_id or not room_code:
            raise RealtimeTransportError("room_create_failed", "Edge function returned an incomplete room")

        self._room_id = str(room_id)
        self._participant_id = str(participant_id)
        self._room_code = str(room_code)
        self._nickname = nickname
        self._role = str(room.get("role") or "host")
        await self._subscribe_channel()

        return {
            "room_code": self._room_code,
            "token": "",
            "participant_id": self._participant_id,
            "role": self._role,
            "room_id": self._room_id,
        }

    async def join_room(
        self, room_code: str, nickname: str, token: str = "", participant_id: str = ""
    ) -> Dict[str, Any]:
        self._require_connected()
        code = str(room_code or "").strip()
        result = await self._invoke("join-room", {
            "room_code": code,
            "nickname": nickname,
        })
        room = self._first(result)
        room_id = room.get("room_id")
        member_id = room.get("participant_id")
        if not room_id or not member_id:
            raise RealtimeTransportError("join_failed", "Edge function returned an incomplete membership")

        self._room_id = str(room_id)
        self._participant_id = str(member_id)
        self._room_code = str(room.get("room_code") or code)
        self._nickname = str(room.get("nickname") or nickname)
        self._role = str(room.get("role") or "guest")
        await self._subscribe_channel()

        return {
            "success": True,
            "participant_id": self._participant_id,
            "nickname": self._nickname,
            "role": self._role,
            "room_code": self._room_code,
            "room_id": self._room_id,
        }

    async def leave_room(self, room_code: str, user_id: str) -> bool:
        # Keep the room identity and live channels when the Edge Function fails
        # so callers can retry instead of orphaning their own membership.
        if not self._room_id:
            self._clear_room_state()
            await self._teardown_channel()
            return True

        if not await self.leave_room_by_id(self._room_id):
            return False
        await self._teardown_channel()
        self._clear_room_state()
        return True

    async def leave_room_by_id(self, room_id: str) -> bool:
        """Leave using only the room UUID; safe when channels are dead."""
        try:
            await self.leave_room_by_id_checked(room_id)
        except RealtimeTransportError as exc:
            logger.warning("Supabase leave failed (%s): %s", exc.code, exc.description)
            return False
        return True

    async def leave_room_by_id_checked(self, room_id: str) -> None:
        """Leave by UUID, raising the Edge Function error for the caller.

        ``not_a_member`` / missing / expired rooms are treated as success: the
        caller no longer has a membership to clean up.
        """
        try:
            await self._invoke("leave-room", {"room_id": room_id})
        except RealtimeTransportError as exc:
            if exc.code in {"not_a_member", "room_not_found", "room_expired"}:
                logger.info("Room membership already gone (%s); nothing to leave", exc.code)
                return
            raise

    async def release_hosted_rooms(self) -> int:
        """Leave every active room this account still hosts.

        ``create_watch_room`` rejects an account with ``too_many_rooms`` after
        an abrupt exit left rooms behind. Membership-only RLS makes the lookup
        safe, and every leave still goes through the ``leave-room`` Edge
        Function (no direct table writes).
        """
        from .cleanup import find_active_hosted_rooms

        try:
            rooms = await find_active_hosted_rooms(self.supabase, self.user_id)
        except Exception as exc:
            raise RealtimeTransportError(
                "cleanup_failed", f"Could not list hosted rooms: {exc}"
            ) from exc

        left = 0
        for room in rooms:
            try:
                await self.leave_room_by_id_checked(room.room_id)
            except RealtimeTransportError as exc:
                logger.warning(
                    "Could not release stale room %s: %s", room.room_id, exc.code
                )
                continue
            left += 1
        return left

    def _clear_room_state(self) -> None:
        self._room_code = None
        self._room_id = None
        self._participant_id = None
        self._role = "guest"
        self._channel_error = None

    async def send_message(self, msg_type: str, payload: Dict[str, Any]) -> None:
        if not self._connected or not self.channel:
            raise RealtimeTransportError("not_connected", "Not joined to a Supabase room")

        # Mirrors the server-side Realtime policy; the server remains the
        # authority because clients can be modified.
        if msg_type in self.HOST_EVENTS and self._role != "host":
            raise RealtimeTransportError(
                "forbidden", "Only the host may send playback events"
            )

        if not self.channel.is_joined:
            raise RealtimeTransportError("not_joined", "Realtime channel is not joined")

        clean = self._sanitize_payload(
            payload,
            allow_playback=msg_type == PartyEvent.EPISODE_CHANGED.value,
        )
        persisted = False
        if msg_type == PartyEvent.EPISODE_CHANGED.value:
            persisted = await self._persist_playback(clean)
        self._seq += 1
        envelope = {
            "type": msg_type,
            "room_id": self._room_id or "",
            "sender": self._participant_id or self.user_id,
            "seq": self._seq,
            "timestamp_ms": int(time.time() * 1000),
            "protocol_version": 1,
            "payload": clean,
        }
        try:
            await self.channel.send_broadcast(msg_type, envelope)
        except Exception as exc:
            if not persisted:
                raise RealtimeTransportError("send_failed", f"Failed to send message: {exc}") from exc
            logger.warning(
                "Realtime broadcast was rejected; persisted playback will be "
                "picked up by guests on their next poll: %s",
                sanitize_diagnostic(exc),
            )

    async def _persist_playback(self, payload: Dict[str, Any]) -> bool:
        """Store the authoritative episode so a missed broadcast is recoverable."""
        if not self._room_id:
            return False
        playback = payload.get("playback")
        if not isinstance(playback, dict):
            url = payload.get("stream_url")
            if not url:
                return False
            playback = {"url": url}
            for key in ("title", "episode_number", "headers"):
                value = payload.get(key)
                if value not in (None, "", {}):
                    playback[key] = value
        if not playback.get("url"):
            return False
        reference = payload.get("anime_reference")
        try:
            await self.supabase.rpc(
                "set_watch_room_playback",
                {
                    "p_room_id": self._room_id,
                    "p_playback": playback,
                    "p_reference": reference if isinstance(reference, dict) else None,
                },
            ).execute()
        except Exception as exc:
            text = str(exc).lower()
            if "set_watch_room_playback" in text and (
                "does not exist" in text or "pgrst202" in text
            ):
                logger.info(
                    "Playback persistence is unavailable; apply "
                    "supabase/migrations/0011_watch_together_playback_state.sql"
                )
            else:
                logger.warning(
                    "Could not persist playback state: %s",
                    sanitize_diagnostic(exc),
                )
            return False
        return True

    async def get_playback_state(self) -> Optional[Dict[str, Any]]:
        """Read the last persisted episode, if the migration is applied."""
        if (
            not self._connected
            or not self._room_id
            or self._playback_columns_missing
        ):
            return None
        try:
            response = await (
                self.supabase.table("watch_rooms")
                .select(
                    "playback,playback_reference,playback_sender,"
                    "playback_seq,playback_updated_at"
                )
                .eq("id", self._room_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            text = str(exc)
            if "42703" in text or "does not exist" in text.lower():
                self._playback_columns_missing = True
                logger.info(
                    "Playback polling disabled; apply "
                    "supabase/migrations/0011_watch_together_playback_state.sql"
                )
            else:
                logger.debug("Playback poll failed: %s", sanitize_diagnostic(exc))
            return None

        rows = getattr(response, "data", None) or []
        row = rows[0] if rows and isinstance(rows[0], dict) else None
        if not row:
            return None
        playback = row.get("playback")
        if not isinstance(playback, dict) or not playback.get("url"):
            return None
        reference = row.get("playback_reference")
        return {
            "playback": playback,
            "anime_reference": reference if isinstance(reference, dict) else None,
            "sender": str(row.get("playback_sender") or "host"),
            "seq": int(row.get("playback_seq") or 0),
            "updated_at": row.get("playback_updated_at"),
        }

    def subscribe(self, event: str, callback: Callable) -> None:
        self._event_handlers.setdefault(event, []).append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        handlers = self._event_handlers.get(event, [])
        if callback in handlers:
            handlers.remove(callback)

    async def get_room_state(self, room_code: str) -> Dict[str, Any]:
        self._require_connected()
        await self._sync_realtime_auth()
        room_id = self._room_id or str(room_code or "")
        if not room_id:
            raise RealtimeTransportError("not_connected", "No room identifier available")

        try:
            members_response = await (
                self.supabase.table("watch_room_members")
                .select("id,user_id,nickname,role,joined_at,last_seen")
                .eq("room_id", room_id)
                .execute()
            )
            room_response = await (
                self.supabase.table("watch_rooms")
                .select("id,host_user_id,language_mode,maximum_members,status,created_at,expires_at")
                .eq("id", room_id)
                .execute()
            )
        except Exception as exc:
            raise RealtimeTransportError("room_state_failed", f"Failed to load room state: {exc}") from exc

        room = (room_response.data or [{}])[0]
        participants = members_response.data or []
        # Host promotion happens server-side when the previous host leaves.
        # Adopt the authoritative role before the next send attempt.
        for member in participants:
            if str(member.get("user_id")) != self.user_id:
                continue
            role = str(member.get("role") or "")
            if role in ("host", "guest"):
                self._role = role
            break

        return {
            "room_code": self._room_code or room_code,
            "room_id": room_id,
            "participants": participants,
            "room_info": room,
            "you": {
                "participant_id": self._participant_id,
                "nickname": self._nickname,
                "role": self._role,
                "user_id": self.user_id,
            },
        }

    def is_connected(self) -> bool:
        return self._connected

    async def retry_channels(self) -> bool:
        """Re-subscribe the private room channel without changing identity."""
        self._require_connected()
        await self._sync_realtime_auth()
        await self._subscribe_channel()
        return self.channels_ready

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _require_connected(self) -> None:
        if not self._connected:
            raise RealtimeTransportError("not_connected", "Not connected to Supabase")

    @property
    def _identity_locked(self) -> bool:
        return bool(self._room_id or self._participant_id)

    async def _refresh_auth(self, *, force_new: bool = False) -> str:
        """Return a live access token and push it to Realtime."""
        # A brand-new anonymous session has a different user id. Inside a room
        # that would orphan the membership, so refresh the existing token only.
        locked = self._identity_locked
        try:
            session = await ensure_authenticated_session(
                self.supabase,
                force_new=force_new and not locked,
                allow_new_identity=not locked,
            )
        except ConnectionError as exc:
            raise RealtimeTransportError("not_authenticated", str(exc)) from exc
        token = str(getattr(session, "access_token", "") or "")
        if not token:
            raise RealtimeTransportError(
                "not_authenticated",
                "Supabase session is missing; anonymous auth did not complete",
            )
        user = getattr(session, "user", None)
        user_id = getattr(user, "id", None)
        if user_id and locked and self.user_id and str(user_id) != str(self.user_id):
            raise RealtimeTransportError(
                "not_authenticated",
                "Supabase identity changed while in a room; rejoin required",
            )
        if user_id:
            self.user_id = str(user_id)
        await self.supabase.realtime.set_auth(token)
        self._realtime_token = token
        return token

    def _auth_headers(self, token: str) -> Dict[str, str]:
        # Be explicit: the Functions client keeps its own header snapshot, and
        # a token refreshed after client construction must not be missed.
        headers = {"Authorization": f"Bearer {token}"}
        if self._publishable_key:
            headers["apikey"] = self._publishable_key
        return headers

    @staticmethod
    def _is_reauthable(exc: Exception) -> bool:
        raw = getattr(exc, "message", None) or str(exc)
        if isinstance(raw, dict):
            raw = f"{raw.get('code', '')} {raw.get('message', '')}"
        text = str(raw).lower()
        if "invalid api key" in text or "invalid apikey" in text:
            return False
        return any(
            marker in text
            for marker in (
                "jwt expired",
                "token has expired",
                "invalid jwt",
                "missing authorization",
                "not_authenticated",
                "unauthorized",
                "401",
            )
        )

    async def _invoke(self, name: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            token = await self._refresh_auth()
        except RealtimeTransportError:
            raise
        except Exception as exc:
            raise self._map_function_error(exc) from exc

        options = {
            "body": body,
            "responseType": "json",
            "headers": self._auth_headers(token),
        }
        try:
            data = await self.supabase.functions.invoke(name, options)
        except Exception as exc:
            if not self._is_reauthable(exc):
                raise self._map_function_error(exc) from exc
            logger.warning(
                "Supabase rejected a stale session during %s; re-authenticating",
                name,
            )
            try:
                token = await self._refresh_auth(force_new=True)
            except RealtimeTransportError:
                raise
            except Exception as auth_exc:
                raise self._map_function_error(auth_exc) from auth_exc
            options["headers"] = self._auth_headers(token)
            try:
                data = await self.supabase.functions.invoke(name, options)
            except Exception as retry_exc:
                raise self._map_function_error(retry_exc) from retry_exc

        if isinstance(data, dict) and data.get("success") is False:
            error = data.get("error") or {}
            raise RealtimeTransportError(
                str(error.get("code") or "function_error"),
                str(error.get("message") or "Edge function rejected the request"),
            )
        if not isinstance(data, dict):
            raise RealtimeTransportError("function_error", "Edge function returned an unexpected payload")
        return data

    @staticmethod
    def _first(result: Dict[str, Any]) -> Dict[str, Any]:
        # PostgREST returns table-valued RPCs as an array. The Edge Functions
        # flatten that already, but accept both shapes defensively.
        if isinstance(result, list):
            return result[0] if result and isinstance(result[0], dict) else {}
        return result

    @staticmethod
    def _map_function_error(exc: Exception) -> RealtimeTransportError:
        raw = getattr(exc, "message", None)
        code = "function_error"
        description = str(raw or exc)

        if isinstance(raw, dict):
            code = str(raw.get("code") or code)
            description = str(raw.get("message") or description)
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                error = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
                code = str(error.get("code") or code)
                description = str(error.get("message") or description)

        lowered = description.lower()
        if "invalid api key" in lowered or "invalid apikey" in lowered:
            code = "invalid_supabase_config"
            description = (
                "Supabase rejected the publishable key. Run "
                "`python main.py --setup-supabase` with the project's key."
            )
        elif code in {"function_error", "error", "401", "403", "unauthorized"} and any(
            marker in lowered
            for marker in (
                "jwt expired",
                "token has expired",
                "invalid jwt",
                "missing authorization",
                "not_authenticated",
                "unauthorized",
            )
        ):
            code = "not_authenticated"
            description = "Your Supabase session expired. Please try again."

        return RealtimeTransportError(code, description)

    async def _subscribe_channel(self) -> None:
        await self._teardown_channel()
        self._channel_error = None
        self._channel_errors.clear()
        self._channel_states.clear()
        if not self._room_id:
            raise RealtimeTransportError("not_joined", "Room identifier is missing")

        await self._sync_realtime_auth()

        topic = f"watch-room:{self._room_id}"
        channel_opts = {
            "config": {
                "private": True,
                "broadcast": {"ack": False, "self": False},
                "presence": {"key": self.user_id, "enabled": True},
            }
        }

        channel = self.supabase.channel(topic, channel_opts)
        channel.on_broadcast("*", self._handle_broadcast)
        channel.on_presence_sync(self._handle_presence)
        self.channel = channel

        states: Dict[str, Any] = {}

        def record(state, error) -> None:
            states["room"] = (state, error)
            self._record_channel_state("room", state, error)

        try:
            self._subscribing = True
            await channel.subscribe(record)
        except Exception as exc:
            await self._teardown_channel()
            detail = sanitize_diagnostic(exc)
            self._channel_error = f"Failed to subscribe to the room channel: {detail}"
            raise RealtimeTransportError(
                "channel_subscribe_failed", self._channel_error
            ) from exc
        finally:
            self._subscribing = False

        state, error = states.get("room", (None, None))
        if not channel.is_joined:
            detail = self._channel_errors.get("room") or self._describe_channel_failure(
                state, error
            )
            self._channel_errors["room"] = detail
            await self._teardown_channel()
            self._channel_error = f"Realtime room channel was not authorized: {detail}"
            raise RealtimeTransportError(
                "channel_subscribe_failed",
                self._channel_error,
            )

        try:
            await channel.track({
                "nickname": self._nickname,
                "role": self._role,
                "user_id": self.user_id,
            })
        except Exception as exc:
            # Presence is useful but not required for playback sync.
            logger.warning("Failed to track Supabase presence: %s", exc)
        logger.info(
            "Supabase Realtime subscribed to %s as %s (%s)",
            topic,
            self._role,
            self._nickname or self.user_id,
        )

    async def _teardown_channel(self) -> None:
        channel = self.channel
        self.channel = None
        if channel is None:
            return
        try:
            await self.supabase.remove_channel(channel)
        except Exception as exc:
            logger.debug("Failed to remove realtime channel: %s", exc)

    def _handle_broadcast(self, message: Any) -> None:
        self._dispatch(message)

    def _handle_presence(self) -> None:
        if self.channel is None:
            return
        try:
            state = self.channel.presence_state()
        except Exception as exc:
            logger.warning("Failed to read presence state: %s", exc)
            return

        msg = PartyMessage(
            type="presence",
            room_id=self._room_id or "",
            sender=self.user_id,
            timestamp_ms=int(time.time() * 1000),
            payload={"participants": state},
        )
        self._dispatch_message(msg)

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        event = str(message.get("event") or "")
        data = message.get("payload")
        if not isinstance(data, dict):
            data = {"value": data}

        # Some Realtime SDK versions deliver the whole broadcast frame
        # ({"type": "broadcast", "event": ..., "payload": <envelope>}) instead
        # of the envelope itself. Unwrap it so the user handler still fires.
        if data.get("type") == "broadcast" and isinstance(data.get("payload"), dict):
            data = data["payload"]

        if data.get("type"):
            try:
                msg = PartyMessage.from_dict(data)
            except Exception as exc:
                logger.warning("Dropping malformed room message: %s", exc)
                return
        else:
            msg = PartyMessage(
                type=event or "message",
                room_id=str(data.get("room_id") or self._room_id or ""),
                sender=str(data.get("sender") or "unknown"),
                payload=data,
            )

        # The Realtime insert policy rejects host events from non-hosts on the
        # shared topic. Re-sanitize anyway: a compromised server or a test double
        # may hand us anything.
        msg.payload = self._sanitize_payload(
            msg.payload,
            allow_playback=msg.type == PartyEvent.EPISODE_CHANGED.value,
        )
        if msg.type == PartyEvent.EPISODE_CHANGED.value:
            logger.info(
                "Realtime episode_changed received for room %s from %s",
                msg.room_id or self._room_id,
                msg.sender,
            )
        else:
            logger.debug(
                "Realtime %s received for room %s from %s",
                msg.type,
                msg.room_id or self._room_id,
                msg.sender,
            )
        self._dispatch_message(msg)

    def _dispatch_message(self, msg: PartyMessage) -> None:
        for event in ("*", msg.type):
            for handler in list(self._event_handlers.get(event, [])):
                try:
                    result = handler(msg)
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception as exc:
                    logger.warning("Event handler for %s failed: %s", msg.type, exc)

    @classmethod
    def _sanitize_payload(
        cls,
        value: Any,
        depth: int = 0,
        *,
        allow_playback: bool = False,
    ) -> Any:
        return sanitize_payload(value, depth, allow_playback=allow_playback)
