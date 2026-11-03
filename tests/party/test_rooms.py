"""
Basic unit tests for Watch Together RoomManager.
"""
import time
import pytest
from nyoanime.party.rooms import RoomManager
from nyoanime.party.models import RoomSettings, Participant, Role

def test_room_creation():
    manager = RoomManager()
    room = manager.create_room("host1", RoomSettings())
    # Room ID is a generated 5‑character uppercase code
    assert isinstance(room.id, str)
    assert len(room.id) == 5
    assert room.id.isupper()
    assert room.host_id == "host1"
    assert len(room.participants) == 1
    host = list(room.participants.values())[0]
    assert host.role == Role.HOST
    assert host.token is not None

def test_room_join():
    manager = RoomManager()
    host = manager.create_room("host1")
    # Find host participant id (since it's added automatically)
    host_id = next(iter(host.participants.keys()))
    participant = manager.join_room(host.id, "guest1", "guest", "token123")
    assert participant is not None
    assert participant.id == "guest1"
    assert participant.role == Role.GUEST
    assert participant.nickname == "guest"
    # Check room participants count increased
    assert len(manager.get_room(host.id).participants) == 2

def test_room_leave():
    manager = RoomManager()
    room = manager.create_room("host1")
    # Use host participant id (it should be present)
    host_id = next(iter(room.participants.keys()))
    # Leave (but we need to use leave_room method)
    success = manager.leave_room(room.id, host_id)
    assert success
    # Host should be removed from participants
    assert host_id not in manager.get_room(room.id).participants

def test_room_full():
    manager = RoomManager()
    room = manager.create_room("host1")
    # Fill up participants (max 8, host + 7 guests)
    for i in range(7):
        pid = f"guest{i}"
        participant = manager.join_room(room.id, pid, f"guest{i}", f"token{i}")
        assert participant is not None
    # Room now has host + 7 guests = 8 participants
    assert len(manager.get_room(room.id).participants) == 8
    # Try to add one more guest (room full)
    participant = manager.join_room(room.id, "guest7", "guest7", "token7")
    assert participant is None  # Should be None because room is full
    # Verify participants count unchanged
    assert len(manager.get_room(room.id).participants) == 8

def test_room_lock():
    # Not implemented yet
    pass

def test_room_cleanup():
    manager = RoomManager()
    # Create a room with no current playback to trigger expiration
    settings = RoomSettings()
    settings.room_expiration_ms = 1  # 1 ms
    room = manager.create_room("host1", settings)
    # Manually set current_playback to None (already by default)
    # Wait for expiration
    time.sleep(0.01)
    manager.cleanup_expired_rooms()
    # Room should be removed
    assert manager.get_room(room.id) is None