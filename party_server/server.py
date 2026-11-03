"""
Watch Together relay server.
Implements a simple WebSocket server for room management and message routing.
"""
import asyncio
import json
import time
import logging
import urllib.parse
from typing import Dict, Optional

from websockets.asyncio.server import serve
from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed

from nyoanime.party.rooms import RoomManager
from nyoanime.party.protocol import PartyMessage, PartyEvent
from nyoanime.party.models import Room, Participant
from .config import MAX_MESSAGE_SIZE, HEARTBEAT_TIMEOUT_MS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_MESSAGE_SIZE = 65536
HEARTBEAT_TIMEOUT_MS = 30_000  # 30 seconds

class WatchTogetherServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8765,
                 max_participants: int = 8, room_expiration_minutes: int = 10,
                 host_reconnection_grace: int = 60):
        self.host = host
        self.port = port
        self.room_manager = RoomManager()
        # Configure room manager defaults (could be used to create default settings)
        self.settings = {
            "max_participants": max_participants,
            "room_expiration_ms": room_expiration_minutes * 60 * 1000,
            "host_reconnection_grace_ms": host_reconnection_grace * 1000,
        }
        self._server = None
        self._connections: Dict[str, WebSocketServerProtocol] = {}

    async def start(self):
        """Start the WebSocket server."""
        self._server = await serve(
            self.handle_connection,
            self.host,
            self.port,
            max_size=MAX_MESSAGE_SIZE,
            ping_interval=20,
            ping_timeout=30,
        )
        logger.info(f"Watch Together relay server started on ws://{self.host}:{self.port}")

    async def stop(self):
        """Stop the server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Watch Together relay server stopped.")

    async def handle_connection(self, websocket: WebSocketServerProtocol, path: str):
        """Handle a new WebSocket connection."""
        import urllib.parse
        query = urllib.parse.parse_qs(path.split("?", 1)[1] if "?" in path else "")
        room_code = query.get("room", [None])[0]
        token = query.get("token", [None])[0]
        nickname = query.get("nickname", [None])[0]

        if not (room_code and token and nickname):
            logger.warning("Incomplete connection parameters; closing.")
            await websocket.close(code=1008, reason="Missing parameters")
            return

        room = self.room_manager.get_room(room_code)
        if not room:
            logger.warning(f"Room {room_code} not found.")
            await websocket.close(code=1006, reason="Room not found")
            return

        participant_id = f"{room_code}_{hash(nickname)}"
        participant = self._get_participant(room, participant_id)
        if not participant or participant.token != token:
            logger.warning(f"Invalid token for participant in room {room_code}.")
            await websocket.close(code=1008, reason="Invalid token")
            return

        # Register connection
        self._connections[participant_id] = websocket
        participant.connected = True
        participant.last_heartbeat = int(time.time() * 1000)

        logger.info(f"Participant {nickname} joined room {room_code}")

        try:
            async for message in websocket:
                participant.last_heartbeat = int(time.time() * 1000)
                try:
                    data = json.loads(message)
                    party_msg = PartyMessage.from_dict(data)
                except Exception as e:
                    logger.warning(f"Invalid message format: {e}")
                    continue

                await self.route_message(room, participant, party_msg)

        except ConnectionClosed:
            logger.info(f"Connection closed for {participant.nickname} in room {room_code}")
        finally:
            participant.connected = False
            self._connections.pop(participant_id, None)
            self.room_manager.leave_room(room_code, participant_id)
            logger.info(f"Participant {participant.nickname} left room {room_code}")

    def _get_participant(self, room: Room, participant_id: str) -> Optional["Participant"]:
        return room.participants.get(participant_id)

    async def route_message(self, room: Room, participant: Participant, msg: PartyMessage):
        """Route incoming message to appropriate handler."""
        try:
            msg.validate()
        except ValueError as e:
            logger.warning(f"Message validation failed: {e}")
            await self.send_to(participant, {
                "type": PartyEvent.ERROR.value,
                "room_id": room.id,
                "sender": participant.id,
                "seq": msg.seq,
                "timestamp_ms": int(time.time() * 1000),
                "payload": {"code": "invalid_message", "description": str(e)},
            })
            return

        if msg.type == PartyEvent.READY.value:
            if self.room_manager.update_participant(room.id, participant.id, ready=True):
                # Broadcast participant joined
                self.broadcast(room, {
                    "type": PartyEvent.PARTICIPANT_JOINED.value,
                    "room_id": room.id,
                    "sender": participant.id,
                    "seq": msg.seq,
                    "timestamp_ms": int(time.time() * 1000),
                    "payload": {"nickname": participant.nickname, "role": participant.role.value},
                })
        elif msg.type == PartyEvent.CHAT.value:
            self.broadcast(room, {
                "type": PartyEvent.CHAT.value,
                "room_id": room.id,
                "sender": participant.id,
                "seq": msg.seq,
                "timestamp_ms": int(time.time() * 1000),
                "payload": msg.payload,
            })
        elif msg.type == PartyEvent.SYNC_PLAYBACK.value:
            if self.room_manager.set_room_state(room.id, "playback", msg.payload):
                self.broadcast(room, {
                    "type": PartyEvent.SYNC_PLAYBACK.value,
                    "room_id": room.id,
                    "sender": participant.id,
                    "seq": msg.seq,
                    "timestamp_ms": int(time.time() * 1000),
                    "payload": msg.payload,
                })
        elif msg.type == PartyEvent.PING.value:
            await self.send_to(participant, {
                "type": PartyEvent.PONG.value,
                "room_id": room.id,
                "sender": participant.id,
                "seq": msg.seq,
                "timestamp_ms": int(time.time() * 1000),
                "payload": {},
            })
        else:
            logger.warning(f"Unhandled message type: {msg.type}")

    def _get_websocket(self, participant_id: str) -> Optional[WebSocketServerProtocol]:
        """Return the websocket for a given participant_id if connected."""
        return self._connections.get(participant_id)

    def _get_participant(self, room_id: str, participant_id: str) -> Optional["Participant"]:
        """Return participant object if participant belongs to the room."""
        room = self.room_manager.get_room(room_id)
        if not room or participant_id not in room.participants:
            return None
        return room.participants[participant_id]

    async def broadcast(self, room: Room, message: Dict):
        """Send a message to all connected participants in the room."""
        for participant_id, participant in room.participants.items():
            ws = self._get_websocket(participant_id)
            if ws:
                try:
                    await ws.send(json.dumps(message))
                except ConnectionClosed:
                    logger.warning(f"Failed to send to participant {participant_id}")
                    self._connections.pop(participant_id, None)

    async def send_to(self, participant: Participant, message: Dict):
        """Send a message to a specific participant."""
        ws = self._get_websocket(participant.id)
        if ws:
            try:
                await ws.send(json.dumps(message))
            except ConnectionClosed:
                logger.warning(f"Failed to send to participant {participant.id}")
                self._connections.pop(participant.id, None)

    async def cleanup(self):
        """Clean up expired rooms and stale connections."""
        self.room_manager.cleanup_expired_rooms()
        now = int(time.time() * 1000)
        for pid, ws in list(self._connections.items()):
            # Find participant via room search (simplified)
            # If connection is stale, close it
            # For simplicity, we assume participant object is stored elsewhere; skip for now.
            pass

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Watch Together relay server")
    parser.add_argument("--host", default="0.0.0.0", help="Host address")
    parser.add_argument("--port", type=int, default=8765, help="Port")
    parser.add_argument("--max-participants", type=int, default=8, help="Maximum participants per room")
    parser.add_argument("--room-expiration", type=int, default=600, help="Room expiration in seconds")
    parser.add_argument("--host-grace", type=int, default=60, help="Host reconnection grace period in seconds")
    args = parser.parse_args()

    server = WatchTogetherServer(
        host=args.host,
        port=args.port,
        max_participants=args.max_participants,
        room_expiration_minutes=args.room_expiration // 60,
        host_reconnection_grace=args.host_grace,
    )
    asyncio.run(server.start())