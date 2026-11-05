"""
Watch Together client for NyoAnime.

The client is a thin, transport-level wrapper around the relay protocol. It
never decides what to play: synchronization lives in SyncEngine.
"""
import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .protocol import PartyEvent, PartyMessage
from . import lifecycle
from .supabase_auth import ensure_authenticated_session
from .supabase_config import (
    SupabaseConfigError,
    describe_config_issue,
    resolve_supabase_config,
    use_supabase_transport,
)
from .transport import (
    RealtimeTransport,
    RealtimeTransportError,
    SupabaseTransport,
    WebSocketRelayTransport,
    sanitize_payload,
)

logger = logging.getLogger(__name__)


class PartyClientError(Exception):
    def __init__(self, code: str, description: str):
        super().__init__(description or code)
        self.code = code
        self.description = description or code


class WTRClient:
    def __init__(
        self,
        relay_url: str,
        nickname: str,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        session_path: Optional[Path] = None,
    ):
        self.relay_url = relay_url
        self.nickname = nickname
        self.room_code: Optional[str] = None
        self.room_id: Optional[str] = None
        self.participant_id: Optional[str] = None
        self.token: Optional[str] = None
        self.role: str = "guest"
        self._channel_error: Optional[str] = None
        self._transport: Optional[RealtimeTransport] = None
        self._transport_kind: str = "websocket"
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._pending: Dict[str, asyncio.Future] = {}
        self._last_episode_message: Optional[PartyMessage] = None
        self._last_playback_key: Optional[str] = None
        self._playback_poll_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._supabase_hosted = False
        self.on_disconnect: Optional[Callable[[], Any]] = None

        self._supabase_url = supabase_url
        self._supabase_key = supabase_key
        self._supabase_client = None
        self._supabase_user_id: Optional[str] = None
        self._supabase_config: Optional[Tuple[str, str]] = None
        self._session_path = Path(session_path).expanduser() if session_path else None

    @property
    def session_path(self) -> Optional[Path]:
        """Explicit session file, or None when the environment/default applies."""
        return self._session_path

    @property
    def channel_error(self) -> Optional[str]:
        """Live transport error, so diagnostics don't show a stale copy."""
        if self._transport is not None:
            live = getattr(self._transport, "channel_error", None)
            if live:
                return str(live)
        return self._channel_error

    @channel_error.setter
    def channel_error(self, value: Optional[str]) -> None:
        self._channel_error = value

    @property
    def connected(self) -> bool:
        return self._transport is not None and self._transport.is_connected()

    @property
    def transport_kind(self) -> str:
        return self._transport_kind

    @property
    def room_active(self) -> bool:
        return bool(self.room_code or self.room_id)

    @property
    def channels_ready(self) -> bool:
        if self._transport is None:
            return False
        ready = getattr(self._transport, "channels_ready", None)
        if callable(ready):
            ready = ready()
        return bool(ready) if ready is not None else self._transport.is_connected()

    @property
    def channel_status(self) -> Dict[str, bool]:
        if self._transport is None:
            return {}
        status = getattr(self._transport, "channel_status", None)
        if callable(status):
            status = status()
        if isinstance(status, dict):
            return dict(status)
        return {"transport": self.channels_ready}

    @property
    def channel_states(self) -> Dict[str, str]:
        """Raw Realtime state per channel (SUBSCRIBED, CHANNEL_ERROR, ...)."""
        if self._transport is None:
            return {}
        states = getattr(self._transport, "channel_states", None)
        if callable(states):
            states = states()
        return dict(states) if isinstance(states, dict) else {}

    @property
    def connection_status(self) -> Dict[str, Any]:
        return {
            "room_code": self.room_code,
            "room_id": self.room_id,
            "role": self.role,
            "participant_id": self.participant_id,
            "transport_kind": self._transport_kind,
            "connected": self.connected,
            "channels_ready": self.channels_ready,
            "channels": self.channel_status,
            "channel_states": self.channel_states,
            "channel_error": self.channel_error,
        }

    def local_room_state(self) -> Dict[str, Any]:
        """Return what is known locally even when Realtime never subscribed."""
        return {
            "room_code": self.room_code,
            "room_id": self.room_id,
            "participants": [],
            "room_info": {},
            "you": {
                "participant_id": self.participant_id,
                "nickname": self.nickname,
                "role": self.role,
            },
        }

    def _adopt_transport_state(self) -> None:
        transport = self._transport
        if transport is None:
            return
        self.room_code = getattr(transport, "room_code", None) or self.room_code
        self.room_id = getattr(transport, "room_id", None) or self.room_id
        self.participant_id = getattr(transport, "participant_id", None) or self.participant_id
        role = getattr(transport, "role", None)
        if role:
            self.role = role
        self.channel_error = getattr(transport, "channel_error", None)

    def _clear_room_state(self) -> None:
        self.room_code = None
        self.room_id = None
        self.participant_id = None
        self.token = None
        self.role = "guest"
        self.channel_error = None
        self._last_episode_message = None
        self._last_playback_key = None

    async def connect(
        self, room_code: Optional[str] = None, token: Optional[str] = None
    ) -> None:
        if self._transport is not None:
            await self.disconnect()

        self._loop = asyncio.get_running_loop()
        config = resolve_supabase_config(self._supabase_url, self._supabase_key)
        if use_supabase_transport() and config is not None:
            issue = describe_config_issue(*config)
            if issue:
                # A malformed key would otherwise fail as an anonymous-auth 401
                # and surface as a generic room creation error.
                raise SupabaseConfigError(
                    f"{issue}. Run `python main.py --setup-supabase` and paste "
                    "the project's publishable key."
                )
            try:
                client, user_id = await self._ensure_supabase_client(config)
                self._transport = SupabaseTransport(client, user_id)
                self._transport_kind = "supabase"
            except ImportError:
                raise ConnectionError(
                    "supabase dependency not installed. Install with: pip install supabase"
                ) from None
            except SupabaseConfigError:
                raise
            except Exception as exc:
                if "invalid api key" in str(exc).lower():
                    raise SupabaseConfigError(
                        "Supabase rejected the publishable key. Run "
                        "`python main.py --setup-supabase` with the project's "
                        "publishable (anon) key."
                    ) from exc
                raise ConnectionError(f"Failed to connect to Supabase: {exc}") from exc
        else:
            self._transport = WebSocketRelayTransport(self.relay_url)
            self._transport_kind = "websocket"

        self._transport.on_disconnect = self._handle_transport_disconnect

        # Subscribers may have been registered before connect(); replay them
        # onto the freshly created transport.
        for event, handlers in self._event_handlers.items():
            for handler in handlers:
                self._transport.subscribe(event, handler)
        # Keep the newest episode change so a screen that subscribes a tick
        # late still launches the stream the host already picked.
        self._transport.subscribe(
            PartyEvent.EPISODE_CHANGED.value, self._remember_episode
        )

        try:
            await self._transport.connect(room_code or "", token or "", "")
        except Exception as exc:
            self._transport = None
            raise ConnectionError(f"Failed to connect: {exc}") from exc

    async def _ensure_supabase_client(self, config: Tuple[str, str]):
        from supabase import acreate_client
        from supabase.lib.client_options import AsyncClientOptions

        from .session_storage import FileSessionStorage, resolve_session_path

        if self._supabase_client is None or self._supabase_config != config:
            storage = FileSessionStorage(resolve_session_path(self._session_path))
            options = AsyncClientOptions(
                storage=storage,
                auto_refresh_token=True,
                persist_session=True,
            )
            self._supabase_client = await acreate_client(config[0], config[1], options)
            self._supabase_config = config
            self._supabase_user_id = None

        client = self._supabase_client
        session = await ensure_authenticated_session(client)

        user = getattr(session, "user", None)
        user_id = getattr(user, "id", None) or self._supabase_user_id
        if not user_id:
            raise ConnectionError("Supabase session does not contain a user id")
        self._supabase_user_id = str(user_id)

        # Realtime private channels read the JWT from the socket handshake.
        await client.realtime.set_auth(session.access_token)
        return client, self._supabase_user_id

    async def disconnect(self) -> None:
        self.stop_playback_poll()
        if self._transport:
            await self._transport.disconnect()
        self._transport = None
        self._pending.clear()
        self._clear_room_state()

    async def reconnect(self) -> None:
        """Restore the same room identity after a dropped connection."""
        if not self.room_code or not self.participant_id:
            raise RuntimeError("Cannot reconnect without an existing room session")
        if self._transport_kind == "websocket" and not self.token:
            raise RuntimeError("Cannot reconnect without a relay token")

        saved = {
            "room_code": self.room_code,
            "room_id": self.room_id,
            "participant_id": self.participant_id,
            "token": self.token,
            "role": self.role,
        }
        room_code = self.room_code
        token = self.token or ""
        participant_id = self.participant_id
        try:
            await self.disconnect()
            await self.connect(room_code, token)
            await self.join_room(room_code, token=token, participant_id=participant_id)
        except Exception:
            # Keep a retryable identity even if the new transport never came up.
            self.room_code = self.room_code or saved["room_code"]
            self.room_id = self.room_id or saved["room_id"]
            self.participant_id = self.participant_id or saved["participant_id"]
            self.token = self.token or saved["token"]
            if self.role == "guest":
                self.role = saved["role"]
            self._adopt_transport_state()
            raise

    async def retry_connection(self) -> None:
        """Re-subscribe channels first; fall back to a full reconnect."""
        if self._transport is not None and self.connected and self.room_id:
            retry = getattr(self._transport, "retry_channels", None)
            if callable(retry):
                try:
                    await retry()
                    self.channel_error = getattr(self._transport, "channel_error", None)
                    if self.channels_ready:
                        return
                except RealtimeTransportError:
                    self._adopt_transport_state()
                except Exception as exc:
                    self._adopt_transport_state()
                    self.channel_error = str(exc)
        await self.reconnect()

    async def create_room(self, language_mode: str = "ar_sub", timeout: float = 10.0) -> str:
        if not self._transport or not self._transport.is_connected():
            raise RuntimeError("Not connected to relay")

        try:
            try:
                result = await self._transport.create_room(self.nickname, language_mode)
            except RealtimeTransportError as exc:
                # A previous crash can leave active rooms under this account.
                # Clear them through leave-room once, then retry instead of
                # surfacing the "already have rooms open" dead end.
                if exc.code != "too_many_rooms" or not await self._release_stale_hosted_rooms():
                    raise
                result = await self._transport.create_room(self.nickname, language_mode)
            self.room_code = result.get("room_code")
            self.room_id = result.get("room_id")
            self.participant_id = result.get("participant_id")
            self.role = result.get("role", "host")
            self.token = result.get("token") or ""
            self.channel_error = None
            self._last_episode_message = None
            self._last_playback_key = None

            if not self.room_code:
                raise PartyClientError("room_create_failed", "Relay did not return a room code")

            self._track_hosting()
            return self.room_code
        except RealtimeTransportError as exc:
            self._adopt_transport_state()
            raise PartyClientError(exc.code, exc.description) from exc
        except PartyClientError:
            raise
        except Exception as exc:
            raise PartyClientError("room_create_failed", f"Failed to create room: {exc}") from exc

    async def _release_stale_hosted_rooms(self) -> bool:
        transport = self._transport
        if transport is None or self._transport_kind != "supabase":
            return False
        releaser = getattr(transport, "release_hosted_rooms", None)
        if not callable(releaser):
            return False
        try:
            left = int(await releaser())
        except Exception as exc:
            logger.warning("Stale hosted-room cleanup failed: %s", exc)
            return False
        if left:
            logger.info("Released %d stale hosted room(s) before creating a new one", left)
        return left > 0

    def _track_hosting(self) -> None:
        if self._transport_kind != "supabase":
            return
        if str(self.role or "").lower() != "host":
            return
        self._supabase_hosted = True
        lifecycle.mark_hosting(self)

    def _release_tracking(self) -> None:
        if not self._supabase_hosted:
            return
        self._supabase_hosted = False
        lifecycle.unmark_hosting(self)

    def _exit_cleanup(self) -> None:
        """Synchronous atexit backstop for an unconfirmed leave."""
        if not self._supabase_hosted:
            return
        lifecycle.run_exit_cleanup()

    async def join_room(
        self,
        room_code: str,
        token: Optional[str] = None,
        participant_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        if not self._transport or not self._transport.is_connected():
            raise RuntimeError("Not connected to relay")

        try:
            result = await self._transport.join_room(
                room_code, self.nickname, token or "", participant_id or ""
            )
            if not result.get("success"):
                raise PartyClientError("join_failed", "Failed to join room")

            self.participant_id = result.get("participant_id")
            self.nickname = result.get("nickname", self.nickname)
            self.role = result.get("role", "guest")
            self.room_code = result.get("room_code") or room_code
            self.room_id = result.get("room_id")
            self.token = result.get("token") or token or ""
            self.channel_error = None
            self._last_episode_message = None
            self._last_playback_key = None
            self._track_hosting()
        except RealtimeTransportError as exc:
            self._adopt_transport_state()
            raise PartyClientError(exc.code, exc.description) from exc
        except PartyClientError:
            raise
        except Exception as exc:
            raise PartyClientError("join_failed", f"Failed to join room: {exc}") from exc

    async def leave_room(self) -> None:
        if self._transport is None and self.room_id and self._transport_kind == "supabase":
            config = resolve_supabase_config(self._supabase_url, self._supabase_key)
            if config is not None:
                try:
                    client, user_id = await self._ensure_supabase_client(config)
                    leftover = SupabaseTransport(client, user_id)
                    left = await leftover.leave_room_by_id(self.room_id)
                except Exception as exc:
                    raise PartyClientError(
                        "leave_failed", f"Failed to leave room: {exc}"
                    ) from exc
                if not left:
                    raise PartyClientError(
                        "leave_failed",
                        "The server did not confirm that you left the room",
                    )
                self._clear_room_state()
                self._release_tracking()
                return

        if self._transport and (self.room_code or self.room_id):
            try:
                left = await self._transport.leave_room(
                    self.room_code or "", self.participant_id or ""
                )
            except RealtimeTransportError as exc:
                self._adopt_transport_state()
                raise PartyClientError(exc.code, exc.description) from exc
            except Exception as exc:
                self._adopt_transport_state()
                raise PartyClientError("leave_failed", f"Failed to leave room: {exc}") from exc
            if not left:
                self._adopt_transport_state()
                raise PartyClientError(
                    "leave_failed",
                    "The server did not confirm that you left the room",
                )
        self._clear_room_state()
        self._release_tracking()

    async def get_room_state(self, room_code: Optional[str] = None) -> Dict[str, Any]:
        if self._transport is None:
            raise PartyClientError("not_connected", "Not connected to a room transport")
        code = room_code or self.room_code or ""
        try:
            state = await self._transport.get_room_state(code)
        except RealtimeTransportError as exc:
            raise PartyClientError(exc.code, exc.description) from exc
        except Exception as exc:
            raise PartyClientError(
                "room_state_failed", f"Failed to load room state: {exc}"
            ) from exc

        if not isinstance(state, dict):
            raise PartyClientError("room_state_failed", "Room state was not an object")
        state.setdefault("room_code", self.room_code)
        state.setdefault("room_id", self.room_id)
        you = state.get("you")
        if not isinstance(you, dict):
            you = {}
        you.setdefault("participant_id", self.participant_id)
        you.setdefault("nickname", self.nickname)
        you.setdefault("role", self.role)
        role = str(you.get("role") or "").lower()
        if role in ("host", "guest"):
            self.role = role
        state["you"] = you
        return state

    async def send_event(self, event: str, payload: Dict[str, Any]) -> None:
        if not self._transport or not self._transport.is_connected():
            raise RuntimeError("Not connected")
        await self._transport.send_message(
            event,
            sanitize_payload(
                payload,
                allow_playback=event == PartyEvent.EPISODE_CHANGED.value,
            ),
        )

    async def send_message(self, msg_type: str, payload: Dict[str, Any]) -> None:
        await self.send_event(msg_type, payload)

    async def broadcast_playback(
        self,
        playback: Dict[str, Any],
        reference: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a resolved stream as ``EPISODE_CHANGED`` to the room.

        The transport sanitizer allowlists playback URLs and safe video headers;
        cookies, authorization headers and tokens are always dropped.
        """
        if not isinstance(playback, dict) or not playback:
            raise PartyClientError(
                "invalid_playback", "No playable stream was resolved for this episode"
            )
        payload: Dict[str, Any] = {"playback": playback}
        if isinstance(reference, dict) and reference:
            payload["anime_reference"] = reference
        # Mirror the essentials for clients that only understand the flat shape.
        for source, target in (
            ("url", "stream_url"),
            ("title", "title"),
            ("episode_number", "episode_number"),
            ("headers", "headers"),
        ):
            value = playback.get(source)
            if value not in (None, "", {}):
                payload[target] = value
        await self.send_event(PartyEvent.EPISODE_CHANGED.value, payload)

    def _remember_episode(self, message: Any) -> None:
        if isinstance(message, PartyMessage):
            self._last_episode_message = message
            key = self._playback_key(message.payload)
            if key is not None:
                self._last_playback_key = key

    @staticmethod
    def _playback_key(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        playback = payload.get("playback")
        if not isinstance(playback, dict):
            playback = payload
        url = playback.get("url") or payload.get("stream_url")
        if not url:
            return None
        episode = playback.get("episode_number")
        if episode in (None, ""):
            episode = payload.get("episode_number") or ""
        return f"{url}|{episode}"

    def start_playback_poll(self, interval: float = 2.0) -> None:
        """Recover a missed EPISODE_CHANGED from the persisted room state."""
        if self._transport_kind != "supabase" or self._transport is None:
            return
        if str(self.role or "").lower() == "host":
            return
        if self._playback_poll_task is not None and not self._playback_poll_task.done():
            return
        self._playback_poll_task = asyncio.create_task(
            self._poll_playback_loop(max(0.05, float(interval)))
        )

    def stop_playback_poll(self) -> None:
        task, self._playback_poll_task = self._playback_poll_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _poll_playback_loop(self, interval: float) -> None:
        while True:
            transport = self._transport
            getter = getattr(transport, "get_playback_state", None)
            if transport is not None and callable(getter) and transport.is_connected():
                try:
                    state = await getter()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("Playback poll failed: %s", type(exc).__name__)
                    state = None
                if isinstance(state, dict):
                    self._apply_playback_state(state)
            await asyncio.sleep(interval)

    def _apply_playback_state(self, state: Dict[str, Any]) -> None:
        playback = state.get("playback")
        if not isinstance(playback, dict):
            return
        payload: Dict[str, Any] = {"playback": dict(playback)}
        reference = state.get("anime_reference")
        if isinstance(reference, dict) and reference:
            payload["anime_reference"] = reference
        for source, target in (
            ("url", "stream_url"),
            ("title", "title"),
            ("episode_number", "episode_number"),
            ("headers", "headers"),
        ):
            value = playback.get(source)
            if value not in (None, "", {}):
                payload[target] = value

        key = self._playback_key(payload)
        if key is None or key == self._last_playback_key:
            return

        message = PartyMessage(
            type=PartyEvent.EPISODE_CHANGED.value,
            room_id=self.room_id or "",
            sender=str(state.get("sender") or "host"),
            payload=payload,
        )
        logger.info(
            "Recovered episode from persisted playback state for room %s",
            self.room_id,
        )
        self._remember_episode(message)
        for handler in list(self._event_handlers.get(PartyEvent.EPISODE_CHANGED.value, [])):
            try:
                result = handler(message)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as exc:
                logger.warning("Playback poll handler failed: %s", exc)

    def _replay_last_episode(self, callback: Callable[[Any], Any]) -> None:
        cached = self._last_episode_message
        if cached is None:
            return
        replay = PartyMessage(
            type=cached.type,
            room_id=cached.room_id,
            sender=cached.sender,
            seq=cached.seq,
            timestamp_ms=cached.timestamp_ms,
            protocol_version=cached.protocol_version,
            payload=dict(cached.payload),
        )
        try:
            result = callback(replay)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as exc:
            logger.warning("Late episode replay failed: %s", exc)

    def subscribe(self, event: str, callback: Callable) -> None:
        handlers = self._event_handlers.setdefault(event, [])
        if callback in handlers:
            return
        if self._transport:
            self._transport.subscribe(event, callback)
        handlers.append(callback)
        if event == PartyEvent.EPISODE_CHANGED.value:
            self._replay_last_episode(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        handlers = self._event_handlers.get(event, [])
        if callback in handlers:
            handlers.remove(callback)
        if self._transport:
            self._transport.unsubscribe(event, callback)

    async def ping(self) -> None:
        await self.send_event(PartyEvent.PING.value, {})

    async def _handle_transport_disconnect(self) -> None:
        if self.on_disconnect:
            try:
                result = self.on_disconnect()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("Disconnect handler failed: %s", exc)

    async def close(self) -> None:
        await self.disconnect()
