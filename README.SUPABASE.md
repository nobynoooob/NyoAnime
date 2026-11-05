# Watch Together on Supabase

This is the deployment and operations guide for the Supabase Realtime backend of
NyoAnime Watch Together. The WebSocket relay in `party_server/` remains a
supported fallback and is used automatically when no publishable key is
configured.

Nothing in this guide has been applied to the remote analytics project. The
backend is **not production-ready until the migration and Edge Functions are
deployed and the remote test suite passes against that deployment**.

## Architecture

| Piece | Purpose |
| --- | --- |
| `supabase/migrations/0010_watch_together.sql` | Tables, constraints, indexes, RPCs, RLS and Realtime authorization |
| `supabase/migrations/0011_watch_together_playback_state.sql` | Repair base-topic writes, persist authoritative playback state |
| `supabase/functions/create-room/` | Verifies the caller JWT, creates a room atomically |
| `supabase/functions/join-room/` | Verifies the caller JWT, joins or reconnects a member |
| `supabase/functions/leave-room/` | Verifies the caller JWT, removes membership, promotes a host |
| `nyoanime/party/supabase_config.py` | Resolves the public URL/key and transport switch |
| `nyoanime/party/local_config.py` | Loads `~/.nyoanime/.env` with private permissions |
| `nyoanime/party/session_storage.py` | Persists the anonymous session with `0600` permissions |
| `nyoanime/party/transport.py` | One private Realtime room channel, presence and payload sanitization |

Identity is always `auth.users.id`. NyoAnime signs in anonymously, so every
participant is a real Supabase authenticated user. No table or function accepts
a user id from a request body.

### Realtime topic

The room uses one **private** channel authorized by table-backed membership:

- `watch-room:<room-uuid>` — chat, ready, buffering, presence and authoritative
  playback (`play`, `pause`, `seek`, `sync_playback`, `episode_changed`,
  `room_state`, `end_room`, `lock_room`, `kick`). Every member receives and may
  write; the client refuses to send host events unless its local role is
  `host`.

The insert policy is membership-only. Realtime authorizes a broadcast by
inserting a probe row carrying only `(topic, extension)`, so
`realtime.messages.event` is NULL during the check and an event-name predicate
there denies every send. Host-only gating therefore lives in the client and in
`set_watch_room_playback(...)`, which verifies `is_watch_room_host(...)` before
persisting. `0011` repairs the base-topic policy for projects deployed with the
older, event-based predicate.

Playback is also persisted on `watch_rooms.playback`. Guests poll it every two
seconds and synthesize a missed `episode_changed`, so a rejected or dropped
broadcast no longer strands the room. Persisted headers are stripped of
`cookie`, `set-cookie`, `authorization` and `proxy-authorization` server-side.

Legacy `watch-room:<room-uuid>:members` and `:host` topics remain authorized
for clients that started a room before an upgrade, but new clients only
subscribe to the base topic.

The six-digit room code is only a lookup secret. It is never used as the
Realtime topic. The Edge Function returns the room UUID after create/join.

## Prerequisites

- Supabase CLI installed and authenticated.
- A Supabase project. The target project for NyoAnime is
  `ani-cli-ar-analytics` (`mxacanxpjkqeudlwzhwr`).
- Anonymous sign-ins enabled.
- Private Realtime channels enabled.
- For local type checking only: Deno and the `supabase` Python package.

Do not expose the service-role key, database password, JWT secret or any
`sb_secret_...` key to the desktop client. Only the project URL and the
publishable/anon key are public.

## 1. Review the migration before applying it

The migration is additive and only touches the `watch_*` tables, the
`watch-room:*` Realtime policies and the `watch_*` / `is_watch_*` helper
functions. It does not alter or drop analytics tables, policies, functions or
data.

```bash
git diff -- supabase/migrations/0010_watch_together.sql \
           supabase/migrations/0011_watch_together_playback_state.sql
```

The files must contain all of the following before deployment:

- `host_user_id uuid references auth.users (id)` and
  `user_id uuid references auth.users (id)`.
- `watch_room_members_single_host` unique partial index.
- `create_watch_room`, `join_watch_room`, `leave_watch_room` and
  `expire_watch_rooms` as `security definer` functions with
  `set search_path = ''`.
- `watch_room_member_after_delete` as an `after delete` trigger on
  `watch_room_members`, plus `ensure_watch_room_host` for atomic promotion.
- A guarded `pg_cron` schedule for `expire_watch_rooms()`.
- RLS enabled on `watch_rooms`, `watch_room_members` and
  `watch_join_attempts`.
- Realtime policies using `realtime.topic()` and the single-argument
  `is_watch_room_member(room_id)` / `is_watch_room_host(room_id)` helpers. The
  insert policy must not reference `realtime.messages.event`.
