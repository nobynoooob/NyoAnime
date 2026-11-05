"""
Watch Together relay server.

The relay only authenticates, authorizes, validates and forwards messages.
It never runs synchronization logic and never talks to a player.
"""
import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from nyoanime.party.models import Role
from nyoanime.party.protocol import PartyEvent, PartyMessage, sanitize_text
from nyoanime.party.rooms import (
    InvalidToken,
    RoomError,
    RoomFull,
    RoomManager,
    RoomNotFound,
)
from .config import HEARTBEAT_TIMEOUT_MS, MAX_MESSAGE_SIZE
from .config import MAX_PARTICIPANTS, ROOM_EXPIRATION_MINUTES, HOST_RECONNECTION_GRACE

logger = logging.getLogger(__name__)

HOST_EVENTS = {
    PartyEvent.SYNC_PLAYBACK.value,
    PartyEvent.PLAY.value,
    PartyEvent.PAUSE.value,
    PartyEvent.SEEK.value,
    PartyEvent.EPISODE_CHANGED.value,
    PartyEvent.END_ROOM.value,
}

# Keys that are safe to relay as part of an anime reference. Stream URLs,
# cookies, authorization headers and provider tokens must never be forwarded.
REFERENCE_KEYS = {
    "title",
    "titles",
    "provider_id",
    "anime_id",
    "episode_number",
    "episode_title",
    "language_mode",
    "poster",
    "anime_type",
}

CHAT_WINDOW_SECONDS = 5.0
CHAT_MAX_MESSAGES = 5


