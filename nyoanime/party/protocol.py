from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict
import json

class PartyEvent(str, Enum):
    CREATE_ROOM = "create_room"
    JOIN_ROOM = "join_room"
    LEAVE_ROOM = "leave_room"
    PARTICIPANT_JOINED = "participant_joined"
    PARTICIPANT_LEFT = "participant_left"
    SYNC_PLAYBACK = "sync_playback"
    PLAY = "play"
    PAUSE = "pause"
    SEEK = "seek"
    EPISODE_CHANGED = "episode_changed"
    READY = "ready"
    BUFFERING = "buffering"
    CHAT = "chat"
    KICK = "kick"
    LOCK_ROOM = "lock_room"
    ROOM_STATE = "room_state"
    END_ROOM = "end_room"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"

@dataclass
class PartyMessage:
    type: str
    room_id: str
    sender: str
    seq: int = 0
    timestamp_ms: int = 0
    protocol_version: int = 1
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "room_id": self.room_id,
            "sender": self.sender,
            "seq": self.seq,
            "timestamp_ms": self.timestamp_ms,
            "protocol_version": self.protocol_version,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PartyMessage":
        return cls(
            type=data["type"],
            room_id=data["room_id"],
            sender=data["sender"],
            seq=data.get("seq", 0),
            timestamp_ms=data.get("timestamp_ms", 0),
            protocol_version=data.get("protocol_version", 1),
            payload=data.get("payload", {}),
        )

    def validate(self) -> None:
        if self.type not in [e.value for e in PartyEvent]:
            raise ValueError(f"Invalid message type: {self.type}")
        if not self.room_id:
            raise ValueError("Room ID cannot be empty")
        if not self.sender:
            raise ValueError("Sender cannot be empty")
        # Additional schema validation can be added per type in router