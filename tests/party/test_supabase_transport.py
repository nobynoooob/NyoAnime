"""Mocked tests for the Supabase transport contract.

No network is used here. Every assertion runs against fake_supabase.py, which
implements the small subset of the async SDK used by the app. Remote checks are
in test_supabase_remote.py and only run when explicitly configured.
"""
import pytest

from nyoanime.party.protocol import PartyEvent
from nyoanime.party.transport import RealtimeTransportError, SupabaseTransport
from tests.party.fake_supabase import (
    FakeFunctionError,
    FakeSession,
    FakeSupabase,
)

ROOM_ID = "11111111-1111-4111-8111-111111111111"
PARTICIPANT_ID = "22222222-2222-4222-8222-222222222222"
GUEST_ID = "33333333-3333-4333-8333-333333333333"
HOST_USER = "44444444-4444-4444-8444-444444444444"
GUEST_USER = "55555555-5555-4555-8555-555555555555"


def make_transport(responses, user_id=HOST_USER, fail_topics=None, rpc_responses=None):
    fake = FakeSupabase(
        session=FakeSession(user_id),
        responses=responses,
        fail_topics=fail_topics,
        rpc_responses=rpc_responses,
    )
    return fake, SupabaseTransport(fake, user_id)


@pytest.mark.asyncio
async def test_create_room_subscribes_one_private_channel_and_tracks_presence():
    fake, transport = make_transport({
        "create-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": PARTICIPANT_ID,
            "role": "host",
            "expires_at": "2026-01-01T00:00:00Z",
        }
    })
    await transport.connect("", "")

    result = await transport.create_room("Alice")

    assert result["room_code"] == "123456"
    assert result["participant_id"] == PARTICIPANT_ID
    assert result["role"] == "host"
    assert fake.realtime.token == "test-access-token"

    assert len(fake.channels) == 1
    room = fake.channels[0]
    assert room.topic == f"watch-room:{ROOM_ID}"
    assert room.params["config"]["private"] is True
    assert room.params["config"]["presence"]["key"] == HOST_USER
    assert room.params["config"]["presence"]["enabled"] is True
    assert room.tracked == {
        "nickname": "Alice",
        "role": "host",
        "user_id": HOST_USER,
    }
    assert transport.channel_status == {"room": True}
    assert transport.channel_states == {"room": "SUBSCRIBED"}

    name, options = fake.functions.calls[0]
    assert name == "create-room"
    assert options["body"] == {"nickname": "Alice", "language_mode": "ar_sub"}
    assert options["headers"]["apikey"] == "test-publishable-key"
    assert options["headers"]["Authorization"] == "Bearer test-access-token"
    assert "user_id" not in options["body"]
    assert "host_user_id" not in options["body"]


@pytest.mark.asyncio
async def test_host_playback_and_chat_share_one_channel():
    fake, transport = make_transport({
        "create-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": PARTICIPANT_ID,
            "role": "host",
        }
    })
    await transport.connect("", "")
    await transport.create_room("Alice")
    room = fake.channels[0]

    await transport.send_message(PartyEvent.CHAT.value, {"text": "hello"})
    await transport.send_message(
        PartyEvent.SYNC_PLAYBACK.value,
        {"position": 12.5, "paused": False, "stream_url": "https://cdn/secret.m3u8"},
    )

    assert [event for event, _ in room.sent] == ["chat", "sync_playback"]
    assert room.sent[0][1]["payload"] == {"text": "hello"}
    assert room.sent[0][1]["sender"] == PARTICIPANT_ID
    assert room.sent[1][1]["payload"]["position"] == 12.5
    assert "stream_url" not in room.sent[1][1]["payload"]


@pytest.mark.asyncio
async def test_guest_cannot_send_host_events_on_the_shared_channel():
    fake, transport = make_transport({
        "join-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": GUEST_ID,
            "role": "guest",
        }
    }, user_id=GUEST_USER)
    await transport.connect("", "")
    await transport.join_room("123456", "Bob")
    room = fake.channels[0]

    with pytest.raises(RealtimeTransportError) as excinfo:
        await transport.send_message(PartyEvent.PLAY.value, {})

    assert excinfo.value.code == "forbidden"
    assert room.sent == []

    await transport.send_message(PartyEvent.CHAT.value, {"text": "hi"})
    assert room.sent[0][0] == "chat"