- `set_watch_room_playback(...)` as a host-only `security definer` RPC that
  strips credential headers and increments `playback_seq`.
- No `using (true)` or `with check (true)` policy.

The membership helpers deliberately take only a room id. They derive the caller
from `auth.uid()` and cannot be used by an authenticated client to probe another
user's membership or host status.

## 2. Create the room-code HMAC secret in Vault

New rooms store `hmac-sha256:<hex>` instead of an unsalted SHA-256 hash. The
HMAC key must exist before the first room is created; if it is missing, both
`create_watch_room` and `join_watch_room` return `room_code_secret_missing`
(HTTP 503) rather than silently falling back to the weak hash.

Run this once in the Supabase SQL editor as a project administrator:

```bash
openssl rand -hex 32
```

```sql
select vault.create_secret(
  '<paste the 64-character hex value here>',
  'watch_room_code_hmac_key',
  'NyoAnime Watch Together room-code HMAC key'
);
```

Verify it without printing the secret:

```sql
select name, length(decrypted_secret) as secret_length
from vault.decrypted_secrets
where name = 'watch_room_code_hmac_key';
```

Expected: one row with `secret_length = 64`.

Do **not** put this value in the repository, `.env` files shipped with the app,
Edge Function secrets exposed to clients, or logs. Only the SQL function reads
it through `vault.decrypted_secrets`; the Python client never sees it.

Rotating this secret invalidates codes for rooms created with the previous key
because the HMAC changes. Let active rooms expire first (two hours), then rotate
and recreate the secret under the same name.

Compatibility: rooms created before this change keep their legacy SHA-256 hash
in `watch_room_code_hash` and remain joinable, because `join_watch_room` looks
up both the HMAC and the legacy hash. Existing codes cannot be re-hashed into
HMAC because the plaintext code is not stored. They expire after two hours and
are deleted by `expire_watch_rooms` after one day; no manual row migration is
needed. Once the Vault secret exists, all newly created rooms use HMAC.

## 3. Link the project (review only, do not push yet)

```bash
supabase link --project-ref mxacanxpjkqeudlwzhwr
```

Then apply the migration when you are authorized to change the remote project:

```bash
supabase db push
```

`supabase db push` is intentionally **not** part of the repair. Confirm the
project has a backup or that the Watch Together tables do not already exist in
an incompatible shape. The migration uses `create table if not exists`; if a
previous broken deployment created `user_id text` or `host_user_id text`, the
old table must be inspected and migrated deliberately instead of relying on
`if not exists`.

The analytics project has migration history that this repository does not
contain. Run `supabase migration list` first. `db push` may refuse because the
remote has migrations unknown locally; resolving that history is an explicit
operator decision, not something to paper over with `--include-all`.

## 4. Enable Anonymous Auth

Dashboard → **Authentication** → **Providers** → **Anonymous sign-ins** →
enable it.

Without this, `auth.sign_in_anonymously()` fails and the client refuses to use
the Supabase transport. Anonymous users receive the `authenticated` role, which
is what the RPC grants and Realtime policies expect.

Do not use `service_role` for anonymous clients. Do not disable JWT
verification on the Edge Functions.

## 5. Enable private Realtime channels

Dashboard → **Realtime** → **Settings** → enable **Allow private channels**
(the exact label depends on dashboard version). Also confirm Realtime is
enabled for the project.

The Python client subscribes with:

```python
{"config": {"private": True, ...}}
```

If private channels are disabled, the join push is rejected and the client
raises `channel_subscribe_failed`. That is expected; the WebSocket relay is the
fallback.

## 6. Deploy the Edge Functions

The functions use `npm:@supabase/supabase-js@2`, verify the bearer token with
`client.auth.getUser(token)`, and call the RPCs with the caller's JWT. They use
`SUPABASE_ANON_KEY`; they do not need `SUPABASE_SERVICE_ROLE_KEY`.

```bash
supabase functions deploy create-room
supabase functions deploy join-room
supabase functions deploy leave-room
```

Keep JWT verification enabled (the default). Do not pass `--no-verify-jwt`:
anonymous users still have valid JWTs, so verification works.

`supabase/functions/_shared/` is bundled automatically by the CLI through the
relative imports in each function.

### Secrets

