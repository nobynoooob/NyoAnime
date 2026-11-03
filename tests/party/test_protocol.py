"""
Unit tests for protocol messages.
"""
import pytest
from nyoanime.party.protocol import PartyMessage, PartyEvent

def test_message_serialization():
    msg = PartyMessage(
        type=PartyEvent.READY.value,
        room_id="ROOM123",
        sender="user1",
        seq=5,
        timestamp_ms=1731500000,
        payload={"ready": True},
    )
    data = msg.to_dict()
    assert data["type"] == PartyEvent.READY.value
    assert data["room_id"] == "ROOM123"
    assert data["sender"] == "user1"
    assert data["seq"] == 5

    # Round-trip
    parsed = PartyMessage.from_dict(data)
    assert parsed.type == msg.type
    assert parsed.room_id == msg.room_id
    assert parsed.sender == msg.sender
    assert parsed.seq == msg.seq
    assert parsed.timestamp_ms == msg.timestamp_ms
    assert parsed.payload == msg.payload

def test_validation_invalid_type():
    msg = PartyMessage(
        type="invalid_type",
        room_id="ROOM",
        sender="user",
        seq=0,
        timestamp_ms=0,
        payload={},
    )
    with pytest.raises(ValueError):
        msg.validate()

def test_validation_empty_room_id():
    msg = PartyMessage(
        type=PartyEvent.READY.value,
        room_id="",
        sender="user",
        seq=0,
        timestamp_ms=0,
        payload={},
    )
    with pytest.raises(ValueError):
        msg.validate()

def test_message_allowed_types():
    for et in PartyEvent:
        msg = PartyMessage(
            type=et.value,
            room_id="ROOM",
            sender="user",
            seq=0,
            timestamp_ms=0,
            payload={},
        )
        # Should not raise
        msg.validate()
        assert msg.type == et.value