@pytest.mark.asyncio
async def test_join_reuses_membership_and_leave_uses_room_uuid():
    fake, transport = make_transport({
        "join-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": GUEST_ID,
            "nickname": "Bob",
            "role": "guest",
        },
        "leave-room": {"success": True, "room_id": ROOM_ID},
    }, user_id=GUEST_USER)
    await transport.connect("", "")

    first = await transport.join_room("123456", "Bob")
    second = await transport.join_room("123456", "Bobby")

    assert first["participant_id"] == GUEST_ID
    assert second["participant_id"] == GUEST_ID
    assert fake.functions.calls[0][1]["body"] == {
        "room_code": "123456",
        "nickname": "Bob",
    }
    assert "user_id" not in fake.functions.calls[0][1]["body"]

    left = await transport.leave_room("123456", GUEST_ID)
    assert left is True
    name, options = fake.functions.calls[-1]
    assert name == "leave-room"
    assert options["body"] == {"room_id": ROOM_ID}
    assert options["responseType"] == "json"
    assert options["headers"]["apikey"] == "test-publishable-key"
    assert options["headers"]["Authorization"] == "Bearer test-access-token"
    assert transport.is_connected() is True
    assert transport._room_id is None
    assert len(fake.removed) == 2  # one subscription per join, one removal each


@pytest.mark.asyncio
async def test_function_errors_are_mapped_to_transport_codes():
    fake, transport = make_transport({
        "join-room": FakeFunctionError("room_full", "room_full"),
    }, user_id=GUEST_USER)
    await transport.connect("", "")

    with pytest.raises(RealtimeTransportError) as excinfo:
        await transport.join_room("123456", "Bob")

    assert excinfo.value.code == "room_full"


@pytest.mark.asyncio
async def test_expired_jwt_reauthenticates_and_retries_once():
    attempts = {"count": 0}

    def create_room_response(_options):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return FakeFunctionError("not_authenticated", "jwt expired")
        return {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": PARTICIPANT_ID,
            "role": "host",
        }

    fake, transport = make_transport({"create-room": create_room_response})
    await transport.connect("", "")
    result = await transport.create_room("Alice")

    assert result["room_code"] == "123456"
    assert attempts["count"] == 2
    assert fake.auth.sign_out_calls == 1
    assert fake.auth.sign_in_calls == 1
    assert fake.functions.calls[1][1]["headers"]["Authorization"] == (
        "Bearer test-access-token"
    )


@pytest.mark.asyncio
async def test_active_room_refresh_never_switches_anonymous_identity():
    create_room = {
        "success": True,
        "room_id": ROOM_ID,
        "room_code": "123456",
        "participant_id": PARTICIPANT_ID,
        "role": "host",
    }
    fake, transport = make_transport({
        "create-room": create_room,
        "leave-room": FakeFunctionError("not_authenticated", "jwt expired"),
    })
    await transport.connect("", "")
    await transport.create_room("Alice")

    # Force a dead refresh token; a fresh anonymous sign-in here would mint a
    # different user id and orphan the host's room membership.
    fake.auth.session.expires_at = 1
    fake.auth.fail_refresh = True

    with pytest.raises(RealtimeTransportError) as excinfo:
        await transport.leave_room_by_id_checked(ROOM_ID)

    assert excinfo.value.code == "not_authenticated"
    assert fake.auth.sign_in_calls == 0
    assert fake.auth.sign_out_calls == 0


@pytest.mark.asyncio
async def test_invalid_api_key_is_reported_as_configuration_not_retried():
    fake, transport = make_transport({
        "create-room": FakeFunctionError("function_error", "Invalid API key"),
    })
    await transport.connect("", "")

    with pytest.raises(RealtimeTransportError) as excinfo:
        await transport.create_room("Alice")

    assert excinfo.value.code == "invalid_supabase_config"
    assert len(fake.functions.calls) == 1
    assert fake.auth.sign_in_calls == 0


