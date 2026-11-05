"""Database-backed checks for the Watch Together migration.

These tests run the real migration against a disposable PostgreSQL database.
They are skipped unless ``NYOANIME_TEST_DATABASE_URL`` points at an instance
where the connecting user may create databases and roles.

The harness creates a uniquely named database, adds the Supabase roles if they
are missing, installs minimal ``auth``, ``realtime`` and ``vault`` stand-ins,
applies the real migration, then drops the database. It never modifies an
existing database or the remote project.
"""
import os
import secrets
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0010_watch_together.sql"
DATABASE_URL = os.environ.get("NYOANIME_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set NYOANIME_TEST_DATABASE_URL to a disposable PostgreSQL instance",
)

BOOTSTRAP_SQL = """
create schema if not exists auth;
create schema if not exists realtime;
create schema if not exists vault;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin noinherit bypassrls;
    end if;
end
$$;

create table if not exists auth.users (
    id uuid primary key,
    email text,
    created_at timestamptz not null default now()
);

create or replace function auth.uid()
returns uuid
language sql
stable
as $$
    select coalesce(
        nullif(current_setting('test.uid', true), '')::uuid,
        nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
    );
$$;

grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;

create table if not exists realtime.messages (
    id bigint generated always as identity primary key,
    topic text not null,
    extension text not null default 'broadcast',
    event text,
    payload jsonb,
    private boolean not null default true,
    inserted_at timestamptz not null default now()
);
alter table realtime.messages enable row level security;

create or replace function realtime.topic()
returns text
language sql
stable
as $$
    select nullif(current_setting('realtime.topic', true), '');
$$;

grant usage on schema realtime to anon, authenticated, service_role;
grant execute on function realtime.topic() to anon, authenticated, service_role;
grant select, insert on table realtime.messages to authenticated;

create table if not exists vault.secrets (
    id uuid primary key default gen_random_uuid(),
    name text unique,
    secret text not null,
    description text,
    created_at timestamptz not null default now()
);

create or replace view vault.decrypted_secrets as
select id, name, secret as decrypted_secret, description, created_at
from vault.secrets;

grant usage on schema vault to service_role;
grant usage on schema public to anon, authenticated, service_role;
"""

HMAC_SECRET_NAME = "watch_room_code_hmac_key"


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self, *, autocommit: bool = True):
        return psycopg.connect(self.dsn, autocommit=autocommit)

    def create_user(self) -> uuid.UUID:
        user_id = uuid.uuid4()
        with self.connect() as conn:
            conn.execute("insert into auth.users (id) values (%s)", (user_id,))
        return user_id

    @contextmanager
    def as_user(self, user_id: uuid.UUID, *, autocommit: bool = True):
        conn = psycopg.connect(self.dsn, autocommit=autocommit)
        try:
            conn.execute("set role authenticated")
            conn.execute("select set_config('test.uid', %s, false)", (str(user_id),))
            yield conn
        finally:
            conn.close()


@pytest.fixture(scope="session")
def database():
    base = conninfo_to_dict(DATABASE_URL)
    admin_dsn = make_conninfo(**base)
    name = f"nyoanime_wt_test_{uuid.uuid4().hex[:12]}"

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'create database "{name}"')

    dsn = make_conninfo(**{**base, "dbname": name})
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(BOOTSTRAP_SQL)
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
            conn.execute(
                "insert into vault.secrets (name, secret) values (%s, %s)",
                (HMAC_SECRET_NAME, secrets.token_hex(32)),
            )
        yield Database(dsn)
    finally:
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute(f'drop database if exists "{name}" with (force)')
        except psycopg.Error:
            pass


def create_room(database, host_id, *, nickname="alice", maximum_members=8):
    with database.as_user(host_id) as conn:
        return conn.execute(
            "select room_id, room_code, participant_id, role "
            "from public.create_watch_room(%s, 'ar_sub', %s)",
            (nickname, maximum_members),
        ).fetchone()


def join_room(database, user_id, code, nickname="guest"):
    with database.as_user(user_id) as conn:
        return conn.execute(
            "select room_id, participant_id, role, nickname, room_code, error_code "
            "from public.join_watch_room(%s, %s)",
            (code, nickname),
        ).fetchone()


def unused_code(database):
    with database.connect() as conn:
        for _ in range(50):
            code = f"{secrets.randbelow(1_000_000):06d}"
            exists = conn.execute(
                "select exists ("
                "  select 1 from public.watch_rooms"
                "  where room_code_hash = public.hmac_watch_room_code(%s)"
                "     or room_code_hash = public.hash_watch_room_code(%s)"
                ")",
                (code, code),
            ).fetchone()[0]
            if not exists:
                return code
    raise AssertionError("could not find an unused six-digit room code")


