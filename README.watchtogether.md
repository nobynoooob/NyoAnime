# Watch Together

## Overview

Watch Together enables synchronized, collaborative anime viewing across local networks or the internet through a lightweight WebSocket relay server. This component adds multiplayer support to NyoAnime.

## Features

- **Room creation**: Host a watch party and share an invitation code.
- **Guest joining**: Join an existing room with a code.
- **Synchronized playback**: Host playback state is reflected in guest players via accurate position tracking and drift compensation.
- **Per-client stream offset**: Guests on a different version (for example a dub with an extra intro) can nudge their own timeline with `[` / `]` and reset with `0`; the last offset is remembered per provider and language.
- **Chat and reactions**: Optional room chat with nicknames.
- **Ready management**: Guests report readiness; host can start when everyone is ready.
- **Provider compatibility**: Guests resolve anime locally through existing NyoAnime providers; no credentials or cookies are relayed.
- **Secure and private**: Invitation codes are random, session tokens are temporary, and the relay never stores media or user credentials.
- **Cross-platform**: Works on Linux, macOS, Windows, and Android Termux.
- **Configurable**: Supports local network, public WSS, and Docker deployment.
- **Player-agnostic**: Tested with MPV; supports VLC/Celluloid/IINA/Syncplay where synchronization is possible.

## Architecture

NyoAnime v2.3 includes three primary subsystems:

1. **NyoAnime client** (`nyoanime/`): User interface, provider integration, download engine, and player controller.
2. **Watch Together room server** (`party_server/`): Lightweight async WebSocket relay managing rooms, participants, and message routing.
3. **Local MPV controller** (`nyoanime/party/mpv_controller.py`): Real-time player interaction via MPV's JSON IPC.

