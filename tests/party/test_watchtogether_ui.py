"""Regression tests for the redesigned Watch Together screens."""
import asyncio
import io

import pytest
from rich.console import Console

from nyoanime.party.client import PartyClientError
from nyoanime.ui import UIManager
from nyoanime.ui_watchtogether import WatchTogetherUI

ROOM_ID = "11111111-1111-4111-8111-111111111111"
PARTICIPANT_ID = "22222222-2222-4222-8222-222222222222"
OTHER_PARTICIPANT_ID = "33333333-3333-4333-8333-333333333333"


def make_console():
    return Console(file=io.StringIO(), width=100, force_terminal=False)


class FakeClient:
    def __init__(
        self,
        role="host",
        room_code="123456",
        room_id=ROOM_ID,
        channels_ready=True,
        channel_error=None,
        nickname="Alice",
    ):
        self.role = role
        self.room_code = room_code
        self.room_id = room_id
        self.nickname = nickname
        self.participant_id = PARTICIPANT_ID
        self.transport_kind = "supabase"
        self.channels_ready = channels_ready
        self.channel_status = {"room": channels_ready}
        self.channel_states = {
            "room": "SUBSCRIBED" if channels_ready else "CHANNEL_ERROR",
        }
        self.channel_error = channel_error
        self.room_active = bool(room_code or room_id)
        self.connected = True
        self.leave_calls = 0
        self.close_calls = 0
        self.leave_error = None
        self.room_state = None
        self.events = []
        self.broadcasts = []
        self.subscriptions = {}
        self.unsubscribed = []
        self.poll_started = 0
        self.poll_stopped = 0

    async def send_event(self, event, payload):
        self.events.append((event, payload))

    def start_playback_poll(self, interval=2.0):
        self.poll_started += 1

    def stop_playback_poll(self):
        self.poll_stopped += 1

    async def broadcast_playback(self, playback, reference=None):
        self.broadcasts.append((dict(playback), dict(reference or {})))
        payload = {"playback": dict(playback)}
        if reference:
            payload["anime_reference"] = reference
        for source, target in (
            ("url", "stream_url"),
            ("title", "title"),
            ("episode_number", "episode_number"),
            ("headers", "headers"),
        ):
            value = playback.get(source)
            if value not in (None, "", {}):
                payload[target] = value
        await self.send_event("episode_changed", payload)

    def subscribe(self, event, callback):
        self.subscriptions.setdefault(event, []).append(callback)

    def unsubscribe(self, event, callback):
        handlers = self.subscriptions.get(event, [])
        if callback in handlers:
            handlers.remove(callback)
        self.unsubscribed.append(event)

    def deliver(self, event, payload):
        for callback in list(self.subscriptions.get(event, [])):
            callback(payload)

    async def leave_room(self):
        self.leave_calls += 1
        if self.leave_error:
            raise self.leave_error
        self.room_code = None
        self.room_id = None
        self.room_active = False

    async def get_room_state(self):
        if self.room_state is not None:
            return self.room_state
        return {
            "participants": [
                {
                    "id": self.participant_id,
                    "nickname": self.nickname,
                    "role": self.role,
                }
            ],
            "you": {
                "participant_id": self.participant_id,
                "nickname": self.nickname,
                "role": self.role,
            },
        }

    def local_room_state(self):
        return {
            "room_code": self.room_code,
            "room_id": self.room_id,
            "participants": [],
            "room_info": {},
            "you": {
                "participant_id": self.participant_id,
                "nickname": self.nickname,
                "role": self.role,
            },
        }

    async def retry_connection(self):
        pass

    async def close(self):
        self.close_calls += 1


def render(ui, renderable):
    ui.console.print(renderable)
    return ui.console.file.getvalue()


def test_host_room_panel_is_friendly_and_hides_diagnostics():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(role="host", channels_ready=True, nickname="Alice")
    state = {
        "participants": [
            {"id": PARTICIPANT_ID, "nickname": "Alice", "role": "host"},
        ],
        "you": {
            "participant_id": PARTICIPANT_ID,
            "nickname": "Alice",
            "role": "host",
        },
    }

    model = ui.room_view_model(client, state=state)
    output = render(ui, ui._build_room_panel(model))

    assert model["role"] == "HOST"
    assert model["status"] == "CONNECTED"
    assert model["status_label"] == "Connected"
    assert "You're hosting" in output
    assert "1 2 3 4 5 6" in output
    assert "Share this code so friends can join" in output
    assert "HOST  Alice" in output
    assert "[Host]" in output
    assert "[b] Browse Anime" in output
    assert "[l] Leave Room" in output
    assert "[i] Details" in output
    assert "[q] Quit" in output
    assert "[c] Copy code" not in output
    assert "supabase" not in output.lower()
    assert "channel" not in output.lower()


