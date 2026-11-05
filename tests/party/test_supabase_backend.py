"""Static guarantees for the SQL schema, Edge Functions and Python client.

These tests do not execute SQL or call the cloud. They exist to prevent the
specific regressions found in the original implementation (TEXT identities,
missing RLS, fake clients, body-supplied user ids, non-private channels) from
coming back.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0010_watch_together.sql"
FUNCTIONS = ROOT / "supabase" / "functions"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def lower(path: Path) -> str:
    return read(path).lower()


def test_migration_uses_auth_users_uuid_identities():
    sql = read(MIGRATION)

    assert sql.count("references auth.users (id)") >= 2
    assert "host_user_id uuid not null references auth.users (id)" in sql
    assert "user_id uuid not null references auth.users (id)" in sql
    assert re.search(r"\bhost_user_id\s+text\b", sql, re.IGNORECASE) is None
    assert re.search(r"\buser_id\s+text\b", sql, re.IGNORECASE) is None


def test_migration_enables_rls_on_every_new_table():
    sql = lower(MIGRATION)

    for table in ("watch_rooms", "watch_room_members", "watch_join_attempts"):
        assert f"alter table public.{table} enable row level security" in sql


def test_no_unconditional_policies():
    sql = lower(MIGRATION)

    assert re.search(r"using\s*\(\s*true\s*\)", sql) is None
    assert re.search(r"with check\s*\(\s*true\s*\)", sql) is None
    assert "is_watch_room_member" in sql
    assert "is_watch_room_host" in sql


def test_room_codes_are_hashed_and_not_stored_in_plaintext():
    sql = read(MIGRATION)

    assert "room_code_hash text not null unique" in sql
    # Legacy lookup remains for rooms created before the HMAC change.
    assert "extensions.digest" in sql
    assert "extensions.gen_random_bytes" in sql
    assert "sha256" in sql
    # New rooms use a Vault-backed keyed hash.
    assert "vault.decrypted_secrets" in sql
    assert "watch_room_code_hmac_key" in sql
    assert "extensions.hmac" in sql
    assert "hmac-sha256:" in sql
    assert "room_code_secret_missing" in sql
    assert re.search(r"\bhmac\s*:=\s*'[^']+'", sql, re.IGNORECASE) is None


def test_membership_helpers_derive_caller_from_auth_uid():
    sql = read(MIGRATION)

    assert "p_user_id" not in sql
    assert "create or replace function public.is_watch_room_member(p_room_id uuid, p_user_id uuid)" not in sql
    assert "create or replace function public.is_watch_room_host(p_room_id uuid, p_user_id uuid)" not in sql
    assert "create or replace function public.is_watch_room_member(p_room_id uuid)" in sql
    assert "create or replace function public.is_watch_room_host(p_room_id uuid)" in sql

    member_fn = sql[sql.index("create or replace function public.is_watch_room_member(p_room_id uuid)"):]
    member_fn = member_fn[:member_fn.index("create or replace function public.is_watch_room_host")]
    host_fn = sql[sql.index("create or replace function public.is_watch_room_host(p_room_id uuid)"):]
    host_fn = host_fn[:host_fn.index("create or replace function public.watch_room_id_from_topic")]

    for body in (member_fn, host_fn):
        assert "auth.uid()" in body
        assert "security definer" in body
        assert "set search_path = ''" in body

    # Policies and grants must use the caller-scoped one-argument form.
    assert "is_watch_room_member(id, auth.uid())" not in sql
    assert "is_watch_room_member(room_id, auth.uid())" not in sql
    assert "grant execute on function public.is_watch_room_member(uuid)" in sql
    assert "grant execute on function public.is_watch_room_host(uuid)" in sql
    assert "revoke all on function public.is_watch_room_member(uuid) from public, anon" in sql
    assert "revoke all on function public.is_watch_room_host(uuid) from public, anon" in sql

    # The old overloads must be dropped, not merely shadowed by a new overload.
    assert "drop function if exists public.is_watch_room_member(uuid, uuid)" in sql
    assert "drop function if exists public.is_watch_room_host(uuid, uuid)" in sql


def test_join_rate_limit_uses_per_user_advisory_lock_before_counter():
    sql = read(MIGRATION)
    create_fn = sql[sql.index("create or replace function public.create_watch_room"):]
    create_fn = create_fn[:create_fn.index("create or replace function public.join_watch_room")]
    join_fn = sql[sql.index("create or replace function public.join_watch_room"):]
    join_fn = join_fn[:join_fn.index("create or replace function public.leave_watch_room")]

    create_lock = "pg_advisory_xact_lock(hashtextextended(v_user::text, 0))"
    join_lock = "pg_advisory_xact_lock(hashtextextended(v_user::text, 1))"
    assert create_lock in create_fn
    assert join_lock in join_fn
    # Distinct seeds prevent unrelated create/join traffic from serializing on
    # the same lock.
    assert create_lock != join_lock

    lock_index = join_fn.index(join_lock)
    counter_index = join_fn.index("select count(*) into v_recent_failures")
    assert lock_index < counter_index
    assert "v_recent_failures >= 10" in join_fn


def test_hmac_secret_is_vault_backed_and_creation_fails_closed():
    sql = read(MIGRATION)
    hmac_fn = sql[sql.index("create or replace function public.hmac_watch_room_code"):]
    hmac_fn = hmac_fn[:hmac_fn.index("create or replace function public.is_watch_room_member")]
    create_fn = sql[sql.index("create or replace function public.create_watch_room"):]
    create_fn = create_fn[:create_fn.index("create or replace function public.join_watch_room")]
    join_fn = sql[sql.index("create or replace function public.join_watch_room"):]
    join_fn = join_fn[:join_fn.index("create or replace function public.leave_watch_room")]

    assert "vault.decrypted_secrets" in hmac_fn
    assert "watch_room_code_hmac_key" in hmac_fn
    assert "extensions.hmac" in hmac_fn
    assert "hmac-sha256:" in hmac_fn
    assert "return null" in hmac_fn.lower()

    # New rooms must never fall back to the legacy hash when Vault is missing.
    assert "public.hmac_watch_room_code(v_code)" in create_fn
    assert "room_code_secret_missing" in create_fn
    assert "public.hash_watch_room_code(v_code)" not in create_fn

    # Legacy rooms stay joinable during the transition.
    assert "public.hmac_watch_room_code(v_code)" in join_fn
    assert "public.hash_watch_room_code(v_code)" in join_fn
    assert "'room_code_secret_missing'::text" in join_fn
    assert "room_code_hash = v_hmac" in join_fn
    assert "room_code_hash = v_legacy" in join_fn

    # A missing key is a server error, not a wrong guess: fail before recording
    # a failure that would rate-limit the caller after recovery.
    not_found = join_fn[join_fn.index("if not found then"):]
    not_found = not_found[:not_found.index("if v_room.status")]
    assert not_found.index("if v_hmac is null then") < not_found.index(
        "insert into public.watch_join_attempts"
    )

    http = read(FUNCTIONS / "_shared" / "http.ts")
    assert "room_code_secret_missing: 503" in http


def test_concurrency_guards_and_capacity():
    sql = lower(MIGRATION)

    assert "for update" in sql
    assert "maximum_members" in sql
    assert "between 2 and 8" in sql
    assert "watch_room_members_single_host" in sql
    assert "unique (room_id, user_id)" in sql
    assert "'room_full'" in sql


def test_join_failures_are_committed_not_rolled_back_by_raise():
    sql = read(MIGRATION)
    join_fn = sql[sql.index("create or replace function public.join_watch_room"):]
    join_fn = join_fn[:join_fn.index("create or replace function public.leave_watch_room")]

    # Expected join failures are returned as rows so inserts into
    # watch_join_attempts and expiry updates actually commit.
    assert "error_code text" in join_fn
    assert "return query select null::uuid" in join_fn
    assert "raise exception 'room_not_found'" not in join_fn
    assert "raise exception 'room_expired'" not in join_fn
    assert "raise exception 'room_full'" not in join_fn
    assert "insert into public.watch_join_attempts" in join_fn


def test_host_promotion_and_empty_room_expiry_are_trigger_driven():
    sql = lower(MIGRATION)
    trigger_fn = sql[sql.index(
        "create or replace function public.watch_room_member_after_delete"
    ):]
    trigger_fn = trigger_fn[:trigger_fn.index(
        "create or replace function public.leave_watch_room"
    )]
    leave_fn = sql[sql.index("create or replace function public.leave_watch_room"):]
    leave_fn = leave_fn[:leave_fn.index(
        "create or replace function public.expire_watch_rooms"
    )]

    assert "after delete on public.watch_room_members" in sql
    assert "for each row execute function public.watch_room_member_after_delete()" in sql
    assert "for update" in trigger_fn
    assert "ensure_watch_room_host" in trigger_fn
    assert "status = 'expired'" in trigger_fn
    assert "v_remaining = 0" in trigger_fn

    # leave_watch_room delegates promotion/expiry to the trigger instead of
    # duplicating it under a different lock order.
    assert "delete from public.watch_room_members" in leave_fn
    assert "ensure_watch_room_host" not in leave_fn
    assert "role = 'host'" not in leave_fn


def test_expiry_sweep_expires_empty_rooms_and_repairs_hostless_rooms():
    sql = lower(MIGRATION)
    expire_fn = sql[sql.index(
        "create or replace function public.expire_watch_rooms"
    ):]
    expire_fn = expire_fn[:expire_fn.index("-- keep cleanup automatic")]

    assert "expires_at <= now()" in expire_fn
    assert "not exists" in expire_fn
    assert "ensure_watch_room_host" in expire_fn
    assert "host_user_id = h.user_id" in expire_fn
    assert "pg_cron" in sql
    assert "nyoanime-watch-room-expiry" in sql
    assert "cron.schedule" in sql
    assert "cron.unschedule" in sql
    assert "select public.expire_watch_rooms();" in sql


def test_realtime_authorization_is_membership_scoped():
    sql = read(MIGRATION)

    assert "realtime.messages" in sql
    assert "realtime.topic()" in sql
    assert "watch_together_realtime_receive" in sql
    assert "watch_together_realtime_room_send" in sql
    assert "watch_together_realtime_members_send" in sql
    assert "watch_together_realtime_host_send" in sql
    assert re.search(r"topic\(\).*?watch-room:\[0-9a-fA-F-\]\{36\}\$", sql)
    assert re.search(r"topic.*:members", sql)
    assert re.search(r"topic.*:host", sql)
    assert "is_watch_room_host" in sql
    # Host-only event names are checked server-side on the shared topic.
    assert "realtime.messages.event" in sql
    assert "'sync_playback'" in sql


def test_migration_does_not_touch_unrelated_tables_or_weaken_policies():
    sql = lower(MIGRATION)

    assert "drop table" not in sql
    assert "truncate" not in sql
    assert "alter table public.analytics" not in sql
    assert "clock_timestamp_ms" not in sql
    assert "revoke all on table public.watch_rooms from anon, authenticated" in sql


def test_edge_functions_use_official_client_and_verified_identity():
    shared = read(FUNCTIONS / "_shared" / "auth.ts")

    assert 'npm:@supabase/supabase-js@2' in shared
    assert "SUPABASE_ANON_KEY" in shared
    assert "SUPABASE_SERVICE_ROLE_KEY" not in shared
    assert "client.auth.getUser(token)" in shared
    assert "missing_authorization" in shared
    assert "invalid_token" in shared

    for name in ("create-room", "join-room", "leave-room"):
        text = read(FUNCTIONS / name / "index.ts")
        assert "authenticate(req)" in text
        assert "caller.client.rpc" in text
        assert "Deno.serve" in text
        assert "digidomains" not in text
        assert "Not configured" not in text
        assert "body.user_id" not in text
        assert "SUPABASE_SERVICE_ROLE_KEY" not in text


def test_create_room_ignores_body_identity():
    text = read(FUNCTIONS / "create-room" / "index.ts")

    assert 'rpc("create_watch_room"' in text
    assert "host_user_id" not in text.replace(
        "// host_user_id in the request body is deliberately ignored.", ""
    ) or "body.host_user_id" not in text
    assert "p_nickname: nickname" in text
    assert "p_language_mode: languageMode" in text
    assert "p_max_members: maximumMembers" in text


def test_join_room_handles_rpc_error_rows_and_validates_code():
    text = read(FUNCTIONS / "join-room" / "index.ts")

    assert 'rpc("join_watch_room"' in text
    assert "error_code" in text
    assert "failureForCode" in text
    assert "/^[0-9]{6}$/" in text
    assert "p_room_code: roomCode" in text
    assert "p_nickname: nickname" in text


def test_leave_room_requires_uuid_and_membership_result():
    text = read(FUNCTIONS / "leave-room" / "index.ts")

    assert "isValidUuid(roomId)" in text
    assert 'rpc("leave_watch_room"' in text
    assert "p_room_id: roomId" in text
    assert "not_a_member" in text
    assert "data !== true" in text


def test_python_client_uses_verified_sdk_names_and_no_secrets():
    client = read(ROOT / "nyoanime" / "party" / "client.py")
    transport = read(ROOT / "nyoanime" / "party" / "transport.py")
    storage = read(ROOT / "nyoanime" / "party" / "session_storage.py")
    config = read(ROOT / "nyoanime" / "party" / "supabase_config.py")
    auth = read(ROOT / "nyoanime" / "party" / "supabase_auth.py")
    combined = "\n".join([client, transport, storage, config, auth])

    assert "acreate_client" in client
    assert "sign_in_anonymously" in auth
    assert "get_session" in auth
    assert "set_auth" in client
    assert re.search(r"sign_in_anonymous\s*\(", combined) is None
    assert "create_client(" not in client.replace("acreate_client(", "")
    assert "SUPABASE_SERVICE_ROLE_KEY" not in combined

    assert 'private": True' in transport
    assert 'f"watch-room:{self._room_id}"' in transport
    assert ":members" not in transport
    assert ":host" not in transport
    assert "members_channel" not in transport
    assert "host_channel" not in transport
    assert "send_broadcast" in transport
    assert "on_presence_sync" in transport
    assert "presence_state" in transport
    assert ".track(" in transport
    assert "channel.send(" not in transport
    assert "channel.on(" not in transport


def test_python_transport_sanitizes_playback_payloads():
    transport = read(ROOT / "nyoanime" / "party" / "transport.py")

    for key in ("stream_url", "cookie", "authorization", "mpv_socket", "ipc_path"):
        assert key in transport
    assert "_sanitize_payload" in transport
    assert "[link removed]" in transport
    assert "HOST_EVENTS" in transport
    assert "Only the host may send playback events" in transport


PLAYBACK_MIGRATION = (
    ROOT / "supabase" / "migrations" / "0011_watch_together_playback_state.sql"
)


def test_realtime_broadcast_policy_does_not_depend_on_the_probe_event_column():
    sql = read(PLAYBACK_MIGRATION)
    policy = sql[sql.index("create policy watch_together_realtime_room_send"):]
    policy = policy[: policy.index(";")]

    # Realtime's write probe inserts only (topic, extension); the event name is
    # NULL there, so any predicate on it denies every broadcast.
    assert "realtime.messages.event" not in policy
    assert "is_watch_room_member" in policy
    assert "watch_room_id_from_topic" in policy


def test_playback_rpc_is_host_only_and_strips_credentials():
    sql = lower(PLAYBACK_MIGRATION)
    fn = sql[sql.index("create or replace function public.set_watch_room_playback"):]

    assert "security definer" in fn
    assert "auth.uid()" in fn
    assert "public.is_watch_room_host(p_room_id)" in fn
    assert "'forbidden'" in fn
    assert "status = 'active'" in fn
    assert "expires_at > now()" in fn
    assert "playback_seq = r.playback_seq + 1" in fn
    assert "'cookie'" in fn
    assert "'authorization'" in fn
    assert "revoke all on function public.set_watch_room_playback" in fn
    assert "grant execute on function public.set_watch_room_playback" in fn


def test_playback_columns_stay_member_readable_through_watch_rooms_rls():
    sql = lower(PLAYBACK_MIGRATION)
    assert "add column if not exists playback jsonb" in sql
    assert "add column if not exists playback_reference jsonb" in sql
    assert "add column if not exists playback_seq bigint" in sql
    # Reads reuse the existing watch_rooms policy; no new broad policy is added.
    assert "create policy" not in sql.replace(
        "create policy watch_together_realtime_room_send", ""
    )