def test_membership_helpers_are_caller_scoped(database):
    with database.connect() as conn:
        old_member, old_host, member_sig, host_sig = conn.execute(
            "select"
            "  to_regprocedure('public.is_watch_room_member(uuid, uuid)'),"
            "  to_regprocedure('public.is_watch_room_host(uuid, uuid)'),"
            "  pg_get_function_identity_arguments("
            "    to_regprocedure('public.is_watch_room_member(uuid)')),"
            "  pg_get_function_identity_arguments("
            "    to_regprocedure('public.is_watch_room_host(uuid)'))"
        ).fetchone()

        assert old_member is None
        assert old_host is None
        assert member_sig == "p_room_id uuid"
        assert host_sig == "p_room_id uuid"

        for function in (
            "public.is_watch_room_member(uuid)",
            "public.is_watch_room_host(uuid)",
        ):
            secdef, config = conn.execute(
                "select prosecdef, proconfig from pg_proc "
                "where oid = %s::regprocedure",
                (function,),
            ).fetchone()
            assert secdef is True
            assert config == ['search_path=""']

    alice = database.create_user()
    bob = database.create_user()
    room_id, code, _, _ = create_room(database, alice)

    with database.as_user(bob) as conn:
        member, host = conn.execute(
            "select public.is_watch_room_member(%s), public.is_watch_room_host(%s)",
            (room_id, room_id),
        ).fetchone()
        visible_rooms = conn.execute(
            "select count(*) from public.watch_rooms where id = %s", (room_id,)
        ).fetchone()[0]
        visible_members = conn.execute(
            "select count(*) from public.watch_room_members where room_id = %s",
            (room_id,),
        ).fetchone()[0]

    assert member is False
    assert host is False
    assert visible_rooms == 0
    assert visible_members == 0

    with database.as_user(alice) as conn:
        member, host = conn.execute(
            "select public.is_watch_room_member(%s), public.is_watch_room_host(%s)",
            (room_id, room_id),
        ).fetchone()
        visible_rooms = conn.execute(
            "select count(*) from public.watch_rooms where id = %s", (room_id,)
        ).fetchone()[0]
        visible_members = conn.execute(
            "select count(*) from public.watch_room_members where room_id = %s",
            (room_id,),
        ).fetchone()[0]
        other_member = conn.execute(
            "select public.is_watch_room_member(%s)", (uuid.uuid4(),)
        ).fetchone()[0]

    assert member is True
    assert host is True
    assert visible_rooms == 1
    assert visible_members == 1
    assert other_member is False


def test_function_privileges_are_least_privilege(database):
    with database.connect() as conn:
        rows = conn.execute(
            "select p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',"
            "       has_function_privilege('authenticated', p.oid, 'EXECUTE'),"
            "       has_function_privilege('anon', p.oid, 'EXECUTE')"
            " from pg_proc p"
            " join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'public'"
            "   and p.proname in ("
            "     'generate_watch_room_code', 'hash_watch_room_code',"
            "     'hmac_watch_room_code', 'is_watch_room_member',"
            "     'is_watch_room_host', 'create_watch_room',"
            "     'join_watch_room', 'leave_watch_room', 'expire_watch_rooms',"
            "     'ensure_watch_room_host', 'watch_room_member_after_delete'"
            "   )"
        ).fetchall()

    privileges = {name: (auth, anon) for name, auth, anon in rows}

    for name in (
        "generate_watch_room_code()",
        "hash_watch_room_code(p_code text)",
        "hmac_watch_room_code(p_code text)",
        "expire_watch_rooms()",
        "ensure_watch_room_host(p_room_id uuid)",
        "watch_room_member_after_delete()",
    ):
        assert privileges[name] == (False, False)

    for name in (
        "is_watch_room_member(p_room_id uuid)",
        "is_watch_room_host(p_room_id uuid)",
        "leave_watch_room(p_room_id uuid)",
        "create_watch_room(p_nickname text, p_language_mode text, p_max_members integer)",
        "join_watch_room(p_room_code text, p_nickname text)",
    ):
        assert privileges[name] == (True, False)


def test_one_user_cannot_join_or_read_another_users_room(database):
    alice = database.create_user()
    bob = database.create_user()
    room_id, code, _, role = create_room(database, alice)

    assert role == "host"

    result = join_room(database, bob, code, "bob")
    assert result[0] == room_id
    assert result[2] == "guest"
    assert result[5] is None

    # Bob is now a member, but caller-scoped helpers still only describe Bob.
    with database.as_user(bob) as conn:
        assert conn.execute(
            "select public.is_watch_room_member(%s)", (room_id,)
        ).fetchone()[0] is True
        assert conn.execute(
            "select public.is_watch_room_host(%s)", (room_id,)
        ).fetchone()[0] is False

    carol = database.create_user()
    with database.as_user(carol) as conn:
        assert conn.execute(
            "select public.is_watch_room_member(%s), public.is_watch_room_host(%s)",
            (room_id, room_id),
        ).fetchone() == (False, False)
        assert conn.execute(
            "select count(*) from public.watch_room_members where room_id = %s",
            (room_id,),
        ).fetchone()[0] == 0