def test_guest_room_panel_identifies_host_and_you():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(role="guest", nickname="Bob")
    state = {
        "participants": [
            {"id": OTHER_PARTICIPANT_ID, "nickname": "Alice", "role": "host"},
            {"id": PARTICIPANT_ID, "nickname": "Bob", "role": "guest"},
        ],
        "you": {
            "participant_id": PARTICIPANT_ID,
            "nickname": "Bob",
            "role": "guest",
        },
    }

    model = ui.room_view_model(client, state=state)
    output = render(ui, ui._build_room_panel(model))

    assert model["role"] == "GUEST"
    assert model["host_name"] == "Alice"
    assert "You're a guest" in output
    assert "HOST  Alice" in output
    assert "Bob" in output
    assert "[Host]" in output
    assert "[Guest]" in output
    assert "[b] Browse Anime" not in output
    assert "[l] Leave Room" in output
    assert "[i] Details" in output
    assert "[q] Quit" in output
    assert model["participants"] == ["Alice (host)", "Bob (guest)"]


def test_partial_connection_uses_friendly_recovery_language():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(
        role="host",
        channels_ready=False,
        channel_error="Realtime host channel was not authorized: timeout",
        nickname="Alice",
    )

    model = ui.room_view_model(
        client,
        initial_error=(
            "The room is open, but live updates aren't connected. "
            "Try again, or leave the room."
        ),
        technical_error=client.channel_error,
    )
    output = render(ui, ui._build_room_panel(model))

    assert model["status"] == "RECONNECTING"
    assert model["status_label"] == "Reconnecting…"
    assert model["reconnecting"] is True
    assert model["spinner"] in output
    assert "live updates aren't connected" in output
    assert "Connection lost" not in output
    assert "[r] Retry" not in output
    assert "[l] Leave Room" in output
    assert "[q] Quit" in output
    assert "channel" not in output.lower()
    assert "not authorized" not in output.lower()


def test_reconnect_spinner_stays_out_of_the_way():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(
        role="guest",
        channels_ready=False,
        channel_error="socket closed",
        nickname="Bob",
    )

    model = ui.room_view_model(client, spinner_frame=3)
    output = render(ui, ui._build_room_panel(model))

    assert model["reconnecting"] is True
    assert model["status_label"] == "Reconnecting…"
    assert model["spinner"] in output
    assert "Connected" not in output
    assert "Connection lost" not in output


