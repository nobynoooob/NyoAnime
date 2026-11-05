"""Real Supabase integration tests (skipped unless explicitly configured).

These tests talk to a *deployed* Watch Together backend. They are the only
tests in this repository that prove the SQL policies and Edge Functions work
together. They are skipped by default because no deployment has been approved
for the analytics project.

Required environment variables::

    NYOANIME_SUPABASE_TEST_URL
    NYOANIME_SUPABASE_TEST_KEY       # publishable/anon key only

Optional, for the expiry test::

    NYOANIME_SUPABASE_TEST_SERVICE_KEY

Never point these at production data. Use a dedicated test project.
"""
import asyncio
import json
import os
import re

import httpx
import pytest

pytest.importorskip("supabase")

from supabase import acreate_client  # noqa: E402
from supabase.lib.client_options import AsyncClientOptions  # noqa: E402
from supabase_auth import AsyncMemoryStorage  # noqa: E402
from supabase_functions.errors import FunctionsHttpError  # noqa: E402

URL = os.environ.get("NYOANIME_SUPABASE_TEST_URL")
KEY = os.environ.get("NYOANIME_SUPABASE_TEST_KEY")
SERVICE_KEY = os.environ.get("NYOANIME_SUPABASE_TEST_SERVICE_KEY")

pytestmark = [
    pytest.mark.remote,
    pytest.mark.skipif(
        not URL or not KEY,
        reason="remote Supabase test credentials are not configured",
    ),
]


class RemoteError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


async def new_user():
    client = await acreate_client(
        URL,
        KEY,
        AsyncClientOptions(
            storage=AsyncMemoryStorage(),
            auto_refresh_token=False,
            persist_session=False,
        ),
    )
    response = await client.auth.sign_in_anonymously()
    assert response.user is not None
    return client, response.user.id


async def call(client, name, body):
    try:
        return await client.functions.invoke(
            name, {"body": body, "responseType": "json"}
        )
    except FunctionsHttpError as exc:
        raw = exc.message
        code = "unknown"
        message = str(raw)
        if isinstance(raw, dict):
            code = str(raw.get("code", code))
            message = str(raw.get("message", message))
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                error = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
                code = str(error.get("code", code))
                message = str(error.get("message", message))
        raise RemoteError(exc.status, code, message) from exc


async def leave(client, room_id):
    try:
        await call(client, "leave-room", {"room_id": room_id})
    except Exception:
        pass


@pytest.mark.asyncio
async def test_create_join_duplicate_and_full_room():
    host, _ = await new_user()
    guest1, _ = await new_user()
    guest2, _ = await new_user()
    room = None

    try:
        room = await call(host, "create-room", {
            "nickname": "Host",
            "maximum_members": 2,
        })
        assert re.fullmatch(r"[0-9]{6}", room["room_code"])
        assert room["role"] == "host"

        first = await call(guest1, "join-room", {
            "room_code": room["room_code"],
            "nickname": "Guest One",
        })
        duplicate = await call(guest1, "join-room", {
            "room_code": room["room_code"],
            "nickname": "Guest One",
        })
        assert first["participant_id"] == duplicate["participant_id"]

        with pytest.raises(RemoteError) as excinfo:
            await call(guest2, "join-room", {
                "room_code": room["room_code"],
                "nickname": "Guest Two",
            })
        assert excinfo.value.code == "room_full"
        assert excinfo.value.status == 409
    finally:
        if room:
            await leave(guest1, room["room_id"])
            await leave(host, room["room_id"])


@pytest.mark.asyncio
async def test_invalid_jwt_is_rejected():
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            f"{URL.rstrip('/')}/functions/v1/create-room",
            headers={
                "apikey": KEY,
                "Authorization": "Bearer definitely-not-a-jwt",
                "Content-Type": "application/json",
            },
            json={"nickname": "Mallory"},
        )

    assert response.status_code == 401
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        assert body["success"] is False
        assert body["error"]["code"] in {"invalid_token", "missing_authorization"}


@pytest.mark.asyncio
async def test_non_member_cannot_leave_room():
    host, _ = await new_user()
    outsider, _ = await new_user()
    room = None

    try:
        room = await call(host, "create-room", {"nickname": "Host"})
        with pytest.raises(RemoteError) as excinfo:
            await call(outsider, "leave-room", {"room_id": room["room_id"]})
        assert excinfo.value.status == 403
        assert excinfo.value.code == "not_a_member"
    finally:
        if room:
            await leave(host, room["room_id"])


@pytest.mark.asyncio
async def test_concurrent_joins_never_exceed_capacity():
    host, _ = await new_user()
    room = await call(host, "create-room", {
        "nickname": "Host",
        "maximum_members": 8,
    })
    guests = [await new_user() for _ in range(11)]

    async def attempt(client, index):
        try:
            result = await call(client, "join-room", {
                "room_code": room["room_code"],
                "nickname": f"Guest {index}",
            })
            return client, result
        except RemoteError as exc:
            return client, exc

    try:
        results = await asyncio.gather(*(
            attempt(client, index) for index, (client, _) in enumerate(guests)
        ))
        joined_ids = []
        for _, result in results:
            if isinstance(result, RemoteError):
                assert result.code == "room_full"
            else:
                joined_ids.append(result["participant_id"])

        assert len(joined_ids) == 7  # host + seven guests = capacity eight
        assert len(set(joined_ids)) == len(joined_ids)
    finally:
        for client, _ in guests:
            await leave(client, room["room_id"])
        await leave(host, room["room_id"])


@pytest.mark.asyncio
async def test_host_departure_promotes_the_next_member():
    host, host_id = await new_user()
    guest1, guest1_id = await new_user()
    guest2, _ = await new_user()
    room = await call(host, "create-room", {"nickname": "Host"})

    try:
        await call(guest1, "join-room", {
            "room_code": room["room_code"],
            "nickname": "First Guest",
        })
        await call(guest2, "join-room", {
            "room_code": room["room_code"],
            "nickname": "Second Guest",
        })
        await leave(host, room["room_id"])

        promoted = await call(guest1, "join-room", {
            "room_code": room["room_code"],
            "nickname": "First Guest",
        })
        other = await call(guest2, "join-room", {
            "room_code": room["room_code"],
            "nickname": "Second Guest",
        })
        assert promoted["role"] == "host"
        assert other["role"] == "guest"
        assert promoted["participant_id"] != other["participant_id"]
    finally:
        await leave(guest1, room["room_id"])
        await leave(guest2, room["room_id"])


@pytest.mark.asyncio
@pytest.mark.skipif(not SERVICE_KEY, reason="service key required to force expiry")
async def test_expired_room_is_rejected():
    host, _ = await new_user()
    guest, _ = await new_user()
    room = await call(host, "create-room", {"nickname": "Host"})

    try:
        admin = await acreate_client(
            URL,
            SERVICE_KEY,
            AsyncClientOptions(
                storage=AsyncMemoryStorage(),
                auto_refresh_token=False,
                persist_session=False,
            ),
        )
        await (
            admin.table("watch_rooms")
            .update({"expires_at": "2000-01-01T00:00:00Z"})
            .eq("id", room["room_id"])
            .execute()
        )

        with pytest.raises(RemoteError) as excinfo:
            await call(guest, "join-room", {
                "room_code": room["room_code"],
                "nickname": "Late Guest",
            })
        assert excinfo.value.code == "room_expired"
        assert excinfo.value.status == 410
    finally:
        await leave(host, room["room_id"])
