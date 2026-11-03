# NyoAnime v2.3.0 — Watch Together (WTR) Specification

## Overview
- **Purpose:** Enable synchronized, collaborative anime viewing across multiple participants.
- **Architecture:** Three core subsystems – **NyoAnime client** (existing UI + new WTR modules), **Watch Together relay server** (stateless WebSocket relay), **Local MPV controller** (MPV IPC wrapper with allowlist).
- **Assumptions:** Only MPV provides reliable playback position; other players may join but without position tracking.
- **Design Principles:** 
  - No media or authentication data passes through the relay.
  - Provider resolution is local; the relay only routes high‑level events.
  - Security via cryptographic room codes and participant tokens.
  - Backward compatible – existing Arabic/English providers and downloads are untouched.

## 1. Client (`nyoanime/party/client.py`)

### Responsibilities
- Connect to a configured relay server (ws:// or wss://).
- Manage authentication tokens (room code, participant token).
- Emit events (create/join/leave room, sync playback, chat, ready).
- Receive events and dispatch to UI state.
- Handle reconnection, heartbeat, and error recovery.

### Public API (pseudo)
```python
class WTRClient:
    def __init__(self, relay_url: str, nickname: str, room_code: Optional[str] = None, host_token: Optional[str] = None)
    async def connect(self) -> None
    async def disconnect(self) -> None
    async def create_room(self) -> str  # returns room code
    async def join_room(self, room_code: str) -> None
    async def leave_room(self) -> None
    async def update_playback(self, state: PlaybackState) -> None
    async def send_chat(self, text: str) -> None
    async def set_ready(self, ready: bool) -> None
    def get_room_info(self) -> Optional[RoomInfo]
    def get_playback_state(self) -> Optional[PlaybackState]
    def is_ready(self) -> bool
    def on(event: str, callback) -> None
```

### Message Flow (simplified)
- Client ↔ relay via JSON WebSocket frames.
- All messages include `type`, `seq`, `room_id`, `sender`, `timestamp_ms`.
- Relay validates schema and forwards to appropriate recipients.

## 2. Protocol (`nyoanime/party/protocol.py`)

### Versioning
- `protocol_version`: integer, starting with `1`.  
- Compatible versions allow cross‑version clients.

### Core Message Structure
```json
{
  "protocol_version": 1,
  "type": "SYNC_PLAYBACK",  // see enum
  "room_id": "NYO-8XK92",
  "sender": "participant-uuid",
  "seq": 42,
  "timestamp_ms": 1731506780000,
  "payload": {
    "position": 120.5,
    "paused": false,
    "speed": 1.0,
    "anime": {"title": "Naruto", "provider_id": "hianime", "anime_id": "1335", "episode": "1", "language_mode": "en_sub"},
    "host": true
  }
}
```

### Message Types (Enum)
- `CREATE_ROOM` – host initiates a new room.
- `JOIN_ROOM` – guest joins using room code.
- `LEAVE_ROOM`
- `PARTICIPANT_JOINED` – notification that a participant has joined.
- `PARTICIPANT_LEFT`
- `SYNC_PLAYBACK` – host authoritative playback state (position, paused, speed, episode, anime reference).
- `READY` – guest reports ready (buffering complete).
- `BUFFERING` – guest reports buffering progress.
- `CHAT` – text message.
- `KICK` – host removes a guest.
- `ROOM_LOCK` / `ROOM_UNLOCK` – toggle room lock.
- `ROOM_STATE` – snapshot of current room (participants, playback, settings).
- `END_ROOM` – host ends the room.
- `ERROR` – error from relay (invalid schema, room full, etc.).
- `PING`, `PONG` – heartbeat.

### Payload Schemas (summary)
- `AnimeReference`: `title`, `provider_id`, `anime_id`, `episode`, `language_mode`.
- `PlaybackState`: `position`, `duration`, `paused`, `speed`, `anime_reference`, `host`, `sequence`.
- `Chat`: `content`.

### Security
- All fields are JSON strings/numbers; extra fields ignored.
- Max payload size: 64 KB.
- Schema validation on receive; reject malformed.
- Messages are signed with HMAC‑SHA256 using a shared secret per room? *No* – relay does not sign; authenticity comes from encrypted WebSocket transport (TLS). The room code + participant token is transmitted with each message to let the relay identify the sender.

## 3. Room Management (`nyoanime/party/rooms.py`)

### Data Models
```python
@dataclass
class Participant:
    id: str                     # UUID
    nickname: str
    role: Literal["host", "guest"]
    token: str                  # random secret for reconnection
    ready: bool = False
    connected: bool = True
    last_heartbeat: int        # server timestamp

@dataclass
class RoomSettings:
    max_participants: int = 8
    room_expiration_ms: int = 10 * 60 * 1000   # 10 min
    host_reconnection_grace_ms: int = 60_000    # 60 s
    locked: bool = False
    language_mode: str = "ar_sub"   # not used – each host decides

@dataclass
class Room:
    id: str                     # room code
    host_id: str
    settings: RoomSettings
    participants: Dict[str, Participant]
    current_playback: Optional[PlaybackState]
    created_at: int
    last_activity: int
```

### Operations
- `create_room(host_id, settings) → Room`
- `join_room(room_id, participant) → Participant`
- `leave_room(room_id, participant_id)`
- `update_participant(…, ready?, connected?)`
- `set_room_state(room_id, key, value)` (for lock, playback, etc.)
- `cleanup_expired_rooms()`

### Token Management
- Room code is case‑insensitive alphanumeric (`A‑Z`, `0‑9`) without ambiguous chars (I,O,L,1,0). 20‑bit entropy → ~1 million possibilities.
- Participant token is a random base64 string (`secrets.token_urlsafe(32)`). Used for reconnection.
- Host token (separate) is optional; if present, host can reconnect without rejoining.

## 4. Relay Server (`party_server/server.py`)

### Architecture
- Single process asyncio server using `websockets` library.
- Each room lives in memory; no persistence across restarts.
- Supports both `ws://` (local) and `wss://` (TLS) – TLS termination handled by external reverse proxy.
- `MAX_MESSAGE_SIZE = 65536`.
- `MAX_CONCURRENT_ROOMS` unlimited (bounded by memory).
- `RATE_LIMIT`: 20 messages/second per client, 100 messages/second per room.

### Core Functions
```python
async def handle_client(websocket, path):
    # authenticate via query parameters or upgrade request (optional)
    # assign participant token
    while True:
        raw = await websocket.recv()
        msg = json.loads(raw)
        validate(msg)
        await route_message(msg, websocket)
```

### Routing Logic
- Verify `room_id` exists and client (`sender`) is authorized.
- Enforce permissions: host‑only for `KICK`, `END_ROOM`, `LOCK`.
- Broadcast to all participants **except** sender for non‑user events (e.g., `PARTICIPANT_JOINED`).
- For reliable ordering, preserve per‑room sequence counters.

### Error Handling
- Send `ERROR` message with `code` and `description`.
- Close connection on repeated protocol violations.

## 5. MPV Controller (`nyoanime/party/mpv_controller.py`)

### Design Goals
- Isolate MPV IPC usage from the rest שׁ existing app.
- Provide a simple API for playback control.
- Ensure safety – only allow host‑authorized commands.

### API
```python
class MPVController:
    def __init__(self, session_id: str, ipc_path: Optional[str] = None):
        # If bundled mpv is present, launch it; otherwise assume mpv on PATH.
        # Create MpvTracker instance for position updates.

    async def play(self) -> None
    async def pause(self) -> None
    async def seek(self, position: float) -> None
    async def set_speed(self, speed: float) -> None
    def get_position(self) -> float
    def get_duration(self) -> float
    def is_paused(self) -> bool
    def get_state(self) -> PlaybackState
    def close(self) -> None
```

### Interaction with Existing NyoAnime
- Use the existing `PlayerManager._play_mpv` logic – the MPVController will reuse the same argument building.
- The controller is only used when Watch Together is active; the main app can still launch players via `PlayerManager` for non‑WTR flows.

### Security
- IPC socket path is unique per session (`ani_mpv_<pid>_<timestamp>`).
- No network exposure – socket is a Unix domain socket (or Windows named pipe).
- Commands are sent via the JSON IPC; the controller validates that only host‑authorized sessions may issue playback commands (host token verification outside IPC).

## 6. Synchronization Engine (`nyoanime/party/sync.py`)

### Host Responsibilities
- Host selects anime/episode via existing NyoAnime UI.
- Host publishes `SYNC_PLAYBACK` events at a fixed interval (2 s) with latest playback state.
- Host can change episode; guests receive `EPISODE_CHANGED` (or a new `SYNC_PLAYBACK` with updated reference).

### Guest Processing
- Guest maintains a local copy of host’s `PlaybackState`.
- On each received `SYNC_PLAYBACK`:
  - Compute drift = `host_position - local_position`.
  - Apply correction:
    - If drift < 0.5 s → no action.
    - 0.5 s ≤ drift ≤ 1.5 s → adjust playback speed (if supported) for a brief period.
    - >1.5 s → absolute seek.
  - Update local `paused` flag (immediate).
  - If episode reference changed → trigger local provider resolution (see §7). If resolution fails, show error and keep current episode.

### Buffering & Readiness
- Guest reports `BUFFERING` after stream load; host tracks readiness.
- Host can start playback after all guests are `READY` or immediately.
- If a guest disconnects mid‑stream, others continue.

### Reconnection
- Guest reconnects with its participant token; relay validates and re‑injects participant.
- On reconnection, guest requests latest `ROOM_STATE` snapshot.
- Synchronization recalculates drift based on new connection latency.

## 7. Provider Integration & Episode Resolution

### Anime Reference (`WatchPartyAnimeReference`)
```python
@dataclass
class WatchPartyAnimeReference:
    title: str
    provider_id: str
    anime_id: str
    episode: str
    language_mode: str   # "ar_sub" | "en_sub" | "en_dub"
    aliases: List[str] = field(default_factory=list)
```

### Resolution Logic (in client)
1. If `provider_id` matches client’s active provider **and** `anime_id` exists locally → direct match.
2. Search local provider list for an anime with matching title/aliases. If found, use provider‑specific ID.
3. If still unresolved, fallback to provider that supports the `language_mode`.
4. Present “Resolution failed” error in lobby – host may reselect.

### Host’s Media Selection
- Host selects via existing NyoAnime quality menu → resolved `DownloadPlan` → extract `master_url`, `audio_tracks`, `subtitle_url`, `audio_lang`.
- These fields populate the `WatchPartyAnimeReference` (e.g., `provider_id` from provider manager, `anime_id` from provider’s internal ID).

## 8. TUI Integration (`nyoanime/ui.py` & `nyoanime/app.py`)

### Home Menu Extension
- Add a new option “Watch Together” (`W`) to the existing home screen (`ui.home_screen_menu`).
- Upon selection, push a Watch Together state machine into the TUI.

### Watch Together Screens
1. **Create Room**
   - Prompt for nickname.
   - Connect to configured relay.
   - Call `client.create_room()`.
   - Display room code, server address, and QR‑like copy button.
   - Enter lobby.

2. **Join Room**
   - Prompt for room code and nickname.
   - Call `client.join_room(room_code)`.
   - If successful, transition to lobby.

3. **Lobby**
   - Title: “NYOANIME WATCH TOGETHER – {room_code}"
   - Participant list with role badges (HOST/READY).
   - Current anime/episode if host has selected, else “Waiting for host..."
   - Chat preview area (max 20 messages).
   - Actions: Copy code, Leave Room, Chat, Mark Ready, Transfer Host, End Room (host only), Lock Room (host only).
   - Refresh on new events.

4. **Active Watch Party**
   - Full‑screen playback indicator (MPV window remains visible via MPV).
   - Right‑panel shows room info, participant list, chat.
   - Host controls: episode selector, play/pause, seek (buttons or keyboard).
   - Guest controls: volume, subtitle track, local settings.
   - Synchronization status bar (e.g., “Sync OK – 0.2 s drift”).

### Keyboard Shortcuts (summary)
- `C` – Create Room
- `J` – Join Room
- `L` – Leave Room
- `P` – Toggle participant list
- `M` – Toggle chat
- `R` – Mark Ready / Unready
- `Q` – Return to previous menu
- `ESC` – Close modal / Cancel

### UI Theme
- Preserve existing color palette (cyan highlights, purple borders).
- Add language badges for `language_mode`.
- Use same box drawing characters (`HEAVY`).

## 9. Integration Points & Hooks

### App Flow
- Modify `NyoAnimeApp.run_tui_mode` to push a Watch Together FSM after home menu selection.
- Provide a separate event loop inside `app.py` that processes WTR client events and updates the UI.
- When user leaves a Watch Together room, return to normal home screen.

### Settings
- Add `relay_url` and `nickname` settings in `settings_menu` (persistent across sessions).
- Option to enable/disable Watch Together.

### History
- When host starts a Watch Together session, create a new history entry with a special `provider` value (e.g., `"watchtogether"`) to differentiate from regular watch history.
- Use existing `HistoryManager` – it already supports provider‑qualified keys.

### Favorites
- No changes – existing favorites unaffected.

### Downloads
- No changes – existing download flow unaffected.

## 10. Deployment & Configuration

### Docker Support
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install --break-system-packages -e .[watchtogether]
EXPOSE 8765
CMD ["python", "-m", "party_server.server", "--host", "0.0.0.0", "--port", "8765"]
```

### Environment Variables
- `WTR_RELAY_URL` – default `ws://localhost:8765`
- `WTR_NICKNAME` – default `Guest`
- `WTR_MAX_PARTICIPANTS` – default `8`
- `WTR_ROOM_EXPIRATION_MINUTES` – default `10`
- `WTR_HOST_RECONNECTION_GRACE` – default `60`

### Reverse Proxy Example (Nginx)
```nginx
server {
    listen 443 ssl;
    server_name watchtogether.example.com;
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
    }
}
```

### TLS
- Relay supports `wss://` out‑of‑the‑box if `ssl_context` is provided (use `asyncio.start_server` with SSL).
- Clients should verify TLS certificates; self‑signed certs are acceptable for local deployment.

## 11. Testing

### Unit Tests
- `tests/test_party_client.py` – WebSocket client, reconnection, message parsing.
- `tests/test_party_server.py` – room lifecycle, auth, rate limiting.
- `tests/test_party_sync.py` – synchronization drift calculations, buffering simulation.
- `tests/test_party_protocol.py` – schema validation, sequence ordering.
- `tests/test_party_rooms.py` – participant management, host/guest permissions.

### Integration Test
- Spin up a local relay (asyncio server) and launch a host + two guest clients.
- Simulate host playback (using mock MPV controller) and verify guests receive synchronized state.
- Test chat, ready, lock, kick, host transfer.
- Clean up resources after test.

### Compatibility
- Ensure existing NyoAnime tests still pass (no regression).
- Verify that Watch Together does not affect Arabic, English sub/dub providers.

## 12. Security Considerations

- **Relay Exposure**: Only expose relay behind TLS; never expose raw Unix sockets.
- **Token Leakage**: Tokens are random and expire with room lifetime; compromise of a token only allows joining the same room (no provider credentials).
- **MPV IPC**: Never exposed; controller uses local sockets with restrictive permissions.
- **Message Validation**: Strict schema; reject unknown types.
- **Rate Limiting**: Prevents DoS attacks on relay.
- **Input Sanitization**: Chat messages escaped to avoid terminal escape sequence injection.
- **Audit Logging**: Log room creation, join/leave, and errors for debugging (without sensitive data).

## 13. Known Limitations & Future Work

- Only MPV provides accurate position – other players have degraded sync (paused state only).
- Cross‑provider anime matching relies on title aliases; manual override may be needed.
- No persistent room storage – rooms disappear on relay restart.
- No premium features (custom themes, multiplayer voice).
- Guest cannot directly change subtitle/audio tracks in sync with host – each client controls its own player.
- Host may need to re‑resolve anime if a guest’s provider differs and resolution fails.

---

**End of Specification** – ready for implementation.