def test_quit_key_leaves_room_and_exits_watch_together(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    RecordingLive.instances.clear()
    client = FakeClient(role="host", channels_ready=True, nickname="Alice")
    ui = WatchTogetherUI(make_console())
    keys = iter(["q"])
    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", lambda: next(keys, None))
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert asyncio.run(ui._room_screen(client)) is True
    assert ui._quit_requested is True
    assert client.leave_calls == 1
    assert client.close_calls == 0


def test_diagnostics_are_available_but_hidden_from_primary_view():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(
        role="host",
        channels_ready=True,
        nickname="Alice",
        channel_error=None,
    )
    model = ui.room_view_model(client)

    primary = render(ui, ui._build_room_panel(model))
    ui.console.file.seek(0)
    ui.console.file.truncate(0)
    diagnostics = render(ui, ui._build_room_panel(model, show_diagnostics=True))

    assert "CONNECTION DETAILS" in diagnostics
    assert "Transport" in diagnostics
    assert "Channels" in diagnostics
    assert "room joined" in diagnostics
    assert "Channel states" in diagnostics
    assert "SUBSCRIBED" in diagnostics
    assert "supabase" in diagnostics.lower()
    assert "Room ID" in diagnostics
    assert "supabase" not in primary.lower()


def test_diagnostics_show_sanitized_real_error():
    ui = WatchTogetherUI(make_console())
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"
    client = FakeClient(
        role="host",
        channels_ready=False,
        channel_error=f"token expired: {token}",
        nickname="Alice",
    )
    client.channel_states = {"room": "CHANNEL_ERROR"}
    model = ui.room_view_model(client)

    diagnostics = render(ui, ui._build_room_panel(model, show_diagnostics=True))

    assert "CHANNEL_ERROR" in diagnostics
    assert "token expired" in diagnostics
    assert token not in diagnostics
    assert "[redacted]" in diagnostics


def test_menu_offers_host_join_and_back(monkeypatch):
    ui = WatchTogetherUI(make_console())
    captured = {}

    def fake_choice(**kwargs):
        captured.update(kwargs)
        return "back"

    monkeypatch.setattr(ui, "_choice_screen", fake_choice)

    assert ui.watch_together_menu() == "back"
    assert [option["label"] for option in captured["options"]] == [
        "Host a room",
        "Join a room",
        "Back",
    ]
    assert captured["footer"].startswith("You'll appear as")


def test_choice_screen_arrow_navigation_and_back(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    class DummyLive:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, renderable, refresh=False):
            pass

    class DummyRaw:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    ui = WatchTogetherUI(make_console())
    options = [
        {"label": "Host a room", "action": "host"},
        {"label": "Join a room", "action": "join"},
        {"label": "Back", "action": "back"},
    ]

    monkeypatch.setattr(ww, "RawTerminal", DummyRaw)
    monkeypatch.setattr(ww, "Live", DummyLive)
    keys = iter(["DOWN", "ENTER"])
    monkeypatch.setattr(ww, "get_key", lambda: next(keys, None))

    assert ui._choice_screen(
        badge="Watch Together",
        subtitle="",
        options=options,
    ) == "join"

    keys = iter(["b"])
    monkeypatch.setattr(ww, "get_key", lambda: next(keys, None))

    assert ui._choice_screen(
        badge="Watch Together",
        subtitle="",
        options=options,
    ) == "back"


def test_room_screen_fits_a_small_terminal(monkeypatch):
    console = Console(file=io.StringIO(), width=40, force_terminal=False, height=18)
    ui = WatchTogetherUI(console)
    client = FakeClient(role="host", channels_ready=True, nickname="Alice")

    model = ui.room_view_model(client)
    ui.console.print(ui._build_room_view(model))
    output = ui.console.file.getvalue()

    assert "1 2 3 4 5 6" in output
    assert "[l] Leave Room" in output
    assert "[i] Details" in output
    assert "[q] Quit" in output


def test_code_entry_accepts_digits_and_backspace(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    class DummyLive:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, renderable, refresh=False):
            pass

    class DummyRaw:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    keys = iter(["1", "2", "3", "BACKSPACE", "4", "5", "6", "7", "ENTER"])
    monkeypatch.setattr(ww, "RawTerminal", DummyRaw)
    monkeypatch.setattr(ww, "Live", DummyLive)
    monkeypatch.setattr(ww, "get_key", lambda: next(keys, None))

    ui = WatchTogetherUI(make_console())
    assert ui._code_entry_screen() == "124567"


@pytest.mark.asyncio
async def test_interrupting_room_screen_attempts_server_leave(monkeypatch):
    ui = WatchTogetherUI(make_console())
    client = FakeClient()

    async def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ui, "_room_screen", interrupted)

    with pytest.raises(KeyboardInterrupt):
        await ui._guard_room_screen(client)

    assert client.leave_calls == 1
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_leave_failure_keeps_room_active_for_retry(monkeypatch):
    ui = WatchTogetherUI(make_console())
    client = FakeClient(channels_ready=False)
    client.leave_error = PartyClientError("leave_failed", "temporary outage")

    async def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ui, "_room_screen", interrupted)
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    with pytest.raises(KeyboardInterrupt):
        await ui._guard_room_screen(client)

    assert client.leave_calls == 1
    assert client.room_code == "123456"
    assert client.room_id == ROOM_ID


def test_main_menu_advertises_watch_together():
    ui = UIManager()
    model = ui._home_navigation_model([])

    watch = [item for item in model["grid_items"] if item["action"] == "watchtogether"]
    assert watch == [{"key": "w", "label": "watch together", "action": "watchtogether"}]

    shortcuts = [item for item in model["shortcut_items"] if item["action"] == "watchtogether"]
    assert shortcuts == watch


