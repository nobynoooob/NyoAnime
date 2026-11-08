"""
Unit tests for the Watch Together MPV controller.
"""
import pytest
import asyncio
import time
from unittest.mock import MagicMock, patch
from nyoanime.player import PlayerManager
from nyoanime.party.mpv_controller import MPVController
@pytest.fixture
def mock_player_manager():
    """Fixture that mocks PlayerManager with a play_mpv_session method."""
    mock_pm = MagicMock(spec=PlayerManager)
    # Mock play_mpv_session to return a fake session dict
    mock_tracker = MagicMock()
    mock_tracker.state = {'time_pos': 10.0, 'duration': 120.0, 'paused': False}
    mock_pm.play_mpv_session.return_value = {
        'tracker': mock_tracker,
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    # Mock static method send_mpv_command (synchronous)
    with patch.object(PlayerManager, 'send_mpv_command', new_callable=MagicMock) as mock_send:
        mock_send.return_value = None
        yield mock_pm, mock_send
@pytest.fixture
def controller(mock_player_manager):
    mock_pm, _ = mock_player_manager
    # Create controller with host=True for permission in command tests
    ctrl = MPVController(host=True, player_manager=mock_pm)
    # Set up a mock session to avoid "MPV not connected" errors
    mock_tracker = MagicMock()
    mock_tracker.state = {'time_pos': 0.0, 'duration': 100.0, 'paused': False}
    mock_pm.play_mpv_session.return_value = {
        'tracker': mock_tracker,
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    return ctrl, mock_pm
@pytest.mark.asyncio
async def test_connect_success(controller):
    ctrl, mock_pm = controller
    # Mock wait_for_ipc to avoid waiting for real socket
    from unittest.mock import AsyncMock
    with patch.object(ctrl, 'wait_for_ipc', new_callable=AsyncMock) as mock_wait:
        # Call connect with dummy parameters
        await ctrl.connect(url='http://example.com/video.mkv', title='Test Video')
        # Verify play_mpv_session was called with expected arguments
        mock_pm.play_mpv_session.assert_called_once()
        call_kwargs = mock_pm.play_mpv_session.call_args[1]
        assert call_kwargs['url'] == 'http://example.com/video.mkv'
        assert call_kwargs['title'] == 'Test Video'
        assert ctrl.session is not None
        # Verify wait_for_ipc was called
        mock_wait.assert_called_once()
@pytest.mark.asyncio
async def test_play(controller):
    ctrl, mock_pm = controller
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    with patch('nyoanime.player.PlayerManager.send_mpv_command', new_callable=MagicMock) as mock_send:
        mock_send.return_value = None
        await ctrl.play()
        await ctrl.flush()
        mock_send.assert_called_once()
        assert mock_send.call_args.args[1] == ["set_property", "pause", False]
@pytest.mark.asyncio
async def test_pause(controller):
    ctrl, mock_pm = controller
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    with patch('nyoanime.player.PlayerManager.send_mpv_command', new_callable=MagicMock) as mock_send:
        mock_send.return_value = None
        await ctrl.pause()
        await ctrl.flush()
        mock_send.assert_called_once()
        assert mock_send.call_args.args[1] == ["set_property", "pause", True]
@pytest.mark.asyncio
async def test_seek_absolute(controller):
    ctrl, mock_pm = controller
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    with patch('nyoanime.player.PlayerManager.send_mpv_command', new_callable=MagicMock) as mock_send:
        mock_send.return_value = None
        await ctrl.seek_absolute(55.5)
        await ctrl.flush()
        mock_send.assert_called_once()
        assert mock_send.call_args.args[1] == ["set_property", "time-pos", 55.5]
@pytest.mark.asyncio
async def test_set_speed(controller):
    ctrl, mock_pm = controller
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    with patch('nyoanime.player.PlayerManager.send_mpv_command', new_callable=MagicMock) as mock_send:
        mock_send.return_value = None
        await ctrl.set_speed(1.5)
        await ctrl.flush()
        mock_send.assert_called_once()
        assert mock_send.call_args.args[1] == ["set_property", "speed", 1.5]
def test_get_position(controller):
    ctrl, mock_pm = controller
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 33.3, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    pos = ctrl.get_position()
    assert pos == 33.3
    ctrl.session['tracker'].state['time_pos'] = 45.0
    assert ctrl.get_position() == 45.0
def test_get_duration(controller):
    ctrl, mock_pm = controller
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 200.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    dur = ctrl.get_duration()
    assert dur == 200.0
def test_is_paused(controller):
    ctrl, mock_pm = controller
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': True}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    assert ctrl.is_paused() is True
    ctrl.session['tracker'].state['paused'] = False
    assert ctrl.is_paused() is False
def test_disconnect(controller):
    ctrl, mock_pm = controller
    mock_pm.play_mpv_session.return_value = {
        'tracker': MagicMock(),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    ctrl.session = mock_pm.play_mpv_session.return_value
    # Capture session before disconnect for verification
    session = ctrl.session
    with patch('nyoanime.player.PlayerManager.send_mpv_command', new_callable=MagicMock) as mock_send:
        mock_send.return_value = None
        # Run disconnect (async)
        asyncio.run(ctrl.disconnect())
        mock_send.assert_called_once_with('/tmp/fake.sock', ["quit"])
        session['process'].terminate.assert_called_once()
        session['tracker'].stop.assert_called_once()
        assert ctrl.session is None

@pytest.mark.asyncio
async def test_pause_listener_fires_on_mpv_property_change(controller):
    ctrl, _ = controller
    tracker = MagicMock()
    tracker.state = {'time_pos': 10.0, 'duration': 100.0, 'paused': False}
    ctrl.session = {
        'tracker': tracker,
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    ctrl.pause_poll_interval = 0.02
    changes = []
    ctrl.set_pause_listener(lambda paused, position: changes.append((paused, position)))

    try:
        await asyncio.sleep(0.08)
        assert changes == []

        tracker.state['paused'] = True
        tracker.state['time_pos'] = 42.0
        for _ in range(50):
            if changes:
                break
            await asyncio.sleep(0.02)

        assert changes == [(True, 42.0)]
    finally:
        await ctrl.disconnect()


@pytest.mark.asyncio
async def test_set_pause_does_not_echo_its_own_command(controller):
    ctrl, _ = controller
    tracker = MagicMock()
    tracker.state = {'time_pos': 0.0, 'duration': 100.0, 'paused': False}
    ctrl.session = {
        'tracker': tracker,
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    ctrl.pause_poll_interval = 0.02
    changes = []
    ctrl.set_pause_listener(lambda paused, position: changes.append(paused))

    try:
        with patch(
            'nyoanime.player.PlayerManager.send_mpv_command', new_callable=MagicMock
        ) as mock_send:
            await ctrl.set_pause(True)
            await ctrl.flush()
            assert mock_send.call_args.args[1] == ["set_property", "pause", True]

        tracker.state['paused'] = True
        await asyncio.sleep(0.1)
        assert changes == []
    finally:
        await ctrl.disconnect()


@pytest.mark.asyncio
async def test_slow_ipc_write_does_not_block_command_callers():
    from unittest.mock import MagicMock, patch

    ctrl = MPVController(host=False)
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    sent = []

    def slow_write(sock_path, command, request_id=None, timeout=1.5):
        sent.append(command)
        time.sleep(0.4)
        return None

    with patch('nyoanime.player.PlayerManager.send_mpv_command', side_effect=slow_write):
        started = time.monotonic()
        await asyncio.wait_for(ctrl.seek_absolute(12.5), timeout=0.1)
        enqueue_time = time.monotonic() - started
        await ctrl.flush(timeout=2.0)
        total_time = time.monotonic() - started

    assert sent == [["set_property", "time-pos", 12.5]]
    assert enqueue_time < 0.1
    assert total_time >= 0.35


@pytest.mark.asyncio
async def test_rapid_updates_coalesce_to_the_latest_value():
    from unittest.mock import MagicMock, patch

    ctrl = MPVController(host=False)
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    sent = []

    def write(sock_path, command, request_id=None, timeout=1.5):
        sent.append(command)
        time.sleep(0.02)
        return None

    with patch('nyoanime.player.PlayerManager.send_mpv_command', side_effect=write):
        for position in range(30):
            await ctrl.seek_absolute(float(position))
        await ctrl.flush(timeout=2.0)

    assert sent
    assert sent[-1] == ["set_property", "time-pos", 29.0]
    assert len(sent) <= 2


@pytest.mark.asyncio
async def test_failed_ipc_write_is_dropped_and_the_worker_survives():
    from unittest.mock import MagicMock, patch

    ctrl = MPVController(host=False)
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    calls = []

    def flaky_write(sock_path, command, request_id=None, timeout=1.5):
        calls.append(command)
        if command[:2] == ["set_property", "speed"]:
            raise RuntimeError("socket went away")
        return None

    with patch('nyoanime.player.PlayerManager.send_mpv_command', side_effect=flaky_write):
        await ctrl.set_speed(1.2)
        await ctrl.flush(timeout=2.0)
        await ctrl.pause()
        await ctrl.flush(timeout=2.0)

    assert ["set_property", "speed", 1.2] in calls
    assert ["set_property", "pause", True] in calls


@pytest.mark.asyncio
async def test_show_osd_is_queued_like_any_other_command():
    ctrl = MPVController(host=False)
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    sent = []

    def write(sock_path, command, request_id=None, timeout=1.5):
        sent.append(command)
        return None

    with patch('nyoanime.player.PlayerManager.send_mpv_command', side_effect=write):
        await ctrl.show_osd("Sync Offset: +2.5s")
        await ctrl.flush(timeout=2.0)

    assert sent[-1] == ["show-text", "Sync Offset: +2.5s", 1400]


@pytest.mark.asyncio
async def test_rapid_osd_updates_only_render_the_newest_text():
    ctrl = MPVController(host=False)
    ctrl.session = {
        'tracker': MagicMock(state={'time_pos': 0.0, 'duration': 100.0, 'paused': False}),
        'sock_path': '/tmp/fake.sock',
        'process': MagicMock(),
    }
    sent = []

    def write(sock_path, command, request_id=None, timeout=1.5):
        sent.append(command)
        time.sleep(0.03)
        return None

    with patch('nyoanime.player.PlayerManager.send_mpv_command', side_effect=write):
        await ctrl.show_osd("Sync Offset: +0.5s")
        await ctrl.show_osd("Sync Offset: +1.0s")
        await ctrl.show_osd("Sync Offset: +1.5s")
        await ctrl.flush(timeout=2.0)

    assert sent[-1] == ["show-text", "Sync Offset: +1.5s", 1400]
    assert len(sent) <= 2
