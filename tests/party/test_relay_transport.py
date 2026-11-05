"""End-to-end relay tests over a real websocket server.

These cover the transport contract the in-memory fakes cannot: the relay
requires every frame to carry routing fields (room_id, sender, seq).
"""
import asyncio

import pytest

from nyoanime.party.protocol import PartyEvent
from nyoanime.party.transport import WebSocketRelayTransport
from party_server.server import WatchTogetherServer


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.mark.asyncio
async def test_host_broadcast_reaches_guest_stream_handler():
    server = WatchTogetherServer(host="127.0.0.1", port=0)
    await server.start()
    host = WebSocketRelayTransport(server.address)
    guest = WebSocketRelayTransport(server.address)
    received = []
    try:
        await host.connect("", "")
        await guest.connect("", "")
        created = await host.create_room("Host")
        joined = await guest.join_room(created["room_code"], "Guest")

        assert created["room_id"] == created["room_code"]
        assert joined["room_id"] == created["room_id"]

        guest.subscribe(PartyEvent.EPISODE_CHANGED.value, received.append)

        await host.send_message(
            PartyEvent.EPISODE_CHANGED.value,
            {
                "anime_reference": {"title": "Frieren", "episode_number": "3"},
                "playback": {
                    "url": "https://cdn.example/episode.m3u8?token=playable",
                    "title": "Frieren - Ep 3",
                    "episode_number": "3",
                    "headers": {
                        "Referer": "https://provider.example/",
                        "Cookie": "session=secret",
                        "Authorization": "Bearer secret",
                    },
                },
            },
        )

        assert await _wait_for(lambda: bool(received))
    finally:
        await host.disconnect()
        await guest.disconnect()
        await server.stop()

    message = received[0]
    assert message.type == PartyEvent.EPISODE_CHANGED.value
    playback = message.payload["playback"]
    assert playback["url"] == "https://cdn.example/episode.m3u8?token=playable"
    assert playback["headers"] == {"Referer": "https://provider.example/"}
    assert message.payload["stream_url"] == playback["url"]
    assert message.payload["episode_number"] == "3"
