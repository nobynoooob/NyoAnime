import secrets
import time
from typing import Dict, Optional, Any

from .models import Room, RoomSettings, Participant, Role


class RoomError(Exception):
    """Base error for room operations."""

    code = "room_error"

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        if code:
            self.code = code


class RoomNotFound(RoomError):
    code = "room_not_found"


class RoomFull(RoomError):
    code = "room_full"


class RoomLocked(RoomError):
    code = "room_locked"


class InvalidToken(RoomError):
    code = "invalid_token"


class RoomManager:
    """In-memory room store with lifecycle management."""

    def __init__(self):
        self._rooms: Dict[str, Room] = {}
        # Exclude visually ambiguous characters.
        self._alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        self._code_length = 5

    def _generate_room_code(self) -> str:
        while True:
            code = "".join(secrets.choice(self._alphabet) for _ in range(self._code_length))
            if code not in self._rooms:
                return code

    def create_room(
        self,
        host_id: str,
        settings: Optional[RoomSettings] = None,
        nickname: str = "Host",
        token: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Room:
        code = self._generate_room_code()
        settings = settings or RoomSettings()
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
        room.metadata.update(metadata or {})
        room.participants[host_id] = Participant(
            id=host_id,
            nickname=nickname,
            role=Role.HOST,
            token=token or secrets.token_urlsafe(32),
            ready=False,
            connected=True,
            last_heartbeat=now,
        )
        self._rooms[code] = room
        return room

    def get_room(self, room_id: str) -> Optional[Room]:
        return self._rooms.get(str(room_id or "").strip().upper())

    def join_room(
        self,
        room_id: str,
        participant_id: str,
        nickname: str,
        token: str,
        role: Role = Role.GUEST,
    ) -> Optional[Participant]:
        room = self.get_room(room_id)
        if not room:
            raise RoomNotFound("Room not found")
        if room.settings.locked and room.host_id != participant_id:
            return None
        if participant_id in room.participants:
            existing = room.participants[participant_id]
            if token and existing.token != token:
                raise InvalidToken("Participant token mismatch")
            existing.connected = True
            existing.last_heartbeat = int(time.time() * 1000)
            if nickname:
                existing.nickname = nickname
            room.last_activity = int(time.time() * 1000)
            return existing
        if len(room.participants) >= room.settings.max_participants:
            return None
        participant = Participant(
            id=participant_id,
            nickname=nickname,
            role=role,
            token=token or secrets.token_urlsafe(32),
            ready=False,
            connected=True,
            last_heartbeat=int(time.time() * 1000),
        )
        room.participants[participant_id] = participant
        room.last_activity = int(time.time() * 1000)
        return participant

    def reconnect(self, room_id: str, participant_id: str, token: str) -> Optional[Participant]:
        room = self.get_room(room_id)
        if not room:
            raise RoomNotFound("Room not found")
        participant = room.participants.get(participant_id)
        if not participant:
            raise InvalidToken("Unknown participant")
        if not token or participant.token != token:
            raise InvalidToken("Participant token mismatch")
        participant.connected = True
        participant.last_heartbeat = int(time.time() * 1000)
        room.last_activity = int(time.time() * 1000)
        return participant

    def mark_disconnected(self, room_id: str, participant_id: str) -> Optional[Participant]:
        room = self.get_room(room_id)
        if not room:
            return None
        participant = room.participants.get(participant_id)
        if participant:
            participant.connected = False
            participant.last_heartbeat = int(time.time() * 1000)
            room.last_activity = int(time.time() * 1000)
        return participant

    def leave_room(self, room_id: str, participant_id: str) -> bool:
        room = self.get_room(room_id)
        if not room or participant_id not in room.participants:
            return False
        del room.participants[participant_id]
        if room.host_id == participant_id:
            guests = [pid for pid, p in room.participants.items() if p.role == Role.GUEST]
            if guests:
                new_host = guests[0]
                room.participants[new_host].role = Role.HOST
                room.host_id = new_host
        room.last_activity = int(time.time() * 1000)
        return True

    def close_room(self, room_id: str) -> bool:
        return self._rooms.pop(str(room_id or "").strip().upper(), None) is not None

    def update_participant(self, room_id: str, participant_id: str, **updates) -> bool:
        room = self.get_room(room_id)
        if not room or participant_id not in room.participants:
            return False
        participant = room.participants[participant_id]
        for key, value in updates.items():
            if hasattr(participant, key):
                setattr(participant, key, value)
        room.last_activity = int(time.time() * 1000)
        return True

    def set_room_state(self, room_id: str, key: str, value: Any) -> bool:
        room = self.get_room(room_id)
        if not room:
            return False
        if key == "locked":
            room.settings.locked = bool(value)
        elif key == "playback":
            room.current_playback = value
        elif key == "episode":
            room.metadata["anime_reference"] = value
        room.last_activity = int(time.time() * 1000)
        return True

    def cleanup_expired_rooms(self) -> None:
        now = int(time.time() * 1000)
        expired = [
            code
            for code, room in self._rooms.items()
            if (now - room.last_activity) > room.settings.room_expiration_ms
        ]
        for code in expired:
            del self._rooms[code]

    def iterate_rooms(self) -> Dict[str, Room]:
        return dict(self._rooms)

    @staticmethod
    def participants_snapshot(room: Room) -> list:
        return [
            {
                "id": p.id,
                "nickname": p.nickname,
                "role": p.role.value,
                "ready": p.ready,
                "connected": p.connected,
            }
            for p in room.participants.values()
        ]

    def snapshot(self, room: Room) -> Dict[str, Any]:
        host = room.participants.get(room.host_id)
        return {
            "room_code": room.id,
            "host_id": room.host_id,
            "host_nickname": host.nickname if host else "",
            "participants": self.participants_snapshot(room),
            "anime_reference": room.metadata.get("anime_reference"),
            "playback": room.current_playback,
            "locked": room.settings.locked,
            "max_participants": room.settings.max_participants,
        }
