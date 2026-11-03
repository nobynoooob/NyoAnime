import time
import secrets
import string
from typing import Dict, Optional, Any
from .models import Room, RoomSettings, Participant, Role
from .protocol import PartyMessage, PartyEvent

class RoomManager:
    """In‑memory room store with lifecycle management."""

    def __init__(self):
        self._rooms: Dict[str, Room] = {}
        # Secure random room code alphabet (exclude I,O,L,1,0)
        self._alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789abcdefghijkmnpqrstuvwxyz3456789"
        self._code_length = 5

    def _generate_room_code(self) -> str:
        while True:
            code = ''.join(secrets.choice(self._alphabet) for _ in range(self._code_length))
            if code not in self._rooms:
                return code.upper()

    def create_room(self, host_id: str, settings: Optional[RoomSettings] = None) -> Room:
        code = self._generate_room_code()
        if settings is None:
            settings = RoomSettings()
        now = int(time.time() * 1000)
        room = Room(
            id=code,
            host_id=host_id,
            settings=settings,
            participants={},
            current_playback=None,
            created_at=now,
            last_activity=now,
        )
        # Add host as participant
        host_token = secrets.token_urlsafe(32)
        room.participants[host_id] = Participant(
            id=host_id,
            nickname="Host",  # will be overridden by client's nickname later
            role=Role.HOST,
            token=host_token,
            ready=False,
            connected=True,
            last_heartbeat=int(time.time() * 1000),
        )
        self._rooms[code] = room
        return room

    def get_room(self, room_id: str) -> Optional[Room]:
        return self._rooms.get(room_id)

    def join_room(self, room_id: str, participant_id: str, nickname: str, token: str) -> Optional[Participant]:
        room = self._rooms.get(room_id)
        if not room:
            return None
        if len(room.participants) >= room.settings.max_participants:
            return None  # Room is full
        if room.settings.locked and room.host_id != participant_id:
            return None  # Room is locked
        if participant_id in room.participants:
            # Existing participant (reconnection)
            part = room.participants[participant_id]
            part.connected = True
            part.last_heartbeat = int(time.time() * 1000)
            part.nickname = nickname
            part.token = token
            return part
        # New participant
        participant = Participant(
            id=participant_id,
            nickname=nickname,
            role=Role.GUEST,
            token=token,
            ready=False,
            connected=True,
            last_heartbeat=int(time.time() * 1000),
        )
        room.participants[participant_id] = participant
        return participant

    def leave_room(self, room_id: str, participant_id: str) -> bool:
        room = self._rooms.get(room_id)
        if not room or participant_id not in room.participants:
            return False
        del room.participants[participant_id]
        # If the host leaves, promote the first guest to host (optional, spec says keep host)
        if room.host_id == participant_id:
            # Find a guest to promote
            guests = [pid for pid, p in room.participants.items() if p.role == Role.GUEST]
            if guests:
                new_host = guests[0]
                room.participants[new_host].role = Role.HOST
                room.host_id = new_host
        self.cleanup_expired_rooms()
        return True

    def update_participant(self, room_id: str, participant_id: str, **updates) -> bool:
        room = self._rooms.get(room_id)
        if not room or participant_id not in room.participants:
            return False
        for key, value in updates.items():
            if hasattr(room.participants[participant_id], key):
                setattr(room.participants[participant_id], key, value)
        room.last_activity = int(time.time() * 1000)
        return True

    def set_room_state(self, room_id: str, key: str, value: Any) -> bool:
        room = self._rooms.get(room_id)
        if not room:
            return False
        if key == "locked":
            room.settings.locked = bool(value)
        elif key == "playback":
            room.current_playback = value
        else:
            return False
        room.last_activity = int(time.time() * 1000)
        return True

    def set_room_state(self, room_id: str, key: str, value: Any) -> bool:
        room = self._rooms.get(room_id)
        if not room:
            return False
        if key == "playback":
            room.current_playback = value
        else:
            return False
        room.last_activity = int(time.time() * 1000)
        return True

    def cleanup_expired_rooms(self) -> None:
        now = int(time.time() * 1000)
        expired = []
        for code, room in self._rooms.items():
            if room.current_playback is None and (now - room.created_at) > room.settings.room_expiration_ms:
                expired.append(code)
            elif room.current_playback is not None and (now - room.last_activity) > room.settings.room_expiration_ms:
                expired.append(code)
        for code in expired:
            del self._rooms[code]

    def iterate_rooms(self) -> Dict[str, Room]:
        return dict(self._rooms)