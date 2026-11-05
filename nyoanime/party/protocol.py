from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict
import re


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

    @classmethod
    def from_str(cls, value: str) -> "PartyEvent":
        return cls(value)


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_CHAT_LENGTH = 500

# Diagnostics are shown to users and written to logs. Error payloads can echo
# request metadata, so scrub anything token-shaped before it leaves this layer.
_DIAGNOSTIC_PATTERNS = (
    re.compile(r"(?i)\b(?:sb_publishable|sb_secret|service_role|anon)_[A-Za-z0-9._-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(access_token|refresh_token|apikey|api_key|authorization|token|secret|password)"
        r"(\s*[:=]\s*)([\"']?)[^\"'\s,&}]+"
    ),
    re.compile(
        r"(?i)([?&](?:apikey|token|access_token|refresh_token|key|secret)=)[^&\s]+"
    ),
    re.compile(r"\b[A-Za-z0-9_-]{48,}\b"),
)


def sanitize_diagnostic(value: Any, limit: int = 400) -> str:
    """Redact tokens/keys and clamp an error string for UI diagnostics."""
    text = str(value or "")
    text = text.replace("\x1b", "").replace("\r", " ")
    text = CONTROL_CHARS.sub("", text)
    text = re.sub(r"(?i)\b(?:https?|wss?)://[^\s/@]+:[^\s/@]+@", "[url]@", text)
    for pattern in _DIAGNOSTIC_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda m: f"{m.group(1)}[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return " ".join(text.split())[:limit]


def sanitize_text(value: Any, limit: int = MAX_CHAT_LENGTH) -> str:
    """Strip terminal escape sequences and clamp length. Chat is untrusted input."""
    text = str(value or "")
    text = text.replace("\x1b", "").replace("\r", " ")
    text = CONTROL_CHARS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


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
        if not isinstance(data, dict):
            raise ValueError("Message must be a JSON object")
        return cls(
            type=data["type"],
            room_id=data["room_id"],
            sender=data["sender"],
            seq=int(data.get("seq", 0) or 0),
            timestamp_ms=int(data.get("timestamp_ms", 0) or 0),
            protocol_version=int(data.get("protocol_version", 1) or 1),
            payload=data.get("payload") or {},
        )

    def validate(self) -> None:
        if self.type not in [e.value for e in PartyEvent]:
            raise ValueError(f"Invalid message type: {self.type}")
        if not self.room_id:
            raise ValueError("Room ID cannot be empty")
        if not self.sender:
            raise ValueError("Sender cannot be empty")
        if self.seq < 0:
            raise ValueError("Sequence must be non-negative")
        if not isinstance(self.payload, dict):
            raise ValueError("Payload must be an object")
