"""Process-exit cleanup hooks for hosted Watch Together rooms."""
import signal

import pytest

from nyoanime.party import cleanup, lifecycle


def test_sigterm_runs_exit_cleanup_before_exiting(monkeypatch):
    lifecycle.reset()
    monkeypatch.setitem(lifecycle._PREVIOUS, signal.SIGTERM, signal.SIG_DFL)
    calls = []
    monkeypatch.setattr(
        cleanup,
        "leave_active_hosted_rooms_blocking",
        lambda *args, **kwargs: calls.append(1) or 1,
    )

    lifecycle.mark_hosting(object())

    with pytest.raises(SystemExit):
        lifecycle._handle_signal(signal.SIGTERM, None)

    assert calls


def test_sigint_defers_to_the_ui_and_does_not_block(monkeypatch):
    lifecycle.reset()
    monkeypatch.setitem(lifecycle._PREVIOUS, signal.SIGINT, signal.SIG_DFL)
    calls = []
    monkeypatch.setattr(
        cleanup,
        "leave_active_hosted_rooms_blocking",
        lambda *args, **kwargs: calls.append(1) or 1,
    )

    lifecycle.mark_hosting(object())

    # SIG_DFL is not callable, so the handler returns without raising and the
    # UI's KeyboardInterrupt/leave path stays in charge.
    lifecycle._handle_signal(signal.SIGINT, None)

    assert calls == []
