"""Tests for the orphaned-room cleanup utility."""
import io

import pytest

import nyoanime.party.cleanup as cleanup
from nyoanime.party.cleanup import (
    CleanupError,
    HostedRoom,
    create_authenticated_client,
    find_active_hosted_rooms,
    leave_active_hosted_rooms,
    leave_rooms,
    run_cleanup,
)
from tests.party.fake_supabase import FakeSession, FakeSupabase

USER_ID = "44444444-4444-4444-8444-444444444444"
OTHER_USER = "55555555-5555-4555-8555-555555555555"
ROOM_ACTIVE = "11111111-1111-4111-8111-111111111111"
ROOM_EXPIRED = "22222222-2222-4222-8222-222222222222"
ROOM_OTHER_HOST = "33333333-3333-4333-8333-333333333333"
ROOM_NOT_MEMBER = "44444444-4444-4444-8444-444444444444"


def make_fake(responses=None, rows=None):
    return FakeSupabase(
        session=FakeSession(USER_ID),
        user_id=USER_ID,
        responses=responses or {},
        rows=rows or {},
    )


def make_rows():
    return {
        "watch_room_members": [
            {"room_id": ROOM_ACTIVE, "user_id": USER_ID, "role": "host"},
            {"room_id": ROOM_EXPIRED, "user_id": USER_ID, "role": "host"},
            {"room_id": ROOM_OTHER_HOST, "user_id": USER_ID, "role": "guest"},
            {"room_id": ROOM_NOT_MEMBER, "user_id": OTHER_USER, "role": "host"},
        ],
        "watch_rooms": [
            {
                "id": ROOM_ACTIVE,
                "host_user_id": USER_ID,
                "status": "active",
                "created_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-01T02:00:00Z",
            },
            {
                "id": ROOM_EXPIRED,
                "host_user_id": USER_ID,
                "status": "expired",
                "created_at": "2026-01-02T00:00:00Z",
                "expires_at": "2026-01-02T02:00:00Z",
            },
            {
                "id": ROOM_OTHER_HOST,
                "host_user_id": OTHER_USER,
                "status": "active",
                "created_at": "2026-01-03T00:00:00Z",
                "expires_at": "2026-01-03T02:00:00Z",
            },
            {
                "id": ROOM_NOT_MEMBER,
                "host_user_id": USER_ID,
                "status": "active",
                "created_at": "2026-01-04T00:00:00Z",
                "expires_at": "2026-01-04T02:00:00Z",
            },
        ],
    }


@pytest.mark.asyncio
async def test_loader_reuses_existing_session_and_never_creates_identity(monkeypatch):
    fake = make_fake()

    async def fake_acreate_client(*args, **kwargs):
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)
    monkeypatch.setattr(
        cleanup,
        "resolve_supabase_config",
        lambda *args, **kwargs: ("https://example.supabase.co", "publishable-key"),
    )

    client, user_id = await create_authenticated_client()

    assert client is fake
    assert user_id == USER_ID
    assert fake.auth.sign_in_calls == 0
    assert fake.realtime.token == "test-access-token"


@pytest.mark.asyncio
async def test_loader_refuses_to_create_a_new_anonymous_identity(monkeypatch):
    fake = FakeSupabase(session=None, user_id=USER_ID)

    async def fake_acreate_client(*args, **kwargs):
        return fake

    monkeypatch.setattr("supabase.acreate_client", fake_acreate_client)
    monkeypatch.setattr(
        cleanup,
        "resolve_supabase_config",
        lambda *args, **kwargs: ("https://example.supabase.co", "publishable-key"),
    )

    with pytest.raises(CleanupError):
        await create_authenticated_client()

    assert fake.auth.sign_in_calls == 0


@pytest.mark.asyncio
async def test_find_only_active_rooms_hosted_by_current_user():
    fake = make_fake(rows=make_rows())

    rooms = await find_active_hosted_rooms(fake, USER_ID)

    assert rooms == [HostedRoom(
        room_id=ROOM_ACTIVE,
        created_at="2026-01-01T00:00:00Z",
        expires_at="2026-01-01T02:00:00Z",
    )]