def test_private_realtime_topic_enforces_membership_and_host_role(database):
    alice = database.create_user()
    bob = database.create_user()
    carol = database.create_user()
    room_id, code, _, _ = create_room(database, alice)
    assert join_room(database, bob, code, "bob")[5] is None

    topic = f"watch-room:{room_id}"

    def send(user_id, event):
        with database.as_user(user_id) as conn:
            conn.execute("select set_config('realtime.topic', %s, false)", (topic,))
            conn.execute(
                "insert into realtime.messages (topic, event, payload) "
                "values (%s, %s, '{}'::jsonb)",
                (topic, event),
            )

    send(alice, "chat")
    send(bob, "chat")

    with database.as_user(carol) as conn:
        conn.execute("select set_config('realtime.topic', %s, false)", (topic,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into realtime.messages (topic, event, payload) "
                "values (%s, 'chat', '{}'::jsonb)",
                (topic,),
            )
        assert conn.execute(
            "select count(*) from realtime.messages where topic = %s",
            (topic,),
        ).fetchone()[0] == 2

    # Only the host may publish authoritative playback events on the shared
    # topic. Chat from the same guest still works.
    with database.as_user(bob) as conn:
        conn.execute("select set_config('realtime.topic', %s, false)", (topic,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into realtime.messages (topic, event, payload) "
                "values (%s, 'play', '{}'::jsonb)",
                (topic,),
            )

    send(alice, "play")

    with database.as_user(alice) as conn:
        conn.execute("select set_config('realtime.topic', %s, false)", (topic,))
        assert conn.execute(
            "select count(*) from realtime.messages where topic = %s",
            (topic,),
        ).fetchone()[0] == 3
    with database.as_user(carol) as conn:
        conn.execute("select set_config('realtime.topic', %s, false)", (topic,))
        assert conn.execute(
            "select count(*) from realtime.messages where topic = %s",
            (topic,),
        ).fetchone()[0] == 0


def test_member_delete_expires_empty_rooms_and_promotes_a_host(database):
    host = database.create_user()
    guest = database.create_user()
    room_id, code, _, _ = create_room(database, host)
    assert join_room(database, guest, code, "guest")[5] is None

    with database.connect() as conn:
        conn.execute(
            "delete from public.watch_room_members "
            "where room_id = %s and user_id = %s",
            (room_id, host),
        )
        new_host = conn.execute(
            "select host_user_id from public.watch_rooms where id = %s",
            (room_id,),
        ).fetchone()[0]
        promoted_role = conn.execute(
            "select role from public.watch_room_members "
            "where room_id = %s and user_id = %s",
            (room_id, guest),
        ).fetchone()[0]

    assert new_host == guest
    assert promoted_role == "host"

    with database.connect() as conn:
        conn.execute(
            "delete from public.watch_room_members where room_id = %s",
            (room_id,),
        )
        status, ended_at = conn.execute(
            "select status, ended_at from public.watch_rooms where id = %s",
            (room_id,),
        ).fetchone()

    assert status == "expired"
    assert ended_at is not None


def test_expiry_sweep_closes_empty_and_expired_rooms_and_repairs_hostless(database):
    host = database.create_user()
    guest = database.create_user()

    expired_room, _, _, _ = create_room(database, host)
    with database.connect() as conn:
        conn.execute(
            "update public.watch_rooms set expires_at = now() - interval '1 minute' "
            "where id = %s",
            (expired_room,),
        )

    # Bypass the delete trigger to simulate a room whose membership vanished
    # outside the normal leave path.
    empty_room, _, _, _ = create_room(database, host)
    with database.connect(autocommit=False) as conn:
        conn.execute(
            "alter table public.watch_room_members "
            "disable trigger watch_room_member_after_delete"
        )
        conn.execute(
            "delete from public.watch_room_members where room_id = %s",
            (empty_room,),
        )
        conn.execute(
            "alter table public.watch_room_members "
            "enable trigger watch_room_member_after_delete"
        )

    hostless_room, hostless_code, _, _ = create_room(database, host)
    assert join_room(database, guest, hostless_code, "guest")[5] is None
    with database.connect(autocommit=False) as conn:
        conn.execute(
            "alter table public.watch_room_members "
            "disable trigger watch_room_member_after_delete"
        )
        conn.execute(
            "delete from public.watch_room_members "
            "where room_id = %s and user_id = %s",
            (hostless_room, host),
        )
        conn.execute(
            "alter table public.watch_room_members "
            "enable trigger watch_room_member_after_delete"
        )

    with database.connect() as conn:
        expired_count = conn.execute(
            "select public.expire_watch_rooms()"
        ).fetchone()[0]
        statuses = dict(
            conn.execute(
                "select id, status from public.watch_rooms "
                "where id in (%s, %s, %s)",
                (expired_room, empty_room, hostless_room),
            ).fetchall()
        )
        repaired_host = conn.execute(
            "select host_user_id from public.watch_rooms where id = %s",
            (hostless_room,),
        ).fetchone()[0]
        repaired_role = conn.execute(
            "select role from public.watch_room_members "
            "where room_id = %s and user_id = %s",
            (hostless_room, guest),
        ).fetchone()[0]

    assert expired_count >= 2
    assert statuses[expired_room] == "expired"
    assert statuses[empty_room] == "expired"
    assert statuses[hostless_room] == "active"
    assert repaired_host == guest
    assert repaired_role == "host"


def test_concurrent_failed_joins_cannot_bypass_rate_limit(database):
    user_id = database.create_user()
    code = unused_code(database)
    results = []
    errors = []
    guard = threading.Lock()

    def attempt():
        try:
            row = join_room(database, user_id, code, "guess")
            with guard:
                results.append(row[5])
        except Exception as exc:  # noqa: BLE001 - surfaced below
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert Counter(results) == Counter({"room_not_found": 10, "rate_limited": 15})

    with database.connect() as conn:
        recorded = conn.execute(
            "select count(*) from public.watch_join_attempts where user_id = %s",
            (user_id,),
        ).fetchone()[0]
    assert recorded == 10


def test_missing_vault_secret_fails_closed(database):
    user_id = database.create_user()
    code = unused_code(database)

    with database.connect(autocommit=False) as conn:
        conn.execute("delete from vault.secrets where name = %s", (HMAC_SECRET_NAME,))
        conn.execute("set local role authenticated")
        conn.execute(
            "select set_config('test.uid', %s, true)", (str(user_id),)
        )
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            conn.execute("select * from public.create_watch_room('dave')")
        assert excinfo.value.diag.message_primary == "room_code_secret_missing"
        conn.rollback()

        conn.execute("delete from vault.secrets where name = %s", (HMAC_SECRET_NAME,))
        conn.execute("set local role authenticated")
        conn.execute(
            "select set_config('test.uid', %s, true)", (str(user_id),)
        )
        row = conn.execute(
            "select error_code from public.join_watch_room(%s, 'dave')", (code,)
        ).fetchone()
        assert row[0] == "room_code_secret_missing"
        conn.execute("reset role")
        assert conn.execute(
            "select count(*) from public.watch_join_attempts where user_id = %s",
            (user_id,),
        ).fetchone()[0] == 0
        conn.rollback()

    with database.connect() as conn:
        assert conn.execute(
            "select count(*) from vault.secrets where name = %s",
            (HMAC_SECRET_NAME,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "select count(*) from public.watch_rooms where host_user_id = %s",
            (user_id,),
        ).fetchone()[0] == 0


def test_capacity_duplicate_join_and_host_promotion_are_preserved(database):
    host = database.create_user()
    guest = database.create_user()
    third = database.create_user()

    room_id, code, host_participant, role = create_room(
        database, host, maximum_members=2
    )
    assert role == "host"

    first = join_room(database, guest, code, "guest")
    duplicate = join_room(database, guest, code, "guest-renamed")
    assert first[5] is None
    assert duplicate[5] is None
    assert duplicate[1] == first[1]
    assert duplicate[2] == "guest"
    assert duplicate[3] == "guest-renamed"

    full = join_room(database, third, code, "third")
    assert full[5] == "room_full"

    with database.as_user(host) as conn:
        assert conn.execute(
            "select public.leave_watch_room(%s)", (room_id,)
        ).fetchone()[0] is True

    with database.connect() as conn:
        new_host = conn.execute(
            "select host_user_id from public.watch_rooms where id = %s",
            (room_id,),
        ).fetchone()[0]
        promoted_role = conn.execute(
            "select role from public.watch_room_members "
            "where room_id = %s and user_id = %s",
            (room_id, guest),
        ).fetchone()[0]

    assert new_host == guest
    assert promoted_role == "host"

    with database.as_user(third) as conn:
        assert conn.execute(
            "select public.leave_watch_room(%s)", (room_id,)
        ).fetchone()[0] is False