@pytest.mark.asyncio
async def test_broadcast_dispatch_sanitizes_untrusted_payloads():
    _, transport = make_transport({})
    await transport.connect("", "")
    transport._room_id = ROOM_ID
    transport._role = "guest"
    received = []
    transport.subscribe(PartyEvent.SYNC_PLAYBACK.value, received.append)

    transport._dispatch({
        "event": "sync_playback",
        "payload": {
            "type": "sync_playback",
            "room_id": ROOM_ID,
            "sender": "someone",
            "payload": {
                "position": 3.0,
                "authorization": "Bearer leaked",
                "nested": {"cookie": "session=abc", "title": "ok"},
            },
        },
    })

    assert len(received) == 1
    message = received[0]
    assert message.type == "sync_playback"
    assert message.payload["position"] == 3.0
    assert "authorization" not in message.payload
    assert "cookie" not in message.payload["nested"]


@pytest.mark.asyncio
async def test_chat_and_host_events_arrive_on_the_same_room_channel():
    fake, transport = make_transport({
        "create-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": PARTICIPANT_ID,
            "role": "host",
        }
    })
    await transport.connect("", "")
    await transport.create_room("Alice")
    room = fake.channels[0]

    received = []
    transport.subscribe(PartyEvent.SYNC_PLAYBACK.value, received.append)
    transport.subscribe(PartyEvent.CHAT.value, received.append)

    room.receive(PartyEvent.SYNC_PLAYBACK.value, {
        "type": PartyEvent.SYNC_PLAYBACK.value,
        "room_id": ROOM_ID,
        "sender": "host",
        "payload": {"position": 12.0, "paused": False},
    })
    room.receive(PartyEvent.CHAT.value, {
        "type": PartyEvent.CHAT.value,
        "room_id": ROOM_ID,
        "sender": "guest",
        "payload": {"text": "hi"},
    })

    assert [message.type for message in received] == [
        PartyEvent.SYNC_PLAYBACK.value,
        PartyEvent.CHAT.value,
    ]
    assert received[0].payload["position"] == 12.0


def test_sanitizer_drops_secrets_and_redacts_urls():
    clean = SupabaseTransport._sanitize_payload({
        "position": 1.0,
        "stream_url": "https://cdn.example/stream.m3u8",
        "headers": {"Authorization": "Bearer abc"},
        "cookie": "session=abc",
        "mpv_socket": "/tmp/mpv.sock",
        "nested": {
            "title": "Watch https://cdn.example/video.m3u8",
            "bearer": "Bearer top-secret-token",
            "ipc": "/run/user/1000/mpv.sock",
            "anime_reference": {"id": "12", "title": "A title"},
        },
    })

    assert "stream_url" not in clean
    assert "headers" not in clean
    assert "cookie" not in clean
    assert "mpv_socket" not in clean
    assert "https://" not in clean["nested"]["title"]
    assert "top-secret-token" not in clean["nested"]["bearer"]
    assert "mpv.sock" not in clean["nested"]["ipc"]
    assert clean["nested"]["anime_reference"] == {"id": "12", "title": "A title"}


def test_episode_sanitizer_keeps_stream_url_and_safe_headers_only():
    clean = SupabaseTransport._sanitize_payload(
        {
            "anime_reference": {"title": "Frieren", "episode_number": "3"},
            "playback": {
                "url": "https://cdn.example/episode.m3u8?token=playable",
                "title": "Frieren - Ep 3",
                "episode_number": "3",
                "headers": {
                    "Referer": "https://provider.example/",
                    "User-Agent": "NyoAnime",
                    "Cookie": "session=secret",
                    "Authorization": "Bearer secret",
                },
                "start_time": 0,
            },
        },
        allow_playback=True,
    )

    playback = clean["playback"]
    assert playback["url"] == "https://cdn.example/episode.m3u8?token=playable"
    assert playback["headers"] == {
        "Referer": "https://provider.example/",
        "User-Agent": "NyoAnime",
    }
    assert "Cookie" not in playback["headers"]
    assert "Authorization" not in playback["headers"]


