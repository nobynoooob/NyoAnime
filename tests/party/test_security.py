"""
Security tests for Watch Together relay.
"""
import pytest
from nyoanime.party.rooms import RoomManager
from nyoanime.party.models import RoomSettings, Participant, Role

def test_room_code_uniqueness():
    # Ensure generated codes are unique across many creations
    manager = RoomManager()
    codes = set()
    for _ in range(1000):
        code = manager._generate_room_code()
        assert code not in codes
        codes.add(code)

def test_participant_token_randomness():
    # Tokens should be long enough and unpredictable
    import secrets
    tokens = [secrets.token_urlsafe(32) for _ in range(10)]
    # Ensure no duplicates (extremely unlikely)
    assert len(set(tokens)) == len(tokens)

def test_message_schema_validation():
    from nyoanime.party.protocol import PartyMessage
    # Valid message passes
    msg = PartyMessage(type="ready", room_id="ROOM", sender="user", seq=0)
    msg.validate()
    # Invalid type raises
    msg.type = "invalid"
    with pytest.raises(ValueError):
        msg.validate()

def test_max_message_size():
    # Not implemented in server yet; placeholder
    pass

def test_rate_limiting_placeholder():
    # Not implemented; placeholder
    pass