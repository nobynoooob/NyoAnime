"""Shared fixtures for Watch Together tests."""
import pytest

from nyoanime.party import lifecycle


@pytest.fixture(autouse=True)
def _reset_exit_cleanup_registry():
    """Keep hosted-room tracking from leaking between tests (and into atexit)."""
    lifecycle.reset()
    yield
    lifecycle.reset()
