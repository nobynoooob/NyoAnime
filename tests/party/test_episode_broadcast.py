"""Client-level episode broadcast and late-subscriber replay."""
import asyncio

import pytest

from nyoanime.party.client import WTRClient
from nyoanime.party.protocol import PartyEvent, PartyMessage
from nyoanime.party.transport import sanitize_payload

ROOM_ID = "11111111-1111-4111-8111-111111111111"


class FakeTransport:
    def __init__(self):
        self.handlers = {}
        self.sent = []
        self.peer = None
        self.connected = True
        self.playback_state = None
        self.playback_reads = 0

    def is_connected(self):
        return self.connected

    async def get_playback_state(self):
        self.playback_reads += 1
        return self.playback_state

    def subscribe(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def unsubscribe(self, event, callback):
        handlers = self.handlers.get(event, [])
        if callback in handlers:
            handlers.remove(callback)

    async def send_message(self, msg_type, payload):
        self.sent.append((msg_type, payload))
        if self.peer is not None:
            self.peer.deliver(msg_type, payload)

    def deliver(self, msg_type, payload, sender="host"):
        clean = sanitize_payload(
            payload,
            allow_playback=msg_type == PartyEvent.EPISODE_CHANGED.value,
        )
        message = PartyMessage(
            type=msg_type,
            room_id=ROOM_ID,
            sender=sender,
            payload=clean,
        )
        for callback in list(self.handlers.get(msg_type, [])):
            callback(message)


def make_client(role="guest"):
    client = WTRClient("ws://relay.test", "Tester")
    client.role = role
    client.room_code = "123456"
    client.room_id = ROOM_ID
    client.participant_id = "p_test"
    transport = FakeTransport()
    client._transport = transport
    return client, transport


@pytest.mark.asyncio
async def test_broadcast_playback_delivers_episode_changed_to_the_guest_handler():
    host, host_transport = make_client("host")
    guest, guest_transport = make_client("guest")
    host_transport.peer = guest_transport

    received = []
    guest.subscribe(PartyEvent.EPISODE_CHANGED.value, received.append)

    await host.broadcast_playback(
        {
            "url": "https://cdn.example/episode.m3u8?token=playable",
            "title": "Frieren - Ep 3",
            "episode_number": "3",
            "headers": {
                "Referer": "https://provider.example/",
                "Cookie": "session=secret",
                "Authorization": "Bearer secret",
            },
        },
        {"title": "Frieren", "episode_number": "3"},
    )

    assert len(received) == 1
    message = received[0]
    assert message.type == PartyEvent.EPISODE_CHANGED.value
    assert message.payload["anime_reference"]["episode_number"] == "3"
    playback = message.payload["playback"]
    assert playback["url"] == "https://cdn.example/episode.m3u8?token=playable"
    assert playback["headers"] == {"Referer": "https://provider.example/"}
    assert message.payload["stream_url"] == playback["url"]
    assert message.payload["headers"] == {"Referer": "https://provider.example/"}


@pytest.mark.asyncio
async def test_guest_subscribing_after_the_broadcast_still_gets_the_episode():
    guest, _ = make_client("guest")
    payload = {
        "anime_reference": {"title": "Frieren", "episode_number": "3"},
        "playback": {"url": "https://cdn.example/episode.m3u8", "episode_number": "3"},
    }
    guest._remember_episode(
        PartyMessage(
            type=PartyEvent.EPISODE_CHANGED.value,
            room_id=ROOM_ID,
            sender="host",
            payload=payload,
        )
    )

    received = []
    guest.subscribe(PartyEvent.EPISODE_CHANGED.value, received.append)

    assert len(received) == 1
    assert received[0].payload["playback"]["url"] == "https://cdn.example/episode.m3u8"


@pytest.mark.asyncio
async def test_broadcast_playback_rejects_a_missing_stream():
    host, _ = make_client("host")

    with pytest.raises(Exception) as excinfo:
        await host.broadcast_playback({})

    assert "No playable stream" in str(excinfo.value)


@pytest.mark.asyncio
async def test_guest_poll_recovers_an_episode_when_the_broadcast_is_missed():
    guest, transport = make_client("guest")
    guest._transport_kind = "supabase"
    transport.playback_state = {
        "playback": {
            "url": "https://cdn.example/episode.m3u8",
            "title": "Frieren - Ep 3",
            "episode_number": "3",
            "headers": {"Referer": "https://provider.example/"},
        },
        "anime_reference": {"title": "Frieren", "episode_number": "3"},
        "sender": "host",
        "seq": 7,
    }
    received = []
    guest.subscribe(PartyEvent.EPISODE_CHANGED.value, received.append)

    guest.start_playback_poll(interval=0.05)
    try:
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.02)
    finally:
        guest.stop_playback_poll()

    assert transport.playback_reads >= 1
    assert len(received) == 1
    message = received[0]
    assert message.type == PartyEvent.EPISODE_CHANGED.value
    assert message.payload["playback"]["url"] == "https://cdn.example/episode.m3u8"
    assert message.payload["stream_url"] == "https://cdn.example/episode.m3u8"
    assert message.payload["episode_number"] == "3"


@pytest.mark.asyncio
async def test_poll_does_not_replay_an_episode_already_received_over_realtime():
    guest, transport = make_client("guest")
    guest._transport_kind = "supabase"
    payload = {
        "anime_reference": {"title": "Frieren", "episode_number": "3"},
        "playback": {"url": "https://cdn.example/episode.m3u8", "episode_number": "3"},
    }
    received = []
    guest.subscribe(PartyEvent.EPISODE_CHANGED.value, received.append)
    guest._remember_episode(
        PartyMessage(
            type=PartyEvent.EPISODE_CHANGED.value,
            room_id=ROOM_ID,
            sender="host",
            payload=payload,
        )
    )
    transport.playback_state = {
        "playback": payload["playback"],
        "anime_reference": payload["anime_reference"],
        "sender": "host",
        "seq": 9,
    }

    guest.start_playback_poll(interval=0.05)
    try:
        await asyncio.sleep(0.16)
    finally:
        guest.stop_playback_poll()

    assert transport.playback_reads >= 1
    assert received == []
