-- Watch Together: rooms, membership, anti-abuse and Realtime authorization.
--
-- This migration is additive. It does not touch any existing analytics table,
-- policy, function or data. It is written to be safe to run on a project that
-- already has unrelated public tables and realtime policies.
--
-- Identity model: every participant is an authenticated Supabase user. NyoAnime
-- signs anonymously in with Supabase Auth, so auth.uid() is a real auth.users id.
-- No table accepts a client-supplied user id.

begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table if not exists public.watch_rooms (
    id uuid primary key default gen_random_uuid(),
    -- Only a keyed hash of the six-digit human code is stored. New rooms use
    -- the Vault HMAC; legacy sha256 rows are matched during the transition.
    room_code_hash text not null unique,
    host_user_id uuid not null references auth.users (id) on delete cascade,
    language_mode text not null default 'ar_sub'
        check (language_mode in ('ar_sub', 'en_sub', 'en_dub')),
    maximum_members integer not null default 8
        check (maximum_members between 2 and 8),
    status text not null default 'active'
        check (status in ('active', 'expired', 'ended')),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '2 hours'),
    ended_at timestamptz,
    constraint watch_rooms_expiry_after_creation check (expires_at > created_at)
);

create table if not exists public.watch_room_members (
    id uuid primary key default gen_random_uuid(),
    room_id uuid not null references public.watch_rooms (id) on delete cascade,
    user_id uuid not null references auth.users (id) on delete cascade,
    nickname text not null check (char_length(btrim(nickname)) between 1 and 32),
    role text not null default 'guest' check (role in ('host', 'guest')),
    joined_at timestamptz not null default now(),
    last_seen timestamptz not null default now(),
    unique (room_id, user_id)
);

-- One host per room, enforced even under concurrent host promotion.
create unique index if not exists watch_room_members_single_host
    on public.watch_room_members (room_id)
    where role = 'host';

create index if not exists watch_rooms_status_expiry
    on public.watch_rooms (status, expires_at);
create index if not exists watch_rooms_host_user
    on public.watch_rooms (host_user_id);
create index if not exists watch_room_members_room
    on public.watch_room_members (room_id);
create index if not exists watch_room_members_user
    on public.watch_room_members (user_id);

-- Failed-join bookkeeping for room-code guessing protection. RPC-only table.
create table if not exists public.watch_join_attempts (
    id bigint generated always as identity primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    room_code_hash text not null,
    attempted_at timestamptz not null default now()
);

create index if not exists watch_join_attempts_user_time
    on public.watch_join_attempts (user_id, attempted_at);

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

-- The previous two-argument helpers accepted an arbitrary user id and were
-- executable by authenticated clients, which allowed membership and host
-- enumeration. Policies are dropped first because they depend on the helpers.
drop policy if exists watch_together_rooms_select_members on public.watch_rooms;
drop policy if exists watch_together_members_select_members on public.watch_room_members;
drop policy if exists watch_together_realtime_receive on realtime.messages;
drop policy if exists watch_together_realtime_room_send on realtime.messages;
drop policy if exists watch_together_realtime_members_send on realtime.messages;
drop policy if exists watch_together_realtime_host_send on realtime.messages;
drop function if exists public.is_watch_room_member(uuid, uuid);
drop function if exists public.is_watch_room_host(uuid, uuid);

-- pgcrypto random bytes -> uniform six-digit code. Leading zeros are kept.
create or replace function public.generate_watch_room_code()
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_num bigint;
begin
    v_num := abs((('x' || encode(extensions.gen_random_bytes(4), 'hex'))::bit(32)::bigint)) % 1000000;
    return lpad(v_num::text, 6, '0');
end;
$$;

-- Legacy SHA-256 hash. New rooms never use it; it is retained so rooms created
-- before the Vault-backed HMAC change remain joinable until they expire.
create or replace function public.hash_watch_room_code(p_code text)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
    select encode(extensions.digest(btrim(p_code), 'sha256'), 'hex');
$$;