@pytest.mark.asyncio
async def test_dry_run_never_calls_leave_room():
    fake = make_fake()
    rooms = [HostedRoom(ROOM_ACTIVE), HostedRoom(ROOM_EXPIRED)]

    results = await leave_rooms(fake, USER_ID, rooms, dry_run=True)

    assert [result.status for result in results] == ["would_leave", "would_leave"]
    assert fake.functions.calls == []


@pytest.mark.asyncio
async def test_confirm_calls_leave_room_edge_function_per_room():
    fake = make_fake({"leave-room": {"success": True}})
    rooms = [HostedRoom(ROOM_ACTIVE), HostedRoom(ROOM_EXPIRED)]

    results = await leave_rooms(fake, USER_ID, rooms, dry_run=False)

    assert [result.status for result in results] == ["left", "left"]
    assert [call[1]["body"]["room_id"] for call in fake.functions.calls] == [
        ROOM_ACTIVE,
        ROOM_EXPIRED,
    ]
    assert all(call[0] == "leave-room" for call in fake.functions.calls)


@pytest.mark.asyncio
async def test_leave_failure_is_reported_and_other_rooms_still_attempted():
    def respond(options):
        room_id = options["body"]["room_id"]
        if room_id == ROOM_EXPIRED:
            return {
                "success": False,
                "error": {"code": "network_error", "message": "temporary outage"},
            }
        return {"success": True}

    fake = make_fake({"leave-room": respond})
    rooms = [HostedRoom(ROOM_ACTIVE), HostedRoom(ROOM_EXPIRED)]

    results = await leave_rooms(fake, USER_ID, rooms, dry_run=False)

    assert results[0].status == "left"
    assert results[1].status == "failed"
    assert "network_error" in results[1].detail
    assert len(fake.functions.calls) == 2


@pytest.mark.asyncio
async def test_not_a_member_is_treated_as_already_left():
    fake = make_fake({
        "leave-room": {
            "success": False,
            "error": {"code": "not_a_member", "message": "already gone"},
        }
    })

    results = await leave_rooms(
        fake, USER_ID, [HostedRoom(ROOM_ACTIVE)], dry_run=False
    )

    assert results[0].status == "left"


@pytest.mark.asyncio
async def test_run_cleanup_defaults_to_dry_run_and_prints_count():
    fake = make_fake(rows=make_rows())
    out = io.StringIO()

    code = await run_cleanup(client=fake, user_id=USER_ID, confirm=False, out=out)

    rendered = out.getvalue()
    assert code == 0
    assert f"Found 1 active room(s)" in rendered
    assert ROOM_ACTIVE in rendered
    assert "Dry run: 1 active room(s) would be left." in rendered
    assert "No changes made." in rendered
    assert fake.functions.calls == []


@pytest.mark.asyncio
async def test_run_cleanup_with_confirm_reports_each_result():
    fake = make_fake(
        {"leave-room": {"success": True}},
        rows=make_rows(),
    )
    out = io.StringIO()

    code = await run_cleanup(client=fake, user_id=USER_ID, confirm=True, out=out)

    rendered = out.getvalue()
    assert code == 0
    assert f"[left] {ROOM_ACTIVE}" in rendered
    assert "Cleanup complete: 1 room(s) left." in rendered
    assert [call[1]["body"]["room_id"] for call in fake.functions.calls] == [
        ROOM_ACTIVE
    ]


@pytest.mark.asyncio
async def test_leave_active_hosted_rooms_leaves_only_hosted_rooms():
    fake = make_fake(
        responses={"leave-room": {"success": True}},
        rows=make_rows(),
    )

    left = await leave_active_hosted_rooms(client=fake, user_id=USER_ID)

    assert left == 1
    assert [name for name, _ in fake.functions.calls] == ["leave-room"]
    assert fake.functions.calls[0][1]["body"] == {"room_id": ROOM_ACTIVE}