@pytest.mark.asyncio
async def test_presence_sync_is_forwarded_to_subscribers():
    _, transport = make_transport({})
    await transport.connect("", "")
    transport._room_id = ROOM_ID
    received = []
    transport.subscribe("presence", received.append)

    from tests.party.fake_supabase import FakeChannel
    channel = FakeChannel(f"watch-room:{ROOM_ID}", {"config": {"private": True}})
    channel.presence = {HOST_USER: [{"nickname": "Alice", "role": "host"}]}
    transport.channel = channel

    transport._handle_presence()

    assert len(received) == 1
    assert received[0].type == "presence"
    assert received[0].payload["participants"][HOST_USER][0]["nickname"] == "Alice"


@pytest.mark.asyncio
async def test_get_room_state_uses_rls_scoped_queries():
    fake, transport = make_transport({
        "create-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": PARTICIPANT_ID,
            "role": "host",
        }
    })
    fake.rows = {
        "watch_room_members": [{
            "id": PARTICIPANT_ID,
            "room_id": ROOM_ID,
            "user_id": HOST_USER,
            "nickname": "Alice",
            "role": "host",
        }],
        "watch_rooms": [{
            "id": ROOM_ID,
            "host_user_id": HOST_USER,
            "status": "active",
            "maximum_members": 8,
        }],
    }
    await transport.connect("", "")
    await transport.create_room("Alice")

    state = await transport.get_room_state("123456")

    assert state["room_id"] == ROOM_ID
    assert state["participants"][0]["nickname"] == "Alice"
    assert state["you"]["role"] == "host"
    assert "room_code_hash" not in state["room_info"]


@pytest.mark.asyncio
async def test_get_room_state_adopts_server_side_host_promotion():
    fake, transport = make_transport({
        "join-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": GUEST_ID,
            "nickname": "Bob",
            "role": "guest",
        }
    }, user_id=GUEST_USER)
    fake.rows = {
        "watch_room_members": [{
            "id": GUEST_ID,
            "room_id": ROOM_ID,
            "user_id": GUEST_USER,
            "nickname": "Bob",
            "role": "host",
        }],
        "watch_rooms": [{
            "id": ROOM_ID,
            "host_user_id": GUEST_USER,
            "status": "active",
            "maximum_members": 8,
        }],
    }
    await transport.connect("", "")
    await transport.join_room("123456", "Bob")
    assert transport.role == "guest"

    state = await transport.get_room_state("123456")

    assert transport.role == "host"
    assert state["you"]["role"] == "host"
    await transport.send_message(PartyEvent.PLAY.value, {"position": 0.0})


@pytest.mark.asyncio
async def test_create_room_keeps_identity_when_channel_subscription_fails():
    fake, transport = make_transport({
        "create-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": PARTICIPANT_ID,
            "role": "host",
        },
    }, fail_topics={f"watch-room:{ROOM_ID}": "private channel was rejected"})
    await transport.connect("", "")

    with pytest.raises(RealtimeTransportError) as excinfo:
        await transport.create_room("Alice")

    assert excinfo.value.code == "channel_subscribe_failed"
    assert transport.room_id == ROOM_ID
    assert transport.room_code == "123456"
    assert transport.participant_id == PARTICIPANT_ID
    assert transport.role == "host"
    assert transport.channels_ready is False
    assert "room" in (transport.channel_error or "")


@pytest.mark.asyncio
async def test_failed_leave_preserves_room_for_retry():
    fake, transport = make_transport({
        "join-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": GUEST_ID,
            "role": "guest",
        },
        "leave-room": FakeFunctionError("network_error", "temporary outage"),
    }, user_id=GUEST_USER)
    await transport.connect("", "")
    await transport.join_room("123456", "Bob")

    assert await transport.leave_room("123456", GUEST_ID) is False
    assert transport.room_id == ROOM_ID
    assert transport.room_code == "123456"
    assert transport.channels_ready is True

    fake.functions.responses["leave-room"] = {"success": True, "room_id": ROOM_ID}
    assert await transport.leave_room("123456", GUEST_ID) is True
    assert transport.room_id is None
    assert transport.room_code is None
    assert transport.channels_ready is False