def test_main_menu_grid_keeps_exit_last_and_all_tiles():
    ui = UIManager()
    model = ui._home_navigation_model([])
    rows = ui._home_grid_rows(model["grid_items"], model["exit_item"], 4)
    flat = [item for row in rows for item in row]

    assert flat[-1]["action"] == "quit"
    assert all(item is None or item in model["grid_items"] + [model["exit_item"]] for item in flat)
    assert len([item for item in flat if item is not None]) == len(model["grid_items"]) + 1


@pytest.mark.asyncio
async def test_room_screen_stays_until_leave_key_and_leaves(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    class DummyLive:
        def __init__(self, *args, **kwargs):
            self.renders = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, renderable, refresh=False):
            self.renders.append(renderable)

    class DummyRaw:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    keys = iter(["l"])
    monkeypatch.setattr(ww, "RawTerminal", DummyRaw)
    monkeypatch.setattr(ww, "Live", DummyLive)
    monkeypatch.setattr(ww, "get_key", lambda: next(keys, None))

    ui = WatchTogetherUI(make_console())
    client = FakeClient(channels_ready=False)
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True
    assert client.leave_calls == 1
    assert client.room_id is None


def test_friendly_errors_do_not_expose_internal_codes():
    ui = WatchTogetherUI(make_console())
    title, message, technical = ui._friendly_error(
        PartyClientError("room_full", "room_full: capacity reached"),
        "join",
    )

    assert "full" in title.lower()
    assert "room_full" not in title
    assert "room_full" not in message
    assert "room_full" in technical


def test_friendly_error_explains_invalid_supabase_configuration():
    from nyoanime.party.supabase_config import SupabaseConfigError

    ui = WatchTogetherUI(make_console())
    title, message, technical = ui._friendly_error(
        SupabaseConfigError(
            "the Supabase publishable key is invalid; use the project's "
            "publishable (or legacy anon) key, not a command"
        ),
        "create",
    )

    assert title == "Watch Together needs setup"
    assert "python main.py --setup-supabase" in message
    assert "publishable (anon) key" in message
    assert "not a command" in technical


def test_room_view_model_includes_local_you_before_state_loads():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(role="host", nickname="Alice")

    model = ui.room_view_model(client)

    assert model["participant_rows"] == [
        {"name": "Alice", "role": "host", "is_you": True, "is_host": True}
    ]
    assert model["you_name"] == "Alice"


def test_copy_is_safe_when_not_attached_to_a_terminal():
    ui = WatchTogetherUI(make_console())
    assert ui._copy_to_clipboard("123456") is False


class RecordingLive:
    instances = []

    def __init__(self, *args, **kwargs):
        self.renders = []
        self.stopped = 0
        self.started = 0
        RecordingLive.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update(self, renderable, refresh=False):
        self.renders.append(renderable)

    def stop(self):
        self.stopped += 1

    def start(self, refresh=False):
        self.started += 1


class NoopRaw:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def render_text(renderable):
    console = Console(file=io.StringIO(), width=100, force_terminal=False)
    console.print(renderable)
    return console.file.getvalue()


def test_room_panel_advertises_browse_without_hiding_leave():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(role="host", channels_ready=True, nickname="Alice")

    output = render(ui, ui._build_room_panel(ui.room_view_model(client)))

    assert "[b] Browse Anime" in output
    assert "[l] Leave Room" in output


def test_host_footer_matches_streamlined_controls():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(role="host", channels_ready=True, nickname="Alice")

    output = render(ui, ui._build_room_panel(ui.room_view_model(client)))

    assert (
        "[b] Browse Anime • [l] Leave Room • [i] Details • [q] Quit"
        in output
    )


def test_guest_footer_has_no_browse_control():
    ui = WatchTogetherUI(make_console())
    client = FakeClient(role="guest", channels_ready=True, nickname="Bob")

    output = render(ui, ui._build_room_panel(ui.room_view_model(client)))

    assert "[b] Browse Anime" not in output
    assert "[l] Leave Room • [i] Details • [q] Quit" in output