-- Keyed hash for new rooms. The key lives in Supabase Vault and is never
-- exposed to clients. Returns NULL when the secret cannot be read so callers
-- can fail closed instead of silently falling back to the weak legacy hash.
create or replace function public.hmac_watch_room_code(p_code text)
returns text
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
    v_secret text;
begin
    select s.decrypted_secret into v_secret
    from vault.decrypted_secrets s
    where s.name = 'watch_room_code_hmac_key'
    limit 1;

    if v_secret is null or char_length(v_secret) = 0 then
        return null;
    end if;

    return 'hmac-sha256:' || encode(
        extensions.hmac(btrim(p_code), v_secret, 'sha256'),
        'hex'
    );
exception
    when others then
        -- Missing Vault schema/table, missing pgcrypto or a permission problem
        -- all mean the keyed hash is unavailable. Never degrade silently.
        raise warning 'watch_room_code_hmac unavailable: %', sqlerrm;
        return null;
end;
$$;

-- Both helpers derive the caller from auth.uid(). They must never accept a
-- user id parameter: that would let any authenticated client enumerate another
-- user's membership or host status.
create or replace function public.is_watch_room_member(p_room_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.watch_room_members m
        where m.room_id = p_room_id
          and m.user_id = auth.uid()
    );
$$;

create or replace function public.is_watch_room_host(p_room_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.watch_room_members m
        where m.room_id = p_room_id
          and m.user_id = auth.uid()
          and m.role = 'host'
    );
$$;

-- Safely extracts the room UUID from "watch-room:<uuid>[:members|:host]".
-- Returns NULL for any topic that is not a Watch Together topic.
create or replace function public.watch_room_id_from_topic(p_topic text)
returns uuid
language plpgsql
immutable
set search_path = ''
as $$
declare
    v_match text;
begin
    if p_topic is null then
        return null;
    end if;
    v_match := substring(p_topic from '^watch-room:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?::(?:members|host))?$');
    if v_match is null then
        return null;
    end if;
    return v_match::uuid;
exception
    when others then
        return null;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPCs (callable by authenticated users; identity always comes from auth.uid())
-- ---------------------------------------------------------------------------