Communication between client and relay happens exclusively over WebSockets (ws:// or wss://). The relay never exposes MPV sockets; it only forwards high-level synchronization messages.

## Quick Start (Local Network)

1. Install NyoAnime (ensure `websockets` is available; the installer will auto-install it):

```bash
pip install --break-system-packages -e .[watchtogether]
```

2. Launch the relay server (one-time setup). Save the generated room code and server address.

```bash
# From repository root
python -m party_server.server
```

3. In another terminal or on another machine, join the room:

```bash
python -m nyoanime.cli join-room <room-code> -s ws://localhost:8765
```

4. Alternatively, start a Watch Together session from the TUI menu (`W`):

- **Create Room**: Generates a code and launches a local lobby.
- **Join Room**: Prompts for a code and server address.

## Remote Internet Setup

To allow guests to connect over the internet, expose the relay server publicly via TLS (e.g., using Nginx, Caddy, or a cloud proxy). Use `wss://` for guests.

Example minimal reverse proxy with HTTPS:

```nginx
server {
    listen 443 ssl;
    server_name watch-together.example.com;

    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://localhost:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Clients use `ws://watch-together.example.com:443` (or `wss://...` with TLS verification).

## Room lifecycle

### Host (creator)

- Generates a cryptographically random room code (e.g., `NYO-8XK92`).
- Creates a room entry with a unique host token.
- Advertises the code in the lobby.
- Selects anime and episode via the same NyoAnime UI.
- Publishes playback state (episode, position, paused, speed, etc.) to the relay.
- Receives readiness reports and guest sync state.

### Guest (joiner)

- Enters invitation code.
- Authenticate with a temporary nickname (or reuse a remembered one).
- The relay validates the code, admits the guest, and sends a session token.
- The guest receives current room state (anime, episode, position, paused) via the relay.
- Guest resolves the anime locally using NyoAnime providers (preferring the host's provider when possible).
- Guest reports readiness to the relay.
- Guest playback is synchronized to host's timeline (position correction, pause state).

## Provider and episode resolution

Because providers may differ per user and anime IDs are provider-specific, the system uses structured watch-party references (`WatchPartyAnimeReference`).

### Reference fields

- **title**: Original title.
- **aliases**: Alternative titles (e.g., Japanese, romanized).
- **provider_id**: Canonical provider identifier (`anime3rb`, `hianime`, `stardima`, etc.).
- **provider_anime_id**: Provider-specific anime identifier.
- **episode_number**: Episode to watch.
- **language_mode**: One of `ar_sub`, `en_sub`, `en_dub`.

### Resolution algorithm

When a guest joins an active room:

1. **Exact provider match**: If the guest has the same provider as the host's reference, resolve using that provider's anime ID.
2. **Fallback search**: If not available, search the guest's provider list for a matching anime using title/aliases.
3. **Manual override**: If resolution fails, show an error and keep the guest in the lobby; host can manually reselect.

Never silently replace a host's chosen anime with an unrelated title.

## Player control and IPC

MPV is the primary synchronization engine. A local `MpvController` uses MPV's JSON IPC (`--input-ipc-server`) to observe state (time-pos, duration, pause) and issue commands (play, pause, seek).

Critical security:

- The MPV socket is local-only; it never leaves the machine.
- The relay only forwards application-level commands (play, pause, seek, switch episode).
- Unsafe commands (e.g., arbitrary shell commands) are explicitly rejected.

If MPV is unavailable, synchronization is degraded: host can still publish state, but guests will not receive precise position updates and will fall back to less accurate playback control.

Other players (VLC, Celluloid, IINA, Syncplay) are listed but only participate where their capabilities match the synchronization requirements (VLC: referer and audio/subtitle slave; Celluloid/IINA/Syncplay: MPV via command line). Guests will be informed if a player lacks full Watch Together support.

## Synchronization protocol

The relay transmits typed events:

```json
{
  "type": "sync",          // or "chat", "ready", "join", "leave", "kick", "lock", "end"
  "room": "room-code",
  "sender": "host-id",
  "target": "guest-id",    // optional for direct commands
  "seq": 12345,            // event sequence number
  "ts": 1731504000000,    // server timestamp (ms)
  "data": {                // event-specific payload
    "episode": "Naruto",
    "episode_number": 1,
    "provider_id": "hianime",
    "provider_anime_id": "1335",
    "language_mode": "en_sub",
    "position": 120.5,
    "paused": false,
    "speed": 1.0,
    "host_token": "xyz"
  }
}
```

The host is authoritative; guests never send playback control events except for volume and local subtitle selections.

Each guest can keep a local `stream_time_offset`, added to the host timeline when that guest seeks. `[` subtracts 0.5s, `]` adds 0.5s, and `0` resets it; MPV shows a short OSD confirmation. The host timeline itself is never modified by a guest's offset. Offsets are stored in `~/.nyoanime/database/stream_offsets.json`, keyed by canonical provider id and language mode, and reapplied when the same provider is used again.

The relay deduplicates, orders, and forwards messages; it does not store unnecessary history beyond active rooms.

## Chat

Room chat messages are relayed with timestamps and nicknames. Messages are sanitized to avoid terminal escape sequence injection. Chat history is kept per-room for the duration of the room (typically ~10 minutes).

## Reconnection and failure recovery

- **Relay disconnect**: Guests attempt reconnection with exponential backoff, presenting a reconnection form.
- **Host disconnect**: If the host reconnects within the grace period, the room continues; otherwise, the room ends and guests return to the lobby.
- **Player crash**: MPV controller detects process death and publishes an error; guests are notified.
- **Network jitter**: Synchronization algorithm compensates for latency and drift using host heartbeats (~2 seconds).

## Security and privacy

- Invitation codes: 5-character alphanumeric, case-insensitive, excluding ambiguous characters (e.g., I, O, L, 1, 0).
- Temporary participant tokens are short-lived, tied to room lifetime.
- Host ownership is protected by a separate host token (presented in the lobby).
- TLS is mandatory for internet deployment; localhost may use plain ws.
- No user registration, no persistent accounts.
- No credential sharing; providers handle authentication locally.
- All sensitive data (tokens, codes, headers) is encrypted in transit and never logged.

## Configuration

- **Room limits**: max participants, room lifetime, grace period, host token expiration.
- **Provider whitelist**: optional restriction on which providers can be used in rooms.
- **Protocol version**: protocol version is negotiated between client and server; mismatched versions prevent joining.
- **CORS/proxy**: When behind a reverse proxy, set `X-Forwarded-For` and respect `Host` headers.
- **Docker**: see `docker-compose.yml` (if present) for an example setup.

## Docker deployment

```bash
# In a folder with docker-compose.yml and a local copy of the NyoAnime source
cd nyoanime
./scripts/setup.sh  # optional: builds deps and installs npm assets if any
```

Sample `docker-compose.yml`:

```yaml
version: '3.8'
services:
  relay:
    build: ./party_server
    ports:
      - "8765:8765"
    volumes:
      - ./party_server:/app/party_server
    environment:
      - ROOM_MAX_PARTICIPANTS=8
      - ROOM_EXPIRATION_MINUTES=10
      - HOST_RECONNECTION_GRACE=60
```

Guests connect using `ws://host.docker.internal:8765` (or the container's public IP).

## Dependencies

Required packages (installed automatically by the NyoAnime dependency installer):

- `websockets>=12,<16`
- `asyncio`, `uuid`, `secrets`, `hashlib` (standard library)
- `aiohttp` is optional for HTTP health checks (not required for core relay).

Ensure you have Python >=3.9.

## Testing

Integration tests (`tests/test_party*.py`) simulate two clients and a relay, verifying host/guest synchronization, reconnection, and error handling.

Run:

```bash
pytest -xvs tests/test_party*
```

## Troubleshooting

### Relay unavailable

Check that the relay server is running and the address (`ws://host:port`) is correct.

### Invalid room code

Ensure no typos; codes are case-insensitive but copy-paste is recommended.

### Guest cannot resolve anime

If the host's provider cannot resolve locally, try switching provider in the lobby.

### Poor synchronization

Ensure all participants use the same player with IPC enabled. If using VLC, enable `--input-slave=audio#sub` as required.

### Chat corruption

Chat sanitization is applied; if you see strange characters, the terminal font may not support Unicode. Use a modern terminal (e
   

---

See also [NyoAnime README](README.md) for general usage and configuration.
