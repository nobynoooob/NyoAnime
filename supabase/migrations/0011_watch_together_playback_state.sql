-- Repair Realtime broadcast writes and persist authoritative playback state.
--
-- Two independent problems motivated this migration:
--
-- 1. The base topic `watch-room:<uuid>` could be joined but not written to.
--    Realtime authorizes a broadcast by inserting a probe row with only
--    (topic, extension); `realtime.messages.event` is NULL during the check.
--    A host-only predicate built on that column therefore evaluates to NULL
--    and denies every send. The legacy `:members` / `:host` topics already
--    proved the membership predicate itself works, so the base topic uses the
--    same predicate. Event-level host gating stays in the client and in
--    `set_watch_room_playback`, which is the only place playback state is
--    persisted and always verifies the caller is the current host.
--
-- 2. Playback needs a source of truth that does not depend on a broadcast
--    arriving. Guests poll `watch_rooms` (already member-readable) and recover
--    the latest episode even when Realtime drops a frame.

begin;

-- ---------------------------------------------------------------------------
-- Base-topic broadcast authorization
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- Authoritative playback state
-- ---------------------------------------------------------------------------

alter table public.watch_rooms
    add column if not exists playback jsonb,
    add column if not exists playback_reference jsonb,
    add column if not exists playback_sender uuid,
    add column if not exists playback_seq bigint not null default 0,
    add column if not exists playback_updated_at timestamptz;

create or replace function public.set_watch_room_playback(
    p_room_id uuid,
    p_playback jsonb,
    p_reference jsonb default null
)
returns table (
    playback_seq bigint,
    playback_updated_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_user uuid := auth.uid();
    v_headers jsonb;
    v_playback jsonb := p_playback;
    v_reference jsonb := p_reference;
    v_seq bigint;
    v_now timestamptz := now();
begin
    if v_user is null then
        raise exception 'not_authenticated' using errcode = '28000';
    end if;

    if v_playback is null or jsonb_typeof(v_playback) <> 'object' then
        raise exception 'invalid_playback' using errcode = '22023';
    end if;

    if not exists (
        select 1
        from public.watch_rooms r
        where r.id = p_room_id
          and r.status = 'active'
          and r.expires_at > now()
    ) then
        raise exception 'room_not_found' using errcode = 'P0002';
    end if;

    if not public.is_watch_room_host(p_room_id) then
        raise exception 'forbidden' using errcode = '42501';
    end if;

    if coalesce(v_playback ->> 'url', '') !~ '^https?://' then
        raise exception 'invalid_playback' using errcode = '22023';
    end if;

    -- A modified client could otherwise persist credentials that every guest
    -- then receives through the poll. Strip them server-side too.
    if jsonb_typeof(v_playback -> 'headers') = 'object' then
        select coalesce(jsonb_object_agg(key, value), '{}'::jsonb)
        into v_headers
        from jsonb_each(v_playback -> 'headers')
        where lower(key) not in (
            'cookie', 'set-cookie', 'authorization', 'proxy-authorization'
        );
        v_playback := jsonb_set(v_playback, '{headers}', v_headers, true);
    end if;

    if v_reference is not null and jsonb_typeof(v_reference) <> 'object' then
        v_reference := null;
    end if;

    if length(v_playback::text) > 65536
       or (v_reference is not null and length(v_reference::text) > 8192) then
        raise exception 'payload_too_large' using errcode = '22001';
    end if;

    update public.watch_rooms r
    set playback = v_playback,
        playback_reference = v_reference,
        playback_sender = v_user,
        playback_seq = r.playback_seq + 1,
        playback_updated_at = v_now
    where r.id = p_room_id
    returning r.playback_seq, r.playback_updated_at into v_seq, v_now;

    if v_seq is null then
        raise exception 'room_not_found' using errcode = 'P0002';
    end if;

    return query select v_seq, v_now;
end;
$$;

revoke all on function public.set_watch_room_playback(uuid, jsonb, jsonb)
    from public, anon;
grant execute on function public.set_watch_room_playback(uuid, jsonb, jsonb)
    to authenticated, service_role;

commit;