create or replace function public.create_watch_room(
    p_nickname text,
    p_language_mode text default 'ar_sub',
    p_max_members integer default 8
)
returns table (
    room_id uuid,
    room_code text,
    participant_id uuid,
    role text,
    expires_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_user uuid := auth.uid();
    v_nick text := btrim(coalesce(p_nickname, ''));
    v_code text;
    v_hash text;
    v_room uuid;
    v_participant uuid;
    v_expires timestamptz;
    v_attempt integer := 0;
    v_active_hosted integer;
begin
    if v_user is null then
        raise exception 'not_authenticated' using errcode = '28000';
    end if;
    if char_length(v_nick) < 1 or char_length(v_nick) > 32 then
        raise exception 'invalid_nickname' using errcode = '22023';
    end if;
    if coalesce(p_language_mode, '') not in ('ar_sub', 'en_sub', 'en_dub') then
        raise exception 'invalid_language_mode' using errcode = '22023';
    end if;
    if p_max_members is null or p_max_members < 2 or p_max_members > 8 then
        raise exception 'invalid_capacity' using errcode = '22023';
    end if;

    -- Light abuse guard: a single account cannot hoard live rooms.
    -- Serialize per user so concurrent creates cannot exceed the limit.
    perform pg_advisory_xact_lock(hashtextextended(v_user::text, 0));
    select count(*) into v_active_hosted
    from public.watch_rooms r
    where r.host_user_id = v_user
      and r.status = 'active'
      and r.expires_at > now();
    if v_active_hosted >= 3 then
        raise exception 'too_many_rooms' using errcode = 'P0001';
    end if;

    loop
        v_attempt := v_attempt + 1;
        v_code := public.generate_watch_room_code();
        v_hash := public.hmac_watch_room_code(v_code);
        if v_hash is null then
            raise exception 'room_code_secret_missing' using errcode = 'P0001';
        end if;
        begin
            insert into public.watch_rooms (room_code_hash, host_user_id, language_mode, maximum_members)
            values (v_hash, v_user, p_language_mode, p_max_members)
            returning id into v_room;
            exit;
        exception
            when unique_violation then
                if v_attempt >= 12 then
                    raise exception 'room_code_generation_failed' using errcode = 'P0001';
                end if;
        end;
    end loop;

    insert into public.watch_room_members (room_id, user_id, nickname, role)
    values (v_room, v_user, v_nick, 'host')
    returning id into v_participant;

    select r.expires_at into v_expires
    from public.watch_rooms r
    where r.id = v_room;

    return query
        select v_room, v_code, v_participant, 'host'::text, v_expires;
end;
$$;

create or replace function public.join_watch_room(
    p_room_code text,
    p_nickname text
)
returns table (
    room_id uuid,
    participant_id uuid,
    role text,
    nickname text,
    room_code text,
    error_code text
)
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_user uuid := auth.uid();
    v_nick text := btrim(coalesce(p_nickname, ''));
    v_code text := btrim(coalesce(p_room_code, ''));
    v_hmac text;
    v_legacy text;
    v_lookup_hash text;
    v_room public.watch_rooms%rowtype;
    v_member public.watch_room_members%rowtype;
    v_count integer;
    v_recent_failures integer;
begin
    if v_user is null then
        raise exception 'not_authenticated' using errcode = '28000';
    end if;

    -- Expected failures are returned as rows, not raised, so that the failure
    -- counter insert and the expiry status update are committed.
    if v_code !~ '^[0-9]{6}$' then
        return query select null::uuid, null::uuid, null::text, null::text, null::text, 'invalid_room_code'::text;
        return;
    end if;
    if char_length(v_nick) < 1 or char_length(v_nick) > 32 then
        return query select null::uuid, null::uuid, null::text, null::text, null::text, 'invalid_nickname'::text;
        return;
    end if;

    -- Serialize per-user join attempts before the failure counter is read.
    -- Seed 1 is distinct from the room-creation lock (seed 0); sharing a seed
    -- would make room creation and joining block each other unnecessarily.
    perform pg_advisory_xact_lock(hashtextextended(v_user::text, 1));

    -- Throttle code guessing: 10 failures per rolling five minutes.
    select count(*) into v_recent_failures
    from public.watch_join_attempts a
    where a.user_id = v_user
      and a.attempted_at > now() - interval '5 minutes';
    if v_recent_failures >= 10 then
        return query select null::uuid, null::uuid, null::text, null::text, null::text, 'rate_limited'::text;
        return;
    end if;

    v_hmac := public.hmac_watch_room_code(v_code);
    v_legacy := public.hash_watch_room_code(v_code);
    v_lookup_hash := coalesce(v_hmac, v_legacy);

    -- Lock the room row so capacity checks and inserts are serialized.
    select r.* into v_room
    from public.watch_rooms r
    where r.room_code_hash = v_hmac
       or r.room_code_hash = v_legacy
    for update;

    if not found then
        -- A missing Vault key is a server misconfiguration, not a wrong guess.
        -- Fail closed without polluting the caller's failure counter.
        if v_hmac is null then
            return query select null::uuid, null::uuid, null::text, null::text, null::text, 'room_code_secret_missing'::text;
            return;
        end if;
        insert into public.watch_join_attempts (user_id, room_code_hash)
        values (v_user, v_lookup_hash);
        return query select null::uuid, null::uuid, null::text, null::text, null::text, 'room_not_found'::text;
        return;
    end if;

    if v_room.status <> 'active' or v_room.expires_at <= now() then
        if v_room.status = 'active' then
            update public.watch_rooms r
            set status = 'expired', ended_at = now()
            where r.id = v_room.id;
        end if;
        insert into public.watch_join_attempts (user_id, room_code_hash)
        values (v_user, v_lookup_hash);
        return query select null::uuid, null::uuid, null::text, null::text, null::text, 'room_expired'::text;
        return;
    end if;

    -- Duplicate join / reconnection: refresh presence and return the same row.
    select m.* into v_member
    from public.watch_room_members m
    where m.room_id = v_room.id
      and m.user_id = v_user
    for update;

    if found then
        update public.watch_room_members m
        set nickname = v_nick, last_seen = now()
        where m.id = v_member.id
        returning m.* into v_member;

        delete from public.watch_join_attempts a where a.user_id = v_user;

        return query
            select v_room.id, v_member.id, v_member.role, v_member.nickname, v_code, null::text;
        return;
    end if;

    select count(*) into v_count
    from public.watch_room_members m
    where m.room_id = v_room.id;

    if v_count >= v_room.maximum_members then
        return query select null::uuid, null::uuid, null::text, null::text, null::text, 'room_full'::text;
        return;
    end if;

    insert into public.watch_room_members (room_id, user_id, nickname, role)
    values (v_room.id, v_user, v_nick, 'guest')
    returning * into v_member;

    delete from public.watch_join_attempts a where a.user_id = v_user;

    return query
        select v_room.id, v_member.id, v_member.role, v_member.nickname, v_code, null::text;
end;
$$;

-- Promote the earliest remaining member when a live room has no host.
-- Callers must hold the room row lock; the partial unique host index makes
-- concurrent promotions fail rather than create two hosts.
create or replace function public.ensure_watch_room_host(p_room_id uuid)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_next public.watch_room_members%rowtype;
begin
    if exists (
        select 1 from public.watch_room_members m
        where m.room_id = p_room_id
          and m.role = 'host'
    ) then
        return null;
    end if;

    select m.* into v_next
    from public.watch_room_members m
    where m.room_id = p_room_id
    order by m.joined_at, m.id
    limit 1;

    if v_next.id is null then
        return null;
    end if;

    update public.watch_room_members m
    set role = 'host'
    where m.id = v_next.id;

    update public.watch_rooms r
    set host_user_id = v_next.user_id
    where r.id = p_room_id;

    return v_next.user_id;
end;
$$;

-- Deleting the last membership expires the room. Deleting the host promotes the
-- next member. Explicit leave, RPC-driven removal and cascade paths all use this
-- trigger. A client that simply disappears keeps its membership until the room
-- expires or the scheduled sweep closes it.
create or replace function public.watch_room_member_after_delete()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_room_id uuid;
    v_status text;
    v_remaining integer;
begin
    select r.id, r.status into v_room_id, v_status
    from public.watch_rooms r
    where r.id = old.room_id
    for update;

    -- The room itself may be disappearing through a cascade, or the sweep is
    -- already retiring it. Nothing left to promote in either case.
    if v_room_id is null or v_status <> 'active' then
        return old;
    end if;

    select count(*) into v_remaining
    from public.watch_room_members m
    where m.room_id = old.room_id;

    if v_remaining = 0 then
        update public.watch_rooms r
        set status = 'expired', ended_at = coalesce(r.ended_at, now())
        where r.id = old.room_id
          and r.status = 'active';
        return old;
    end if;

    perform public.ensure_watch_room_host(old.room_id);
    return old;
end;
$$;

drop trigger if exists watch_room_member_after_delete on public.watch_room_members;
create trigger watch_room_member_after_delete
after delete on public.watch_room_members
for each row execute function public.watch_room_member_after_delete();

create or replace function public.leave_watch_room(p_room_id uuid)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_user uuid := auth.uid();
    v_role text;
begin
    if v_user is null then
        raise exception 'not_authenticated' using errcode = '28000';
    end if;
    if p_room_id is null then
        raise exception 'invalid_room_id' using errcode = '22023';
    end if;

    perform 1
    from public.watch_rooms r
    where r.id = p_room_id
    for update;

    if not found then
        return false;
    end if;

    select m.role into v_role
    from public.watch_room_members m
    where m.room_id = p_room_id
      and m.user_id = v_user
    for update;

    -- Non-members must not be able to remove anyone or probe membership.
    if v_role is null then
        return false;
    end if;

    -- The delete trigger owns host promotion and empty-room expiry so every
    -- removal path behaves the same.
    delete from public.watch_room_members m
    where m.room_id = p_room_id
      and m.user_id = v_user;

    return true;
end;
$$;

-- Administrative/cleanup helper. Safe to call from a scheduled job.
create or replace function public.expire_watch_rooms()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_count integer;
    v_room_id uuid;
begin
    -- Empty rooms are abandoned by definition. Hostless rooms with members are
    -- repaired below instead of being killed mid-session.
    update public.watch_rooms r
    set status = 'expired', ended_at = coalesce(r.ended_at, now())
    where r.status = 'active'
      and (
          r.expires_at <= now()
          or not exists (
              select 1 from public.watch_room_members m
              where m.room_id = r.id
          )
      );
    get diagnostics v_count = row_count;

    for v_room_id in
        select r.id
        from public.watch_rooms r
        where r.status = 'active'
          and exists (
              select 1 from public.watch_room_members m
              where m.room_id = r.id
          )
          and not exists (
              select 1 from public.watch_room_members m
              where m.room_id = r.id
                and m.role = 'host'
          )
        for update
    loop
        perform public.ensure_watch_room_host(v_room_id);
    end loop;

    -- Repair a stale denormalized host pointer, which can happen after a manual
    -- membership repair.
    update public.watch_rooms r
    set host_user_id = h.user_id
    from public.watch_room_members h
    where h.room_id = r.id
      and h.role = 'host'
      and r.status = 'active'
      and r.host_user_id <> h.user_id;

    delete from public.watch_join_attempts a
    where a.attempted_at < now() - interval '1 day';

    -- Expired room codes are globally unique; release them after a retention
    -- window so the six-digit space is not exhausted permanently.
    delete from public.watch_rooms r
    where r.status <> 'active'
      and coalesce(r.ended_at, r.expires_at) < now() - interval '1 day';

    return v_count;
end;
$$;

-- Keep cleanup automatic. pg_cron is optional: local and self-hosted projects
-- without it still get trigger-driven cleanup and the two-hour expiry check on
-- join. Any failure here is a warning, never a migration failure.
do $$
begin
    begin
        create extension if not exists pg_cron;
    exception
        when others then
            raise warning 'pg_cron unavailable, watch room sweep not scheduled: %', sqlerrm;
    end;

    if exists (select 1 from pg_extension where extname = 'pg_cron') then
        begin
            if exists (
                select 1 from cron.job
                where jobname = 'nyoanime-watch-room-expiry'
            ) then
                perform cron.unschedule('nyoanime-watch-room-expiry');
            end if;
            perform cron.schedule(
                'nyoanime-watch-room-expiry',
                '* * * * *',
                'select public.expire_watch_rooms();'
            );
        exception
            when others then
                raise warning 'Could not schedule watch room expiry: %', sqlerrm;
        end;
    end if;
end
$$;

-- ---------------------------------------------------------------------------
-- Privileges. Clients may only read through RLS and mutate through RPCs.
-- ---------------------------------------------------------------------------

revoke all on function public.generate_watch_room_code() from public, anon, authenticated;
revoke all on function public.hash_watch_room_code(text) from public, anon, authenticated;
revoke all on function public.hmac_watch_room_code(text) from public, anon, authenticated;
revoke all on function public.is_watch_room_member(uuid) from public, anon;
revoke all on function public.is_watch_room_host(uuid) from public, anon;
revoke all on function public.watch_room_id_from_topic(text) from public, anon;
revoke all on function public.ensure_watch_room_host(uuid) from public, anon, authenticated;
revoke all on function public.watch_room_member_after_delete() from public, anon, authenticated;
revoke all on function public.create_watch_room(text, text, integer) from public, anon;
revoke all on function public.join_watch_room(text, text) from public, anon;
revoke all on function public.leave_watch_room(uuid) from public, anon;
revoke all on function public.expire_watch_rooms() from public, anon, authenticated;

grant execute on function public.is_watch_room_member(uuid) to authenticated, service_role;
grant execute on function public.is_watch_room_host(uuid) to authenticated, service_role;
grant execute on function public.watch_room_id_from_topic(text) to authenticated, service_role;
grant execute on function public.create_watch_room(text, text, integer) to authenticated, service_role;
grant execute on function public.join_watch_room(text, text) to authenticated, service_role;
grant execute on function public.leave_watch_room(uuid) to authenticated, service_role;
grant execute on function public.expire_watch_rooms() to service_role;

-- Clients read through RLS only; every mutation goes through the RPCs above.
revoke all on table public.watch_rooms from anon, authenticated;
revoke all on table public.watch_room_members from anon, authenticated;
revoke all on table public.watch_join_attempts from anon, authenticated;
grant select on table public.watch_rooms to authenticated;
grant select on table public.watch_room_members to authenticated;

-- ---------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------

alter table public.watch_rooms enable row level security;
alter table public.watch_room_members enable row level security;
alter table public.watch_join_attempts enable row level security;

-- Members may read the room they belong to. Non-members see nothing.
drop policy if exists watch_together_rooms_select_members on public.watch_rooms;
create policy watch_together_rooms_select_members
on public.watch_rooms
for select
to authenticated
using (public.is_watch_room_member(id));

-- Members may read the membership list of their own room.
drop policy if exists watch_together_members_select_members on public.watch_room_members;
create policy watch_together_members_select_members
on public.watch_room_members
for select
to authenticated
using (public.is_watch_room_member(room_id));

-- No client INSERT/UPDATE/DELETE policies: all writes go through the
-- SECURITY DEFINER RPCs above. Service role retains its normal bypass.
-- watch_join_attempts intentionally has RLS with no policies at all.

-- ---------------------------------------------------------------------------
-- Realtime Authorization
--
-- Supabase manages RLS on realtime.messages; do not enable/disable it here.
-- Private channels must also be enabled in the project's Realtime settings.
--
-- Topic:
--   watch-room:<room_id> -> chat, presence and authoritative playback events
--
-- The base topic is the only topic new clients use. The legacy :members and
-- :host topics remain authorized so an older client can finish an active room.
-- ---------------------------------------------------------------------------

drop policy if exists watch_together_realtime_receive on realtime.messages;
create policy watch_together_realtime_receive
on realtime.messages
for select
to authenticated
using (
    public.is_watch_room_member(
        public.watch_room_id_from_topic(realtime.topic())
    )
);

-- Any member may write to the topic. Realtime authorizes a broadcast by
-- inserting a probe row that only carries (topic, extension); the event name is
-- NULL at check time, so an event-name predicate here would deny every send.
-- Host-only gating for playback lives in the client and in
-- set_watch_room_playback (migration 0011), which verifies the caller is host.
drop policy if exists watch_together_realtime_room_send on realtime.messages;
create policy watch_together_realtime_room_send
on realtime.messages
for insert
to authenticated
with check (
    realtime.topic() ~ '^watch-room:[0-9a-fA-F-]{36}$'
    and public.is_watch_room_member(
        public.watch_room_id_from_topic(realtime.topic())
    )
);

-- Legacy split topics. The members topic never carries authoritative playback.
drop policy if exists watch_together_realtime_members_send on realtime.messages;
create policy watch_together_realtime_members_send
on realtime.messages
for insert
to authenticated
with check (
    realtime.topic() ~ '^watch-room:[0-9a-fA-F-]{36}:members$'
    and public.is_watch_room_member(
        public.watch_room_id_from_topic(realtime.topic())
    )
    and coalesce(realtime.messages.event, '') not in (
        'play', 'pause', 'seek', 'sync_playback', 'episode_changed',
        'room_state', 'end_room', 'lock_room', 'kick'
    )
);

drop policy if exists watch_together_realtime_host_send on realtime.messages;
create policy watch_together_realtime_host_send
on realtime.messages
for insert
to authenticated
with check (
    realtime.topic() ~ '^watch-room:[0-9a-fA-F-]{36}:host$'
    and public.is_watch_room_host(
        public.watch_room_id_from_topic(realtime.topic())
    )
);

commit;
