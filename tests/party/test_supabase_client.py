"""Mocked tests for WTRClient configuration, auth and session persistence."""
import json
import os

import pytest

import nyoanime.party.client as client_module
from nyoanime.party.client import PartyClientError, WTRClient
from nyoanime.party.session_storage import FileSessionStorage
from nyoanime.party.supabase_config import (
    DEFAULT_SUPABASE_PUBLISHABLE_KEY,
    DEFAULT_SUPABASE_URL,
    resolve_supabase_config,
    use_supabase_transport,
)
from tests.party.fake_supabase import FakeFunctionError, FakeSession, FakeSupabase

ROOM_ID = "11111111-1111-4111-8111-111111111111"
PARTICIPANT_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def supabase_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("NYOANIME_SUPABASE_SESSION_PATH", str(tmp_path / "session.json"))
    monkeypatch.setattr(
        client_module,
        "resolve_supabase_config",
        lambda *args, **kwargs: (
            "https://example.supabase.co",
            "sb_publishable_test_key",
        ),
    )
    monkeypatch.setattr(client_module, "use_supabase_transport", lambda: True)


@pytest.mark.asyncio
async def test_client_signs_in_anonymously_and_uses_supabase_transport(
    supabase_mode, monkeypatch
):
    fake = FakeSupabase(
        session=None,
        user_id=USER_ID,
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

    async def fake_acreate_client(*args, **kwargs):
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()

    assert client.transport_kind == "supabase"
    assert fake.auth.sign_in_calls == 1
    assert fake.realtime.token == "test-access-token"

    code = await client.create_room()
    assert code == "123456"
    assert client.participant_id == PARTICIPANT_ID
    assert client.role == "host"
    assert client.room_id == ROOM_ID
    assert client.channels_ready is True


@pytest.mark.asyncio
async def test_client_reuses_persisted_session(supabase_mode, monkeypatch):
    session = FakeSession(USER_ID, access_token="persisted-token")
    fake = FakeSupabase(
        session=session,
        user_id=USER_ID,
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

    async def fake_acreate_client(*args, **kwargs):
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()

    assert fake.auth.sign_in_calls == 0
    assert fake.realtime.token == "persisted-token"


@pytest.mark.asyncio
async def test_supabase_reconnect_does_not_require_a_relay_token(
    supabase_mode, monkeypatch
):
    fake = FakeSupabase(
        session=FakeSession(USER_ID),
        user_id=USER_ID,
        responses={
            "create-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            },
            "join-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            },
        },
    )

    async def fake_acreate_client(*args, **kwargs):
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()
    await client.create_room()

    await client.reconnect()

    assert client.room_code == "123456"
    assert client.participant_id == PARTICIPANT_ID
    assert any(name == "join-room" for name, _ in fake.functions.calls)


@pytest.mark.asyncio
async def test_missing_publishable_key_falls_back_to_websocket(monkeypatch):
    monkeypatch.setattr(client_module, "resolve_supabase_config", lambda *a, **k: None)
    monkeypatch.setattr(client_module, "use_supabase_transport", lambda: True)

    class FakeWS:
        def __init__(self, url):
            self.url = url
            self.on_disconnect = None
            self._connected = False
            self.handlers = []

        async def connect(self, *args):
            self._connected = True

        async def disconnect(self):
            self._connected = False

        def is_connected(self):
            return self._connected

        def subscribe(self, event, callback):
            self.handlers.append((event, callback))

    monkeypatch.setattr(client_module, "WebSocketRelayTransport", FakeWS)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()

    assert client.transport_kind == "websocket"


@pytest.mark.asyncio
async def test_malformed_publishable_key_fails_before_network(monkeypatch):
    from nyoanime.party.supabase_config import SupabaseConfigError

    monkeypatch.setattr(
        client_module,
        "resolve_supabase_config",
        lambda *args, **kwargs: (
            "https://example.supabase.co",
            "python main.py --setup-supabase",
        ),
    )
    monkeypatch.setattr(client_module, "use_supabase_transport", lambda: True)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("network client should not be created")

    monkeypatch.setattr("supabase.acreate_client", must_not_run)

    client = WTRClient("ws://localhost:8765", "Alice")
    with pytest.raises(SupabaseConfigError) as excinfo:
        await client.connect()

    assert excinfo.value.code == "invalid_supabase_config"
    assert "not a command" in str(excinfo.value)


@pytest.mark.asyncio
async def test_rejected_publishable_key_becomes_a_configuration_error(
    supabase_mode, monkeypatch
):
    from nyoanime.party.supabase_config import SupabaseConfigError

    async def rejecting_client(*args, **kwargs):
        raise ConnectionError("Failed to connect to Supabase: Invalid API key")

    monkeypatch.setattr("supabase.acreate_client", rejecting_client)

    client = WTRClient("ws://localhost:8765", "Alice")
    with pytest.raises(SupabaseConfigError) as excinfo:
        await client.connect()

    assert excinfo.value.code == "invalid_supabase_config"
    assert "python main.py --setup-supabase" in str(excinfo.value)


@pytest.mark.asyncio
async def test_client_recovers_from_stale_anonymous_session(
    supabase_mode, monkeypatch
):
    stale = FakeSession(USER_ID, access_token="expired-token", expires_at=1)
    fake = FakeSupabase(
        session=stale,
        user_id=USER_ID,
        responses={
            "create-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "654321",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            }
        },
    )
    fake.auth.fail_refresh = True
    _fake_factory(monkeypatch, fake)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()

    assert fake.auth.refresh_calls == 1
    assert fake.auth.sign_out_calls == 1
    assert fake.auth.sign_in_calls == 1
    assert fake.realtime.token == "test-access-token"

    assert await client.create_room() == "654321"


@pytest.mark.asyncio
async def test_file_session_storage_is_private_and_persistent(tmp_path):
    path = tmp_path / "session.json"
    storage = FileSessionStorage(path)

    await storage.set_item("sb-example-auth-token", '{"access_token":"abc"}')
    assert await storage.get_item("sb-example-auth-token") == '{"access_token":"abc"}'
    assert os.stat(path).st_mode & 0o777 == 0o600

    reloaded = FileSessionStorage(path)
    assert await reloaded.get_item("sb-example-auth-token") == '{"access_token":"abc"}'

    await reloaded.remove_item("sb-example-auth-token")
    assert await reloaded.get_item("sb-example-auth-token") is None

    path.write_text("not-json", encoding="utf-8")
    assert await reloaded.get_item("sb-example-auth-token") is None


def test_config_resolution_and_transport_switch(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    # Zero-config builds fall back to the bundled public project credentials.
    assert resolve_supabase_config() == (
        DEFAULT_SUPABASE_URL,
        DEFAULT_SUPABASE_PUBLISHABLE_KEY,
    )

    assert resolve_supabase_config("https://example.supabase.co", "pub") == (
        "https://example.supabase.co",
        "pub",
    )

    monkeypatch.setenv("NYOANIME_WATCH_TOGETHER_TRANSPORT", "websocket")
    assert use_supabase_transport() is False

    monkeypatch.delenv("NYOANIME_WATCH_TOGETHER_TRANSPORT")
    monkeypatch.setenv("USE_SUPABASE_REALM", "false")
    assert use_supabase_transport() is False


def test_config_never_accepts_service_role_style_secret_from_env(monkeypatch):
    # The resolver only reads the publishable key variable. A service-role key
    # left in the environment must not silently become the client key.
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-secret")
    resolved = resolve_supabase_config()
    assert resolved == (DEFAULT_SUPABASE_URL, DEFAULT_SUPABASE_PUBLISHABLE_KEY)
    assert "service-role-secret" not in resolved


def _fake_factory(monkeypatch, fake):
    async def fake_acreate_client(*args, **kwargs):
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)


@pytest.mark.asyncio
async def test_client_retains_partial_room_state_when_channels_fail(
    supabase_mode, monkeypatch
):
    fake = FakeSupabase(
        session=FakeSession(USER_ID),
        user_id=USER_ID,
        responses={
            "create-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            }
        },
        fail_topics={f"watch-room:{ROOM_ID}": "private channel was rejected"},
    )
    _fake_factory(monkeypatch, fake)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()

    with pytest.raises(PartyClientError) as excinfo:
        await client.create_room()

    assert excinfo.value.code == "channel_subscribe_failed"
    assert client.room_code == "123456"
    assert client.room_id == ROOM_ID
    assert client.participant_id == PARTICIPANT_ID
    assert client.role == "host"
    assert client.channels_ready is False
    assert client.connection_status["room_id"] == ROOM_ID
    assert "private channel" in (client.channel_error or "")


@pytest.mark.asyncio
async def test_client_leave_failure_is_retryable(supabase_mode, monkeypatch):
    fake = FakeSupabase(
        session=FakeSession(USER_ID),
        user_id=USER_ID,
        responses={
            "join-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "guest",
            },
            "leave-room": FakeFunctionError("network_error", "temporary outage"),
        },
    )
    _fake_factory(monkeypatch, fake)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()
    await client.join_room("123456")

    with pytest.raises(PartyClientError) as excinfo:
        await client.leave_room()

    assert excinfo.value.code == "leave_failed"
    assert client.room_code == "123456"
    assert client.room_id == ROOM_ID

    fake.functions.responses["leave-room"] = {"success": True, "room_id": ROOM_ID}
    await client.leave_room()
    assert client.room_code is None
    assert client.room_id is None
    assert client.participant_id is None


@pytest.mark.asyncio
async def test_client_retry_connection_resubscribes_channels_without_rejoin(
    supabase_mode, monkeypatch
):
    fake = FakeSupabase(
        session=FakeSession(USER_ID),
        user_id=USER_ID,
        responses={
            "create-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            }
        },
        fail_topics={f"watch-room:{ROOM_ID}": "private channel was rejected"},
    )
    _fake_factory(monkeypatch, fake)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()
    with pytest.raises(PartyClientError):
        await client.create_room()

    fake.fail_topics.clear()
    await client.retry_connection()

    assert client.channels_ready is True
    assert client.room_code == "123456"
    assert all(name != "join-room" for name, _ in fake.functions.calls)


@pytest.mark.asyncio
async def test_create_room_releases_stale_hosted_rooms_and_retries(
    supabase_mode, monkeypatch
):
    attempts = {"n": 0}
    stale_room = "99999999-9999-4999-8999-999999999999"

    def create_response(_options):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeFunctionError("too_many_rooms", "too many rooms")
        return {
            "success": True,
            "room_id": ROOM_ID,
            "room_code": "123456",
            "participant_id": PARTICIPANT_ID,
            "role": "host",
        }

    fake = FakeSupabase(
        session=FakeSession(USER_ID),
        user_id=USER_ID,
        responses={
            "create-room": create_response,
            "leave-room": {"success": True},
        },
        rows={
            "watch_room_members": [
                {"room_id": stale_room, "user_id": USER_ID, "role": "host"}
            ],
            "watch_rooms": [
                {
                    "id": stale_room,
                    "host_user_id": USER_ID,
                    "status": "active",
                    "created_at": "2026-01-01T00:00:00Z",
                    "expires_at": "2026-01-01T02:00:00Z",
                }
            ],
        },
    )

    async def fake_acreate_client(*args, **kwargs):
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()
    code = await client.create_room()

    assert code == "123456"
    assert attempts["n"] == 2
    assert [name for name, _ in fake.functions.calls] == [
        "create-room",
        "leave-room",
        "create-room",
    ]
    assert client.room_id == ROOM_ID


@pytest.mark.asyncio
async def test_exit_cleanup_releases_a_hosted_room_and_is_idempotent(
    supabase_mode, monkeypatch
):
    from nyoanime.party import cleanup, lifecycle

    fake = FakeSupabase(
        session=FakeSession(USER_ID),
        user_id=USER_ID,
        responses={
            "create-room": {
                "success": True,
                "room_id": ROOM_ID,
                "room_code": "123456",
                "participant_id": PARTICIPANT_ID,
                "role": "host",
            },
            "leave-room": {"success": True},
        },
    )

    async def fake_acreate_client(*args, **kwargs):
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)

    calls = []

    def fake_blocking_cleanup(timeout=8.0, session_path=None):
        calls.append(timeout)
        return 1

    monkeypatch.setattr(
        cleanup, "leave_active_hosted_rooms_blocking", fake_blocking_cleanup
    )

    client = WTRClient("ws://localhost:8765", "Alice")
    await client.connect()
    await client.create_room()
    assert client in lifecycle.active_hosts()
    assert client._supabase_hosted is True

    client._exit_cleanup()
    assert len(calls) == 1

    await client.leave_room()
    assert client not in lifecycle.active_hosts()
    assert client._supabase_hosted is False

    client._exit_cleanup()
    assert len(calls) == 1