These variables are injected by the Supabase Edge Runtime and need no manual
configuration:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY` (present, but unused by these functions)

If you later add a function that genuinely needs the service role, set it with
the CLI secret store and never return it:

```bash
supabase secrets set SOME_ADMIN_TOKEN=...
```

Do not put `SUPABASE_SERVICE_ROLE_KEY` in Python, `.env` files shipped with the
app, release builds or logs.

## 7. Configure the NyoAnime client

Only two values are needed. Save them once:

```bash
python main.py --setup-supabase
```

The setup flow asks for the project URL (press Enter to keep the current or
default URL) and reads the publishable key with hidden input. It writes:

```text
~/.nyoanime/.env
```

The directory is created with mode `0700` and the file with mode `0600`. The
file is ignored by Git. The key is never echoed, printed in diagnostics or
written to logs. Only `SUPABASE_*`, `NYOANIME_*` and `USE_SUPABASE_REALM` keys
are loaded from this file, so it cannot override `PATH` or other process
variables.

Existing non-empty environment variables still win, which keeps CI, packagers
and temporary overrides working:

```bash
export SUPABASE_URL="https://mxacanxpjkqeudlwzhwr.supabase.co"
export SUPABASE_PUBLISHABLE_KEY="sb_publishable_..."   # or the legacy anon JWT
```

Precedence: explicit client arguments > process environment > `~/.nyoanime/.env`
> `DEFAULT_SUPABASE_URL` / empty publishable-key default.

`SUPABASE_ANON_KEY` is accepted as a fallback name for the same public key.
`SUPABASE_PUBLISHABLE_KEY` is public by design. It is safe to bake into a
release build or set through the environment. `nyoanime/party/supabase_config.py`
also contains `DEFAULT_SUPABASE_URL`; the publishable key is intentionally
empty in the repository. Never put a service-role key in the user config.

Transport selection:

| Configuration | Transport |
| --- | --- |
| URL + publishable key present | Supabase Realtime (default) |
| No publishable key | WebSocket relay fallback |
| `NYOANIME_WATCH_TOGETHER_TRANSPORT=websocket` | WebSocket relay |
| `USE_SUPABASE_REALM=false` (legacy) | WebSocket relay |

The anonymous session is stored at:

```text
~/.nyoanime/database/supabase_session.json
```

The file is written atomically with mode `0600` and refreshed automatically.
Override the path for tests with `NYOANIME_SUPABASE_SESSION_PATH`.

### Running a second instance (Watch Together testing)

Two NyoAnime processes under the same Linux user share one session file by
default. Supabase rotates refresh tokens and uses `auth.uid()` as the room
identity, so sharing a session makes the second process steal the host's token
and reuse the host's `(room_id, user_id)` membership row. Give the guest its own
persistent profile instead:

```bash
# Primary instance (unchanged defaults)
python main.py

# Guest instance: separate directory and, optionally, a distinct nickname
NYOANIME_WATCH_TOGETHER_NAME=Guest \
  python main.py --session-dir ~/.nyoanime/guest