@pytest.mark.asyncio
async def test_not_a_member_leave_clears_local_state():
    _, transport = make_transport({
        "join-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": GUEST_ID,
            "role": "guest",
        },
        "leave-room": FakeFunctionError("not_a_member", "already gone"),
    }, user_id=GUEST_USER)
    await transport.connect("", "")
    await transport.join_room("123456", "Bob")

    assert await transport.leave_room("123456", GUEST_ID) is True
    assert transport.room_id is None
    assert transport.room_code is None
    assert transport.channels_ready is False


@pytest.mark.asyncio
async def test_late_channel_failure_is_captured_and_redacted():
    fake, transport = make_transport({
        "create-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": PARTICIPANT_ID,
            "role": "host",
        }
    })
    await transport.connect("", "")
    await transport.create_room("Alice")

    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"
    room = fake.channels[0]
    room.drop(f"token has expired (Bearer {token}, apikey=sb_secret_abc123)")

    assert transport.channel_states["room"] == "CHANNEL_ERROR"
    error = transport.channel_error or ""
    assert "room:" in error
    assert "CHANNEL_ERROR" in error
    assert "token has expired" in error
    assert token not in error
    assert "sb_secret_abc123" not in error

    if transport._auth_sync_task is not None:
        await transport._auth_sync_task


@pytest.mark.asyncio
async def test_retry_channels_pushes_the_newest_auth_token():
    fake, transport = make_transport({
        "join-room": {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": GUEST_ID,
            "role": "guest",
        }
    }, user_id=GUEST_USER)
    await transport.connect("", "")
    await transport.join_room("123456", "Bob")
    assert fake.realtime.token == "test-access-token"

    # supabase-auth rotates tokens in the background; the socket must follow.
    fake.auth.session.access_token = "rotated-access-token"
    assert await transport.retry_channels() is True

    assert fake.realtime.token == "rotated-access-token"
    assert fake.realtime.tokens[-1] == "rotated-access-token"
    assert transport.channels_ready is True
    assert transport.channel_states == {"room": "SUBSCRIBED"}


def test_sanitize_diagnostic_redacts_credentials_and_controls():
    from nyoanime.party.protocol import sanitize_diagnostic

    raw = (
        "wss://user:pass@example.supabase.co/realtime"
        "?apikey=sb_secret_abcdefghi&x=1 "
        "Authorization: Bearer abc.def-ghi "
        "refresh_token=abc123&access_token=xyz "
        + "A" * 80
        + "\nnext\tline\x01"
    )
    clean = sanitize_diagnostic(raw)

    assert "user:pass" not in clean
    assert "sb_secret_abcdefghi" not in clean
    assert "abc.def-ghi" not in clean
    assert "abc123" not in clean
    assert "A" * 80 not in clean
    assert "[url]@" in clean
    assert "[redacted]" in clean
    assert "\n" not in clean and "\x01" not in clean


class _BusChannel:
    """Delivery-only fake that fans broadcasts out to every joined channel."""

    def __init__(self, bus, topic, params, fail_message=None):
        from tests.party.fake_supabase import FakeChannel

        self._inner = FakeChannel(topic, params, fail_message=fail_message)
        self.bus = bus
        self.topic = topic
        self.params = params

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def on_broadcast(self, event, callback):
        self._inner.on_broadcast(event, callback)
        return self

    def on_presence_sync(self, callback):
        self._inner.on_presence_sync(callback)
        return self

    async def subscribe(self, callback=None):
        return await self._inner.subscribe(callback)

    async def send_broadcast(self, event, data):
        await self._inner.send_broadcast(event, data)
        for channel in self.bus.channels.get(self.topic, []):
            if channel is self:
                continue
            channel._inner.receive(event, data)

    async def track(self, data):
        return await self._inner.track(data)


class _Bus:
    def __init__(self):
        self.channels = {}

    def channel(self, topic, params):
        channel = _BusChannel(self, topic, params)
        self.channels.setdefault(topic, []).append(channel)
        return channel