def _sanitize_reference(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    clean: Dict[str, Any] = {}
    for key in REFERENCE_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == "titles":
            if isinstance(item, list):
                clean[key] = [sanitize_text(v, 200) for v in item[:8] if str(v).strip()]
        elif key in ("episode_number", "anime_type"):
            clean[key] = sanitize_text(item, 40)
        elif key in ("provider_id", "anime_id", "language_mode"):
            clean[key] = sanitize_text(item, 100)
        else:
            clean[key] = sanitize_text(item, 300)
    return clean or None


PLAYBACK_URL_KEYS = {
    "url",
    "stream_url",
    "video_url",
    "playback_url",
    "source_url",
    "master_url",
    "subtitle_url",
    "audio_url",
}
PLAYBACK_SCALAR_KEYS = {
    "title",
    "episode_number",
    "episode_title",
    "start_time",
    "audio_lang",
    "subtitle_lang",
    "subtitles_disabled",
    "hls_bitrate",
    "quality",
    "provider_id",
    "anime_id",
    "ext",
}
PLAYBACK_HEADER_KEYS = {
    "referer",
    "origin",
    "user-agent",
    "accept",
    "accept-language",
    "range",
}


def _clean_media_text(value: Any, limit: int) -> str:
    return sanitize_text(value, limit)


def _sanitize_playback_headers(value: Any) -> Optional[Dict[str, str]]:
    if not isinstance(value, dict):
        return None
    clean = {}
    for name, item in value.items():
        if not isinstance(name, str) or name.strip().lower() not in PLAYBACK_HEADER_KEYS:
            continue
        text = _clean_media_text(item, 1000)
        if text:
            clean[name] = text
    return clean or None


def _sanitize_playback(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    clean: Dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        lowered = key.strip().lower()
        if lowered in PLAYBACK_URL_KEYS:
            text = _clean_media_text(item, 4000)
            if text:
                clean[key] = text
        elif lowered in {"headers", "stream_headers", "video_headers"}:
            headers = _sanitize_playback_headers(item)
            if headers:
                clean[key] = headers
        elif lowered in PLAYBACK_SCALAR_KEYS:
            if isinstance(item, (int, float, bool)):
                clean[key] = item
            else:
                text = _clean_media_text(item, 300)
                if text:
                    clean[key] = text
    return clean or None


def _episode_payload(msg: PartyMessage) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    reference = _sanitize_reference((msg.payload or {}).get("anime_reference"))
    if reference:
        payload["anime_reference"] = reference
    playback = _sanitize_playback((msg.payload or {}).get("playback"))
    if playback:
        payload["playback"] = playback
        # Mirror the essentials for clients that predate the playback object.
        for key, source in (
            ("stream_url", "url"),
            ("title", "title"),
            ("episode_number", "episode_number"),
            ("headers", "headers"),
        ):
            if playback.get(source):
                payload[key] = playback[source]
    return payload


class WatchTogetherServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        max_participants: int = MAX_PARTICIPANTS,
        room_expiration_minutes: int = ROOM_EXPIRATION_MINUTES,
        host_reconnection_grace: int = HOST_RECONNECTION_GRACE,
    ):
        self.host = host
        self.port = port
        self.room_manager = RoomManager()
        self.settings = {
            "max_participants": max_participants,
            "room_expiration_ms": room_expiration_minutes * 60 * 1000,
            "host_reconnection_grace_ms": host_reconnection_grace * 1000,
        }
        self._server = None
        self._connections: Dict[str, ServerConnection] = {}
        self._participant_rooms: Dict[str, str] = {}
        self._last_seq: Dict[str, int] = {}
        self._chat_times: Dict[str, list] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def address(self) -> str:
        port = self.port
        if self._server and self._server.sockets:
            try:
                port = self._server.sockets[0].getsockname()[1]
            except Exception:
                pass
        return f"ws://{self.host}:{port}"

    async def start(self):
        if self._server is not None:
            return
        self._server = await serve(
            self.handle_connection,
            self.host,
            self.port,
            max_size=MAX_MESSAGE_SIZE,
            ping_interval=20,
            ping_timeout=30,
        )
        if self.port == 0 and self._server.sockets:
            self.port = self._server.sockets[0].getsockname()[1]
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Watch Together relay listening on %s", self.address)

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        for ws in list(self._connections.values()):
            try:
                await ws.close(code=1001, reason="Relay stopping")
            except Exception:
                pass
        self._connections.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Watch Together relay stopped")

    async def _cleanup_loop(self):
        try:
            while True:
                await asyncio.sleep(30)
                self.room_manager.cleanup_expired_rooms()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Connection / session
    # ------------------------------------------------------------------
    async def handle_connection(self, websocket: ServerConnection):
        room = None
        participant = None
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="strict")
                if len(raw) > MAX_MESSAGE_SIZE:
                    await self._send_error(websocket, "message_too_large", "Message exceeds size limit")
                    continue
                try:
                    data = json.loads(raw)
                    msg = PartyMessage.from_dict(data)
                except Exception as exc:
                    await self._send_error(websocket, "invalid_message", str(exc))
                    continue

                if msg.type == PartyEvent.CREATE_ROOM.value:
                    room, participant = await self._handle_create(websocket, msg)
                    continue
                if msg.type == PartyEvent.JOIN_ROOM.value:
                    room, participant = await self._handle_join(websocket, msg)
                    continue

                if room is None or participant is None:
                    await self._send_error(websocket, "not_in_room", "Join a room first")
                    continue

                if not self._validate_incoming(room, participant, msg):
                    await self._send_error(websocket, "invalid_message", "Message rejected")
                    continue

                seq_key = participant.id
                last_seq = self._last_seq.get(seq_key, -1)
                if msg.seq <= last_seq:
                    await self._send_error(websocket, "stale_message", "Replayed or stale message")
                    continue
                self._last_seq[seq_key] = msg.seq
                participant.last_heartbeat = int(time.time() * 1000)

                await self.route_message(room, participant, msg)

        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.warning("Connection error: %s", exc)
        finally:
            if participant is not None and room is not None:
                self._connections.pop(participant.id, None)
                self._participant_rooms.pop(participant.id, None)
                still_open = any(
                    p.id == participant.id and p.connected
                    for p in room.participants.values()
                )
                if still_open:
                    self.room_manager.mark_disconnected(room.id, participant.id)
                    await self.broadcast(room, self._room_state_message(room, None))
                    await self.broadcast(room, {
                        "type": PartyEvent.PARTICIPANT_LEFT.value,
                        "room_id": room.id,
                        "sender": participant.id,
                        "seq": 0,
                        "timestamp_ms": int(time.time() * 1000),
                        "payload": {
                            "participant_id": participant.id,
                            "nickname": participant.nickname,
                            "temporary": True,
                        },
                    })
                logger.info("Participant %s disconnected from %s", participant.nickname, room.id)

    def _validate_incoming(self, room, participant, msg: PartyMessage) -> bool:
        try:
            msg.validate()
        except ValueError:
            return False
        if msg.room_id != room.id:
            return False
        if msg.sender != participant.id:
            return False
        if msg.type in HOST_EVENTS and participant.role != Role.HOST:
            return False
        if msg.type == PartyEvent.CHAT.value:
            text = sanitize_text(msg.payload.get("text", ""))
            if not text:
                return False
            msg.payload["text"] = text
        if msg.type == PartyEvent.SYNC_PLAYBACK.value:
            if not self._valid_playback_payload(msg.payload):
                return False
        return True

    @staticmethod
    def _valid_playback_payload(payload: Dict[str, Any]) -> bool:
        try:
            position = float(payload.get("position", 0.0))
            duration = float(payload.get("duration", 0.0))
            speed = float(payload.get("speed", 1.0))
            sequence = int(payload.get("sequence", 0))
            int(payload.get("timestamp_ms", 0))
        except (TypeError, ValueError):
            return False
        if position < -1 or duration < 0 or speed < 0.25 or speed > 4.0:
            return False
        return sequence >= 0 and payload.get("host") is True

    async def _handle_create(self, websocket, msg: PartyMessage):
        payload = msg.payload or {}
        nickname = sanitize_text(payload.get("nickname", ""), 40) or "Host"
        language_mode = sanitize_text(payload.get("language_mode", "ar_sub"), 20)
        import secrets
        participant_id = "p_" + secrets.token_urlsafe(9)
        token = secrets.token_urlsafe(32)
        room = self.room_manager.create_room(
            participant_id,
            nickname=nickname,
            token=token,
            metadata={"language_mode": language_mode},
        )
        room.settings.max_participants = int(self.settings["max_participants"])
        room.settings.room_expiration_ms = int(self.settings["room_expiration_ms"])
        participant = room.participants[participant_id]
        self._bind(websocket, participant, room)
        await self._send_json(websocket, self._room_state_message(room, participant))
        return room, participant

    async def _handle_join(self, websocket, msg: PartyMessage):
        payload = msg.payload or {}
        room_code = str(payload.get("room_code") or msg.room_id or "").strip().upper()
        nickname = sanitize_text(payload.get("nickname", ""), 40) or "Guest"
        token = str(payload.get("token") or "")
        participant_id = str(payload.get("participant_id") or "")

        try:
            room = self.room_manager.get_room(room_code)
            if room is None:
                raise RoomNotFound("Room not found")

            if token and participant_id:
                participant = self.room_manager.reconnect(room.id, participant_id, token)
            else:
                import secrets
                participant_id = "p_" + secrets.token_urlsafe(9)
                token = secrets.token_urlsafe(32)
                participant = self.room_manager.join_room(room.id, participant_id, nickname, token)
                if participant is None:
                    raise RoomFull("Room is full or locked")
        except RoomNotFound:
            await self._send_error(websocket, "room_not_found", "Room not found")
            return None, None
        except InvalidToken:
            await self._send_error(websocket, "invalid_token", "Invalid room token")
            return None, None
        except RoomFull:
            await self._send_error(websocket, "room_full", "Room is full")
            return None, None
        except RoomError as exc:
            await self._send_error(websocket, exc.code, str(exc))
            return None, None

        self._bind(websocket, participant, room)
        state = self._room_state_message(room, participant)
        await self._send_json(websocket, state)
        await self.broadcast(room, self._room_state_message(room, None), exclude={participant.id})
        await self.broadcast(room, {
            "type": PartyEvent.PARTICIPANT_JOINED.value,
            "room_id": room.id,
            "sender": participant.id,
            "seq": msg.seq,
            "timestamp_ms": int(time.time() * 1000),
            "payload": {
                "participant_id": participant.id,
                "nickname": participant.nickname,
                "role": participant.role.value,
            },
        })
        logger.info("Participant %s joined %s", participant.nickname, room.id)
        return room, participant

    def _bind(self, websocket, participant, room):
        self._connections[participant.id] = websocket
        self._participant_rooms[participant.id] = room.id
        self._last_seq[participant.id] = -1
        participant.connected = True
        participant.last_heartbeat = int(time.time() * 1000)
        room.last_activity = int(time.time() * 1000)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    async def route_message(self, room, participant, msg: PartyMessage):
        now_ms = int(time.time() * 1000)

        if msg.type == PartyEvent.READY.value:
            participant.ready = bool(msg.payload.get("ready", True))
            room.last_activity = now_ms
            await self.broadcast(room, self._room_state_message(room, None))
            return

        if msg.type == PartyEvent.BUFFERING.value:
            participant.metadata["buffering"] = bool(msg.payload.get("buffering", True))
            room.last_activity = now_ms
            await self.broadcast(room, self._room_state_message(room, None))
            return

        if msg.type == PartyEvent.CHAT.value:
            if not self._allow_chat(participant.id):
                await self._send_error(
                    self._connections.get(participant.id), "rate_limited", "Slow down"
                )
                return
            text = sanitize_text(msg.payload.get("text", ""))
            await self.broadcast(room, {
                "type": PartyEvent.CHAT.value,
                "room_id": room.id,
                "sender": participant.id,
                "seq": msg.seq,
                "timestamp_ms": now_ms,
                "payload": {
                    "nickname": participant.nickname,
                    "text": text,
                    "timestamp_ms": now_ms,
                },
            })
            return

        if msg.type == PartyEvent.LEAVE_ROOM.value:
            self._connections.pop(participant.id, None)
            self._participant_rooms.pop(participant.id, None)
            self.room_manager.leave_room(room.id, participant.id)
            remaining = self.room_manager.get_room(room.id)
            if remaining:
                await self.broadcast(remaining, self._room_state_message(remaining, None))
                await self.broadcast(remaining, {
                    "type": PartyEvent.PARTICIPANT_LEFT.value,
                    "room_id": remaining.id,
                    "sender": participant.id,
                    "seq": msg.seq,
                    "timestamp_ms": now_ms,
                    "payload": {
                        "participant_id": participant.id,
                        "nickname": participant.nickname,
                    },
                })
            return

        if msg.type == PartyEvent.SYNC_PLAYBACK.value:
            self.room_manager.set_room_state(room.id, "playback", msg.payload)
            await self.broadcast(room, {
                "type": PartyEvent.SYNC_PLAYBACK.value,
                "room_id": room.id,
                "sender": participant.id,
                "seq": msg.seq,
                "timestamp_ms": now_ms,
                "payload": msg.payload,
            })
            return

        if msg.type == PartyEvent.PLAY.value:
            await self.broadcast(room, self._control_message(room, msg, participant, PartyEvent.PLAY))
            return

        if msg.type == PartyEvent.PAUSE.value:
            await self.broadcast(room, self._control_message(room, msg, participant, PartyEvent.PAUSE))
            return

        if msg.type == PartyEvent.SEEK.value:
            await self.broadcast(room, self._control_message(room, msg, participant, PartyEvent.SEEK))
            return

        if msg.type == PartyEvent.EPISODE_CHANGED.value:
            reference = _sanitize_reference(msg.payload.get("anime_reference"))
            playback = _sanitize_playback(msg.payload.get("playback"))
            if reference is None and playback is None:
                return
            if reference is not None:
                room.metadata["anime_reference"] = reference
            room.current_playback = None
            for p in room.participants.values():
                p.ready = p.role == Role.HOST
            await self.broadcast(room, {
                "type": PartyEvent.EPISODE_CHANGED.value,
                "room_id": room.id,
                "sender": participant.id,
                "seq": msg.seq,
                "timestamp_ms": now_ms,
                "payload": _episode_payload(msg),
            })
            await self.broadcast(room, self._room_state_message(room, None))
            return

        if msg.type == PartyEvent.END_ROOM.value:
            await self.broadcast(room, {
                "type": PartyEvent.END_ROOM.value,
                "room_id": room.id,
                "sender": participant.id,
                "seq": msg.seq,
                "timestamp_ms": now_ms,
                "payload": {"reason": "host_ended"},
            })
            for pid in list(room.participants):
                self._connections.pop(pid, None)
            self.room_manager.close_room(room.id)
            return

        if msg.type == PartyEvent.PING.value:
            await self._send_json(self._connections.get(participant.id), {
                "type": PartyEvent.PONG.value,
                "room_id": room.id,
                "sender": participant.id,
                "seq": msg.seq,
                "timestamp_ms": now_ms,
                "payload": {},
            })
            return

        logger.warning("Unhandled message type: %s", msg.type)

    def _control_message(self, room, msg, participant, event) -> Dict[str, Any]:
        return {
            "type": event.value,
            "room_id": room.id,
            "sender": participant.id,
            "seq": msg.seq,
            "timestamp_ms": int(time.time() * 1000),
            "payload": msg.payload,
        }

    def _allow_chat(self, participant_id: str) -> bool:
        now = time.monotonic()
        times = self._chat_times.setdefault(participant_id, [])
        times[:] = [t for t in times if now - t < CHAT_WINDOW_SECONDS]
        if len(times) >= CHAT_MAX_MESSAGES:
            return False
        times.append(now)
        return True

    # ------------------------------------------------------------------
    # Outbound helpers
    # ------------------------------------------------------------------
    def _room_state_message(self, room, participant) -> Dict[str, Any]:
        payload = self.room_manager.snapshot(room)
        if participant is not None:
            payload.update({
                "you": {
                    "participant_id": participant.id,
                    "token": participant.token,
                    "nickname": participant.nickname,
                    "role": participant.role.value,
                }
            })
        return {
            "type": PartyEvent.ROOM_STATE.value,
            "room_id": room.id,
            "sender": room.host_id,
            "seq": 0,
            "timestamp_ms": int(time.time() * 1000),
            "payload": payload,
        }

    async def broadcast(self, room, message: Dict[str, Any], exclude=None):
        exclude = exclude or set()
        for participant_id in list(room.participants):
            if participant_id in exclude:
                continue
            ws = self._connections.get(participant_id)
            if ws is None:
                continue
            try:
                await ws.send(json.dumps(message))
            except ConnectionClosed:
                self._connections.pop(participant_id, None)

    async def _send_json(self, websocket, message: Optional[Dict[str, Any]]):
        if websocket is None or message is None:
            return
        try:
            await websocket.send(json.dumps(message))
        except ConnectionClosed:
            pass

    async def _send_error(self, websocket, code: str, description: str):
        await self._send_json(websocket, {
            "type": PartyEvent.ERROR.value,
            "room_id": "",
            "sender": "server",
            "seq": 0,
            "timestamp_ms": int(time.time() * 1000),
            "payload": {"code": code, "description": description},
        })


async def serve_forever(host: str = "0.0.0.0", port: int = 8765, **kwargs):
    server = WatchTogetherServer(host=host, port=port, **kwargs)
    await server.start()
    print(f"Watch Together relay listening on {server.address}")
    print("Press Ctrl+C to stop.")
    try:
        await asyncio.get_running_loop().create_future()
    finally:
        await server.stop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Watch Together relay server")
    parser.add_argument("--host", default="0.0.0.0", help="Host address")
    parser.add_argument("--port", type=int, default=8765, help="Port")
    parser.add_argument("--max-participants", type=int, default=MAX_PARTICIPANTS)
    parser.add_argument("--room-expiration", type=int, default=ROOM_EXPIRATION_MINUTES * 60,
                        help="Room inactivity timeout in seconds")
    parser.add_argument("--host-grace", type=int, default=HOST_RECONNECTION_GRACE,
                        help="Host reconnection grace period in seconds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve_forever(
            host=args.host,
            port=args.port,
            max_participants=args.max_participants,
            room_expiration_minutes=max(1, args.room_expiration // 60),
            host_reconnection_grace=args.host_grace,
        ))
    except KeyboardInterrupt:
        pass