```

Equivalent forms:

```bash
python main.py --session-dir ~/.nyoanime/guest
python main.py --session-path ~/.nyoanime/guest/supabase_session.json
NYOANIME_SESSION_DIR=~/.nyoanime/guest python main.py
NYOANIME_SUPABASE_SESSION_PATH=~/.nyoanime/guest/supabase_session.json python main.py
```

Precedence is `--session-path` / `--session-dir` > `NYOANIME_SUPABASE_SESSION_PATH`
> `NYOANIME_SESSION_DIR/supabase_session.json` > the default path. A missing
session in the guest directory triggers a fresh anonymous sign-in, so the guest
gets a different `auth.users` identity and its own participant row. The primary
session is never read, rewritten or signed out.

When `[i] Details` shows `Connection lost`, `Channel states` and `Last error`
report the real Realtime state (for example `members CHANNEL_ERROR: ... token
has expired`). Tokens, bearer credentials, API keys and JWT-shaped strings are
redacted before rendering or logging.

The client never broadcasts stream URLs, cookies, authorization headers,
service-role keys, MPV IPC paths or temporary playback credentials. Payloads
are sanitized recursively before every send, on both transports.

## 8. Test

Mocked/unit/static checks (no network):

```bash
python -m pytest tests/party -q
```

Full suite:

```bash
python -m pytest -q
```

Optional real integration tests against a **dedicated test project**:

```bash
export NYOANIME_SUPABASE_TEST_URL="https://your-test-project.supabase.co"
export NYOANIME_SUPABASE_TEST_KEY="sb_publishable_..."
# Optional, only for the forced-expiry test:
export NYOANIME_SUPABASE_TEST_SERVICE_KEY="..."
python -m pytest -m remote -q
```

The remote tests create rooms and anonymous users. Never run them against the
analytics project or production data. They verify create/join/duplicate/full,
invalid JWT, unauthorized leave, concurrent joins, host departure/promotion and
forced expiry.

Optional database-backed checks against a disposable PostgreSQL instance:

```bash
export NYOANIME_TEST_DATABASE_URL="postgresql://postgres@/postgres?host=/tmp/pg&port=5432"
python -m pytest tests/party/test_supabase_db.py -q
```

The harness creates a uniquely named database, adds the Supabase roles
(`anon`, `authenticated`, `service_role`) if they are missing, installs minimal
`auth`, `realtime` and `vault` stand-ins, applies the real migration, then drops
the database. Install the optional driver first with `pip install "psycopg[binary]"`.
The connecting role must be able to create databases and roles (`pgcrypto` is
required). It proves membership isolation, Realtime policy enforcement, the
concurrent failed-join rate limit, Vault fail-closed behavior, function grants
and capacity/host promotion.

## Database rules

- Capacity is 2–8 members and is enforced under a room-row lock.
- Expiry is 2 hours; `join_watch_room` marks an expired room and rejects it.
- A membership delete trigger expires the room when the last member leaves and
  promotes the earliest remaining member when the host disappears. Every
  removal path, including a clean `leave-room` call, goes through the trigger.
- `expire_watch_rooms()` also closes empty rooms immediately, repairs hostless
  rooms by promoting a member, and is scheduled every minute with `pg_cron`
  when that extension is available. The migration degrades to a warning if it
  is not.
- Room codes are six digits generated from `gen_random_bytes` and stored as a
  Vault-keyed HMAC (`hmac-sha256:<hex>`). Legacy unsalted SHA-256 rows remain
  joinable until they expire.
- `is_watch_room_member(room_id)` and `is_watch_room_host(room_id)` derive the
  caller from `auth.uid()` and cannot inspect another user's membership.
- Failed joins are recorded in `watch_join_attempts`; ten failures in five
  minutes trigger `rate_limited`. A per-user transaction advisory lock is taken
  before the counter is read, so concurrent failed joins cannot bypass it.
  Successful joins clear the counter.
- Duplicate joins return the existing membership, which is how reconnects work.
- Host departure promotes the earliest remaining member and updates
  `watch_rooms.host_user_id` in the same transaction through the membership
  delete trigger.
- `leave_watch_room` returns `false` for non-members; the Edge Function maps
  that to `403 not_a_member`.
- Cleanup is automatic: the trigger handles immediate departures, and the
  migration schedules `select public.expire_watch_rooms();` every minute with
  `pg_cron` when available. No client-side or manual cleanup script is needed.
  The function is revoked from `anon` and `authenticated`.

## Edge Function API

All endpoints require `Authorization: Bearer <anonymous-or-user JWT>` and a JSON
body.

`POST /functions/v1/create-room`

```json
{"nickname": "Alice", "language_mode": "ar_sub", "maximum_members": 8}
```

`POST /functions/v1/join-room`

```json
{"room_code": "123456", "nickname": "Bob"}
```

`POST /functions/v1/leave-room`

```json
{"room_id": "uuid-returned-by-create-or-join"}
```

Any `user_id`, `host_user_id`, `participant_id` or `token` field in the body is
ignored. Identity comes only from the verified JWT.

Error responses are structured:

```json
{"success": false, "error": {"code": "room_full", "message": "Join rejected: room_full"}}
```

Common statuses: `400` invalid input, `401` missing/invalid JWT, `403`
non-member leave, `404` unknown room, `409` full room, `410` expired room, `429`
rate limit or too many hosted rooms, `503` Vault HMAC secret unavailable.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `503 room_code_secret_missing` | Create `watch_room_code_hmac_key` in Vault with the SQL above |
| `channel_subscribe_failed` | Anonymous auth enabled, private channels enabled, migration applied, JWT still valid |
| Broadcast ack `unauthorized` | Apply `0011`; the old insert policy referenced `realtime.messages.event`, which is NULL in Realtime's write probe |
| `401 invalid_token` | Client signed in, session not expired, `SUPABASE_PUBLISHABLE_KEY` matches the project |
| `404 room_not_found` | Six-digit code, correct project, room not expired |
| `409 room_full` | Capacity includes the host |
| `429 rate_limited` | Ten failed code guesses in five minutes |
| Client silently uses the relay | `SUPABASE_PUBLISHABLE_KEY` is unset or transport was forced to websocket |

## Deployment checklist

- [ ] Migrations (`0010`, `0011`) reviewed; no analytics objects touched.
- [ ] `watch_room_code_hmac_key` created in Vault and verified.
- [ ] `supabase db push` applied by an authorized operator.
- [ ] Anonymous sign-ins enabled.
- [ ] Private Realtime channels enabled.
- [ ] `create-room`, `join-room`, `leave-room` deployed with JWT verification.
- [ ] Publishable key configured in the client; service-role key absent.
- [ ] Local mocked/static suite passes.
- [ ] Optional local SQL suite passes with `NYOANIME_TEST_DATABASE_URL`.
- [ ] Remote suite passes against a dedicated test project.
- [ ] WebSocket fallback still works with `NYOANIME_WATCH_TOGETHER_TRANSPORT=websocket`.
