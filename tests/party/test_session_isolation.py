"""Two local NyoAnime instances must keep separate Supabase identities."""
import sys

import pytest

import nyoanime.party.client as client_module
from nyoanime.app import NyoAnimeApp
from nyoanime.party.client import WTRClient
from nyoanime.party.session_storage import (
    DEFAULT_SESSION_FILENAME,
    SESSION_DIR_ENV,
    SESSION_PATH_ENV,
    FileSessionStorage,
    resolve_session_path,
)
from tests.party.fake_supabase import FakeSupabase

ROOM_ID = "11111111-1111-4111-8111-111111111111"
HOST_PARTICIPANT = "22222222-2222-4222-8222-222222222222"
GUEST_PARTICIPANT = "33333333-3333-4333-8333-333333333333"
HOST_USER = "44444444-4444-4444-8444-444444444444"
GUEST_USER = "55555555-5555-4555-8555-555555555555"


def test_session_path_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_PATH_ENV, raising=False)
    monkeypatch.delenv(SESSION_DIR_ENV, raising=False)

    assert resolve_session_path() == (
        tmp_path / "home" / ".nyoanime" / "database" / DEFAULT_SESSION_FILENAME
    )

    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path / "guest"))
    assert resolve_session_path() == tmp_path / "guest" / DEFAULT_SESSION_FILENAME

    monkeypatch.setenv(SESSION_PATH_ENV, str(tmp_path / "exact.json"))
    assert resolve_session_path() == tmp_path / "exact.json"

    # An explicit argument always wins, including over both environment vars.
    assert resolve_session_path(tmp_path / "explicit.json") == tmp_path / "explicit.json"


def test_cli_session_dir_and_path_are_applied_before_startup(monkeypatch, tmp_path):
    monkeypatch.delenv(SESSION_PATH_ENV, raising=False)
    monkeypatch.delenv(SESSION_DIR_ENV, raising=False)

    app = NyoAnimeApp.__new__(NyoAnimeApp)
    monkeypatch.setattr(
        sys,
        "argv",
        ["nyoanime", "--session-dir", str(tmp_path / "guest"), "--version"],
    )

    with pytest.raises(SystemExit):
        app.run()

    assert resolve_session_path() == tmp_path / "guest" / DEFAULT_SESSION_FILENAME
    assert not (tmp_path / "guest" / DEFAULT_SESSION_FILENAME).exists()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nyoanime",
            "--session-dir",
            str(tmp_path / "ignored"),
            "--session-path",
            str(tmp_path / "exact.json"),
            "--version",
        ],
    )
    with pytest.raises(SystemExit):
        app.run()
    assert resolve_session_path() == tmp_path / "exact.json"


@pytest.mark.asyncio
async def test_two_clients_use_distinct_storage_and_identities(
    monkeypatch, tmp_path
):
    primary = tmp_path / "primary" / DEFAULT_SESSION_FILENAME
    primary.parent.mkdir(parents=True)
    primary.write_text('{"sentinel":"keep-me"}', encoding="utf-8")
    guest_path = tmp_path / "guest" / DEFAULT_SESSION_FILENAME

    monkeypatch.delenv(SESSION_PATH_ENV, raising=False)
    monkeypatch.delenv(SESSION_DIR_ENV, raising=False)
    monkeypatch.setattr(
        client_module,
        "resolve_supabase_config",
        lambda *args, **kwargs: (
            "https://example.supabase.co",
            "sb_publishable_test_key",
        ),
    )
    monkeypatch.setattr(client_module, "use_supabase_transport", lambda: True)

    storage_paths = []
    fakes = {}

    async def fake_acreate_client(_url, _key, options):
        path = str(options.storage.path)
        storage_paths.append(path)
        user_id, participant_id = (
            (HOST_USER, HOST_PARTICIPANT)
            if path == str(primary)
            else (GUEST_USER, GUEST_PARTICIPANT)
        )
        fake = FakeSupabase(
            session=None,
            user_id=user_id,
            responses={
                "create-room": {
                    "success": True,
                    "room_id": ROOM_ID,
                    "room_code": "123456",
                    "participant_id": participant_id,
                    "role": "host",
                },
                "join-room": {
                    "success": True,
                    "room_id": ROOM_ID,
                    "room_code": "123456",
                    "participant_id": participant_id,
                    "role": "guest",
                },
            },
        )
        fakes[path] = fake
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)

    host = WTRClient("ws://localhost:8765", "Alice", session_path=primary)
    guest = WTRClient("ws://localhost:8765", "Bob", session_path=guest_path)

    await host.connect()
    await host.create_room()

    await guest.connect()
    await guest.join_room("123456")

    assert storage_paths == [str(primary), str(guest_path)]
    assert fakes[str(primary)] is not fakes[str(guest_path)]
    assert host._supabase_user_id == HOST_USER
    assert guest._supabase_user_id == GUEST_USER
    assert host._supabase_user_id != guest._supabase_user_id
    assert fakes[str(primary)].auth.sign_in_calls == 1
    assert fakes[str(guest_path)].auth.sign_in_calls == 1

    assert host.room_id == guest.room_id == ROOM_ID
    assert host.participant_id == HOST_PARTICIPANT
    assert guest.participant_id == GUEST_PARTICIPANT
    assert host.participant_id != guest.participant_id
    assert host.role == "host"
    assert guest.role == "guest"

    # The guest's sign-in and storage must never touch the primary profile.
    assert primary.read_text(encoding="utf-8") == '{"sentinel":"keep-me"}'
    assert FileSessionStorage(guest_path).path == guest_path

    await host.close()
    await guest.close()
