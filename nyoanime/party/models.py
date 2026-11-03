from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List
from enum import Enum

class Role(str, Enum):
    HOST = "host"
    GUEST = "guest"

@dataclass
class Participant:
    id: str
    nickname: str
    role: Role
    token: str
    ready: bool = False
    connected: bool = True
    last_heartbeat: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class RoomSettings:
    max_participants: int = 8
    room_expiration_ms: int = 10 * 60 * 1000  # 10 minutes
    host_reconnection_grace_ms: int = 60 * 1000  # 60 seconds
    locked: bool = False
    language_mode: str = "ar_sub"

@dataclass
class PlaybackState:
    position: float = 0.0
    duration: float = 0.0
    paused: bool = False
    speed: float = 1.0
    anime_reference: Optional[Dict[str, Any]] = None
    sequence: int = 0

@dataclass
class Room:
    id: str
    host_id: str
    settings: RoomSettings
    participants: Dict[str, Participant]
    current_playback: Optional[PlaybackState] = None
    created_at: int = 0
    last_activity: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)