@pytest.mark.asyncio
async def test_episode_changed_crosses_the_unified_supabase_channel(monkeypatch):
    bus = _Bus()
    host_fake = FakeSupabase(
        session=FakeSession(HOST_USER),
        responses={
            "create-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            }
        },
    )
    guest_fake = FakeSupabase(
        session=FakeSession(GUEST_USER),
        responses={
            "join-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": GUEST_ID,
                "role": "guest",
                "nickname": "Bob",
            }
        },
    )
    monkeypatch.setattr(host_fake, "channel", bus.channel)
    monkeypatch.setattr(guest_fake, "channel", bus.channel)

    host = SupabaseTransport(host_fake, HOST_USER)
    guest = SupabaseTransport(guest_fake, GUEST_USER)
    await host.connect("", "")
    await guest.connect("", "")
    await host.create_room("Alice")
    await guest.join_room("123456", "Bob")

    assert len(bus.channels[f"watch-room:{ROOM_ID}"]) == 2

    received = []
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
                    "User-Agent": "NyoAnime",
                    "Cookie": "session=secret",
                    "Authorization": "Bearer secret",
                },
            },
        },
    )

    assert len(received) == 1
    message = received[0]
    assert message.type == PartyEvent.EPISODE_CHANGED.value
    playback = message.payload["playback"]
    assert playback["url"] == "https://cdn.example/episode.m3u8?token=playable"
    assert playback["headers"] == {
        "Referer": "https://provider.example/",
        "User-Agent": "NyoAnime",
    }


@pytest.mark.asyncio
async def test_episode_changed_persists_playback_before_broadcasting():
    fake, transport = make_transport(
        {
            "create-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            }
        },
        rpc_responses={"set_watch_room_playback": [{"playback_seq": 1}]},
    )
    await transport.connect("", "")
    await transport.create_room("Alice")

    await transport.send_message(
        PartyEvent.EPISODE_CHANGED.value,
        {
            "anime_reference": {"title": "Frieren", "episode_number": "3"},
            "playback": {
                "url": "https://cdn.example/episode.m3u8",
                "episode_number": "3",
                "headers": {"Referer": "https://provider.example/"},
            },
        },
    )

    name, args = fake.rpc_calls[0]
    assert name == "set_watch_room_playback"
    assert args["p_room_id"] == ROOM_ID
    assert args["p_playback"]["url"] == "https://cdn.example/episode.m3u8"
    assert args["p_reference"]["episode_number"] == "3"
    assert fake.channels[0].sent[0][0] == PartyEvent.EPISODE_CHANGED.value


@pytest.mark.asyncio
async def test_rejected_broadcast_is_survivable_when_playback_was_persisted():
    fake, transport = make_transport(
        {
            "create-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            }
        },
        rpc_responses={"set_watch_room_playback": [{"playback_seq": 1}]},
    )
    await transport.connect("", "")
    await transport.create_room("Alice")

    async def reject(_event, _data):
        raise RuntimeError("unauthorized")

    fake.channels[0].send_broadcast = reject

    await transport.send_message(
        PartyEvent.EPISODE_CHANGED.value,
        {"playback": {"url": "https://cdn.example/episode.m3u8"}},
    )

    assert fake.rpc_calls[0][0] == "set_watch_room_playback"


@pytest.mark.asyncio
async def test_get_playback_state_reads_the_persisted_episode():
    fake, transport = make_transport({})
    await transport.connect("", "")
    transport._room_id = ROOM_ID
    fake.rows["watch_rooms"] = [
        {
            "id": ROOM_ID,
            "playback": {
                "url": "https://cdn.example/episode.m3u8",
                "title": "Frieren - Ep 3",
                "episode_number": "3",
            },
            "playback_reference": {"title": "Frieren", "episode_number": "3"},
            "playback_sender": HOST_USER,
            "playback_seq": 4,
            "playback_updated_at": "2026-11-05T00:00:00Z",
        }
    ]

    state = await transport.get_playback_state()

    assert state["playback"]["url"] == "https://cdn.example/episode.m3u8"
    assert state["anime_reference"]["title"] == "Frieren"
    assert state["seq"] == 4


@pytest.mark.asyncio
async def test_get_playback_state_is_none_for_an_empty_room():
    _, transport = make_transport({})
    await transport.connect("", "")
    transport._room_id = ROOM_ID

    assert await transport.get_playback_state() is None