@pytest.mark.asyncio
async def test_browse_keeps_room_identity_and_returns_to_it(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    RecordingLive.instances.clear()
    client = FakeClient(role="host", channels_ready=True, nickname="Alice")
    observed = {}

    def browse(api):
        observed.update({
            "api": api,
            "room_code": client.room_code,
            "room_id": client.room_id,
            "participant_id": client.participant_id,
            "leave_calls": client.leave_calls,
            "close_calls": client.close_calls,
            "connected": client.connected,
        })
        return None

    marker = object()
    ui = WatchTogetherUI(make_console(), browse_callback=browse)
    ui._api = marker
    keys = iter(["b", "l"])
    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", lambda: next(keys, None))
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True

    assert observed["api"] is marker
    assert observed["room_code"] == "123456"
    assert observed["room_id"] == ROOM_ID
    assert observed["participant_id"] == PARTICIPANT_ID
    assert observed["leave_calls"] == 0
    assert observed["close_calls"] == 0
    assert observed["connected"] is True
    assert client.leave_calls == 1
    live = RecordingLive.instances[0]
    assert live.stopped == 1
    assert live.started == 1
    assert any("Back in the room" in render_text(row) for row in live.renders)
    assert "episode_changed" in client.subscriptions
    assert "room_state" in client.subscriptions
    assert client.unsubscribed


@pytest.mark.asyncio
async def test_host_browse_broadcasts_selection_without_recreating_room(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    RecordingLive.instances.clear()
    client = FakeClient(role="host", channels_ready=True, nickname="Alice")
    selection = {
        "anime": {
            "id": "frieren",
            "title_en": "Frieren: Beyond Journey's End",
            "thumbnail": "https://example.test/frieren.jpg",
            "type": "TV",
        },
        "episode": {"display_num": 3, "title": "Killing Magic"},
        "provider_id": "hianime",
        "language_mode": "en_sub",
    }
    ui = WatchTogetherUI(make_console(), browse_callback=lambda api: selection)
    keys = iter(["b", "l"])
    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", lambda: next(keys, None))
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True

    assert len(client.events) == 1
    event, payload = client.events[0]
    assert event == "episode_changed"
    reference = payload["anime_reference"]
    assert reference["title"] == "Frieren: Beyond Journey's End"
    assert reference["anime_id"] == "frieren"
    assert reference["episode_number"] == "3"
    assert reference["episode_title"] == "Killing Magic"
    assert reference["provider_id"] == "hianime"
    assert reference["poster"] == "https://example.test/frieren.jpg"
    assert client.leave_calls == 1
    assert client.close_calls == 0


@pytest.mark.asyncio
async def test_guest_receives_and_displays_host_selection(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    RecordingLive.instances.clear()
    client = FakeClient(role="guest", channels_ready=True, nickname="Bob")
    dispatched = {"done": False}

    def key():
        if not dispatched["done"]:
            dispatched["done"] = True
            client.deliver(
                "episode_changed",
                {
                    "payload": {
                        "anime_reference": {
                            "title": "Frieren",
                            "episode_number": "4",
                            "episode_title": "The Journey's End",
                        }
                    }
                },
            )
        return "l"

    ui = WatchTogetherUI(make_console())
    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", key)
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True

    live = RecordingLive.instances[0]
    outputs = "\n".join(render_text(row) for row in live.renders)
    assert "Frieren" in outputs
    assert "Episode 4" in outputs
    assert "Chosen by the host." in outputs
    assert client.leave_calls == 1
    assert client.poll_started == 1
    assert client.poll_stopped == 1


@pytest.mark.asyncio
async def test_guest_browse_does_not_hijack_the_room(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    RecordingLive.instances.clear()
    client = FakeClient(role="guest", channels_ready=True, nickname="Bob")
    ui = WatchTogetherUI(
        make_console(),
        browse_callback=lambda api: {"title": "Not Mine", "episode_number": "1"},
    )
    keys = iter(["b", "l"])
    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", lambda: next(keys, None))
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True

    assert client.events == []
    outputs = "\n".join(
        render_text(row) for row in RecordingLive.instances[0].renders
    )
    assert "Only the host can choose what the room watches." in outputs


def test_selection_reference_accepts_direct_reference():
    reference = WatchTogetherUI._selection_reference(
        {"anime_reference": {"title": "Frieren", "episode_number": "2"}}
    )

    assert reference == {"title": "Frieren", "episode_number": "2"}


@pytest.mark.asyncio
async def test_offline_host_pick_is_shared_after_reconnect(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    RecordingLive.instances.clear()
    client = FakeClient(role="host", channels_ready=False, nickname="Alice")
    ui = WatchTogetherUI(
        make_console(),
        browse_callback=lambda api: {"title": "Frieren", "episode_number": "5"},
    )
    calls = {"keys": 0}

    def key():
        calls["keys"] += 1
        if calls["keys"] == 1:
            return "b"
        if calls["keys"] == 2:
            client.channels_ready = True
            client.channel_status = {"room": True}
            return None
        return "l"

    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", key)
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True

    assert len(client.events) == 1
    event, payload = client.events[0]
    assert event == "episode_changed"
    assert payload["anime_reference"]["episode_number"] == "5"
    assert client.leave_calls == 1
    assert client.close_calls == 0


def test_app_browse_reuses_existing_anime_and_episode_menus(monkeypatch):
    import nyoanime.utils as utils
    from nyoanime.app import NyoAnimeApp
    from nyoanime.models import AnimeResult, Episode

    anime = AnimeResult(id="frieren", title_en="Frieren", thumbnail="poster.jpg")
    episodes = [Episode(number="1", display_num=1), Episode(number="2", display_num=2)]
    console = Console(file=io.StringIO(), width=100, force_terminal=False)
    calls = []

    class FakeUI:
        def __init__(self):
            self.console = console

        def clear(self):
            pass

        def run_with_loading(self, message, func, *args):
            return func(*args)

        def anime_selection_menu(self, results, **kwargs):
            calls.append(("anime", len(results)))
            return 0

        def episode_selection_menu(self, *args, **kwargs):
            calls.append(("episode", kwargs.get("anime_details", {}).get("type")))
            return 1

        def render_message(self, *args, **kwargs):
            calls.append(("message", args[0]))

    class FakeAPI:
        id = "hianime"
        language = "en"

        def search_anime(self, query):
            calls.append(("search", query))
            return [anime]

        def get_episodes(self, anime_id):
            calls.append(("episodes", anime_id))
            return episodes

    class FakeHistory:
        def get_last_watched(self, anime_id, provider=""):
            return None

        def get_all_episodes_progress(self, anime_id, provider=""):
            return {}

    class FakeFavorites:
        def is_favorite(self, anime_id):
            return False

    monkeypatch.setattr(console, "input", lambda *args, **kwargs: "frieren")
    monkeypatch.setattr(utils, "restore_terminal_for_input", lambda: None)
    monkeypatch.setattr(utils, "enter_raw_mode_after_input", lambda: None)

    app = NyoAnimeApp.__new__(NyoAnimeApp)
    app.ui = FakeUI()
    app.history = FakeHistory()
    app.favorites = FakeFavorites()
    app.rpc = None
    app._language_mode = lambda: "en_sub"
    app._get_default_download_quality = lambda: "1080p"
    app._get_download_mode = lambda: "internal"
    app._get_download_directory = lambda: "downloads"

    selection = app.browse_anime_for_watch_together(FakeAPI())

    assert selection["anime"] is anime
    assert selection["episode"] is episodes[1]
    assert selection["provider_id"] == "hianime"
    assert selection["language_mode"] == "en_sub"
    assert calls[0] == ("search", "frieren")
    assert ("episodes", "frieren") in calls
    assert ("anime", 1) in calls
    assert any(call[0] == "episode" for call in calls)


class _FakeMPV:
    instances = []

    def __init__(self, host=False, player_manager=None):
        self.host = host
        self.session = {"fake": True}
        self.connected = True
        self.connected_with = None
        self.disconnects = 0
        _FakeMPV.instances.append(self)

    async def connect(self, **kwargs):
        self.connected_with = kwargs

    async def disconnect(self):
        self.disconnects += 1
        self.session = None
        self.connected = False


class _FakeSync:
    instances = []

    def __init__(self, mpv, role="guest", send_callback=None, anime_reference=None):
        self.mpv = mpv
        self.role = role
        self.send_callback = send_callback
        self.anime_reference = anime_reference
        self.on_error = lambda exc: None
        self.host_started = 0
        self.stopped = 0
        self.applied = []
        self.state = "disconnected"
        _FakeSync.instances.append(self)

    async def host_start_playback(self):
        self.host_started += 1
        self.state = "playing"

    def set_state(self, state):
        self.state = state

    async def apply_host_state(self, payload):
        self.applied.append(payload)

    async def stop(self):
        self.stopped += 1
        await self.mpv.disconnect()


def _playback_payload(url="https://cdn.example.test/episode.m3u8"):
    return {
        "url": url,
        "title": "Frieren - Ep 3",
        "episode_number": "3",
        "headers": {
            "Referer": "https://provider.example.test/",
            "User-Agent": "NyoAnime",
            "Cookie": "session=secret",
        },
        "audio_lang": "jpn,ja",
        "quality": "1080p",
    }


@pytest.mark.asyncio
async def test_host_selection_launches_mpv_and_broadcasts_stream(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    _FakeMPV.instances.clear()
    _FakeSync.instances.clear()
    RecordingLive.instances.clear()
    monkeypatch.setattr(ww, "MPVController", _FakeMPV)
    monkeypatch.setattr(ww, "SyncEngine", _FakeSync)

    client = FakeClient(role="host", channels_ready=True, nickname="Alice")
    selection = {
        "anime": {"id": "frieren", "title_en": "Frieren"},
        "episode": {"display_num": 3, "number": "ep-3"},
        "provider_id": "hianime",
        "language_mode": "en_sub",
        "playback": _playback_payload(),
    }
    ui = WatchTogetherUI(make_console(), browse_callback=lambda api: selection)
    calls = {"n": 0}

    def key():
        calls["n"] += 1
        if calls["n"] == 1:
            return "b"
        if calls["n"] < 8:
            return None
        return "l"

    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", key)
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True

    assert len(client.events) == 1
    event, payload = client.events[0]
    assert event == "episode_changed"
    assert payload["anime_reference"]["episode_number"] == "3"
    assert payload["playback"]["url"] == "https://cdn.example.test/episode.m3u8"
    assert payload["playback"]["headers"]["Referer"] == "https://provider.example.test/"
    assert "Cookie" not in payload["playback"]["headers"]
    assert payload["stream_url"] == "https://cdn.example.test/episode.m3u8"
    assert payload["episode_number"] == "3"
    assert len(client.broadcasts) == 1
    broadcast_playback, broadcast_reference = client.broadcasts[0]
    assert broadcast_playback["url"] == "https://cdn.example.test/episode.m3u8"
    assert "Cookie" not in broadcast_playback["headers"]
    assert broadcast_reference["episode_number"] == "3"

    assert len(_FakeMPV.instances) == 1
    mpv = _FakeMPV.instances[0]
    assert mpv.host is True
    assert mpv.connected_with["url"] == "https://cdn.example.test/episode.m3u8"
    assert mpv.connected_with["headers"]["Cookie"] == "session=secret"

    assert len(_FakeSync.instances) == 1
    assert _FakeSync.instances[0].role == "host"
    assert _FakeSync.instances[0].host_started == 1


@pytest.mark.asyncio
async def test_guest_episode_event_launches_mpv_and_applies_sync(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    _FakeMPV.instances.clear()
    _FakeSync.instances.clear()
    RecordingLive.instances.clear()
    monkeypatch.setattr(ww, "MPVController", _FakeMPV)
    monkeypatch.setattr(ww, "SyncEngine", _FakeSync)

    client = FakeClient(role="guest", channels_ready=True, nickname="Bob")
    ui = WatchTogetherUI(make_console())
    calls = {"n": 0}
    sync_payload = {
        "host": True,
        "sequence": 1,
        "position": 12.0,
        "duration": 1440.0,
        "paused": False,
        "speed": 1.0,
        "timestamp_ms": 1,
    }

    def key():
        calls["n"] += 1
        if calls["n"] == 1:
            client.deliver(
                "episode_changed",
                {
                    "payload": {
                        "anime_reference": {
                            "title": "Frieren",
                            "episode_number": "3",
                        },
                        "playback": _playback_payload(),
                    }
                },
            )
            return None
        if calls["n"] == 2:
            client.deliver("sync_playback", {"payload": sync_payload})
            return None
        if calls["n"] < 8:
            return None
        return "l"

    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", key)
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True

    assert len(_FakeMPV.instances) == 1
    assert _FakeMPV.instances[0].host is False
    assert (
        _FakeMPV.instances[0].connected_with["url"]
        == "https://cdn.example.test/episode.m3u8"
    )
    assert len(_FakeSync.instances) == 1
    assert _FakeSync.instances[0].role == "guest"
    await asyncio.sleep(0)  # let the apply_sync task run
    assert _FakeSync.instances[0].applied == [sync_payload]


@pytest.mark.asyncio
async def test_mpv_launch_failure_becomes_a_notice(monkeypatch):
    import nyoanime.ui_watchtogether as ww

    class BrokenMPV(_FakeMPV):
        async def connect(self, **kwargs):
            raise FileNotFoundError("MPV not found on PATH")

    _FakeMPV.instances.clear()
    _FakeSync.instances.clear()
    RecordingLive.instances.clear()
    monkeypatch.setattr(ww, "MPVController", BrokenMPV)
    monkeypatch.setattr(ww, "SyncEngine", _FakeSync)

    client = FakeClient(role="host", channels_ready=True, nickname="Alice")
    selection = {
        "anime": {"id": "frieren", "title_en": "Frieren"},
        "episode": {"display_num": 3},
        "provider_id": "hianime",
        "playback": _playback_payload(),
    }
    ui = WatchTogetherUI(make_console(), browse_callback=lambda api: selection)
    calls = {"n": 0}

    def key():
        calls["n"] += 1
        if calls["n"] == 1:
            return "b"
        if calls["n"] < 8:
            return None
        return "l"

    monkeypatch.setattr(ww, "RawTerminal", NoopRaw)
    monkeypatch.setattr(ww, "Live", RecordingLive)
    monkeypatch.setattr(ww, "get_key", key)
    monkeypatch.setattr(ui, "_pause", lambda *args, **kwargs: None)

    assert await ui._room_screen(client) is True

    outputs = "\n".join(
        render_text(row) for row in RecordingLive.instances[0].renders
    )
    assert "MPV isn't installed" in outputs


def test_app_browse_resolves_a_direct_stream_for_watch_together(monkeypatch):
    import nyoanime.utils as utils
    from nyoanime.app import NyoAnimeApp
    from nyoanime.models import AnimeResult, Episode, QualityOption

    anime = AnimeResult(
        id="frieren",
        title_en="Frieren",
        thumbnail="poster.jpg",
        type="TV",
    )
    episodes = [Episode(number="ep-3", display_num=3, title="Killing Magic")]
    console = Console(file=io.StringIO(), width=100, force_terminal=False)
    quality = QualityOption(
        name="1080p",
        direct_url="https://cdn.example/frieren-3.m3u8",
        headers={
            "Referer": "https://provider.example/",
            "User-Agent": "NyoAnime",
            "Cookie": "session=secret",
        },
        audio_lang="jpn,ja",
        subtitle_url="https://cdn.example/frieren-3.vtt",
        subtitle_lang="en",
    )

    class FakeUI:
        def __init__(self):
            self.console = console

        def clear(self):
            pass

        def run_with_loading(self, message, func, *args):
            return func(*args)

        def anime_selection_menu(self, results, **kwargs):
            return 0

        def episode_selection_menu(self, *args, **kwargs):
            return 0

        def render_message(self, *args, **kwargs):
            pass

    class FakeAPI:
        id = "hianime"
        language = "en"
        type = "TV"

        def search_anime(self, query):
            return [anime]

        def get_episodes(self, anime_id):
            return episodes

        def get_streaming_servers(self, anime_id, episode_num, anime_type="SERIES"):
            assert anime_id == "frieren"
            assert episode_num == "ep-3"
            return {"Qualities": [quality], "CurrentEpisode": {}}

    class FakeHistory:
        def get_last_watched(self, anime_id, provider=""):
            return None

        def get_all_episodes_progress(self, anime_id, provider=""):
            return {}

    class FakeFavorites:
        def is_favorite(self, anime_id):
            return False

    monkeypatch.setattr(console, "input", lambda *args, **kwargs: "frieren")
    monkeypatch.setattr(utils, "restore_terminal_for_input", lambda: None)
    monkeypatch.setattr(utils, "enter_raw_mode_after_input", lambda: None)

    app = NyoAnimeApp.__new__(NyoAnimeApp)
    app.ui = FakeUI()
    app.history = FakeHistory()
    app.favorites = FakeFavorites()
    app.rpc = None
    app._language_mode = lambda: "en_sub"
    app._get_default_download_quality = lambda: "1080p"
    app._get_download_mode = lambda: "internal"
    app._get_download_directory = lambda: "downloads"

    selection = app.browse_anime_for_watch_together(FakeAPI())

    assert selection["playback"]["url"] == "https://cdn.example/frieren-3.m3u8"
    assert selection["playback"]["episode_number"] == "3"
    assert selection["playback"]["episode_title"] == "Killing Magic"
    assert selection["playback"]["headers"]["Referer"] == "https://provider.example/"
    assert selection["playback"]["subtitle_url"] == "https://cdn.example/frieren-3.vtt"
    assert selection["playback_error"] is None
