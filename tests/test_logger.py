"""
Unit tests for NyoAnime logger.py
Tests debug logging, local crash dumps, and the absence of network reporting.
"""

import sys
import json
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nyoanime.logger import (
    DebugLogger,
    logger,
    get_system_summary,
    get_base_dir,
    record_crash,
    install_global_exception_handler,
)


class TestDebugLogger:
    """Tests for DebugLogger functionality."""

    def test_singleton_instance(self):
        l1 = DebugLogger()
        l2 = DebugLogger()
        assert l1 is l2
        assert l1 is logger

    def test_enable_and_disable(self):
        logger.disable()
        assert not logger.is_enabled()
        logger.enable(console_mirror=False)
        assert logger.is_enabled()
        logger.disable()
        assert not logger.is_enabled()

    def test_logging_methods(self):
        logger.enable()
        logger.debug("TEST", "Debug message")
        logger.info("TEST", "Info message")
        logger.warning("TEST", "Warn message")
        logger.error("TEST", "Error message")
        logger.log_crypto("AES_DECRYPT", "Success")
        logger.log_player("mpv", ["mpv", "http://example.com/test.mp4"], exit_code=0, duration=12.5)
        logger.log_downloader("internal", "http://example.com/test.mp4", "test.mp4", success=True)
        logger.log_request("GET", "http://example.com/api", status_code=200, duration=0.12)
        try:
            raise ValueError("Logged exception")
        except Exception as e:
            logger.log_exception(e, context="unit_test")
        logger.disable()


class TestCrashReporting:
    """Tests for local crash dumps and unhandled crash hook."""

    def test_get_system_summary(self):
        summary = get_system_summary()
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_record_crash_creates_local_dump(self, tmp_path):
        with patch("nyoanime.logger.get_base_dir", return_value=tmp_path):
            try:
                raise RuntimeError("Simulated crash error")
            except RuntimeError:
                exc_type, exc_val, tb = sys.exc_info()
                record_crash(exc_type, exc_val, tb, context="test_context")

        crash_dir = tmp_path / "crashes"
        assert crash_dir.exists()
        dump_files = list(crash_dir.glob("crash_*.json"))
        assert len(dump_files) == 1

        with open(dump_files[0], "r", encoding="utf-8") as f:
            dump_data = json.load(f)
        assert dump_data["error_type"] == "RuntimeError"
        assert "Simulated crash error" in dump_data["error_message"]
        assert dump_data["context"] == "test_context"


    def test_install_global_exception_handler(self):
        orig_hook = sys.excepthook
        try:
            install_global_exception_handler()
            installed_hook = sys.excepthook
            assert installed_hook != orig_hook

            with patch("nyoanime.logger.record_crash") as mock_record:
                try:
                    raise KeyboardInterrupt()
                except KeyboardInterrupt:
                    exc_type, exc_val, tb = sys.exc_info()
                    installed_hook(exc_type, exc_val, tb)
                    mock_record.assert_not_called()

            with patch("nyoanime.logger.record_crash") as mock_record:
                try:
                    raise ZeroDivisionError("division by zero")
                except ZeroDivisionError:
                    exc_type, exc_val, tb = sys.exc_info()
                    installed_hook(exc_type, exc_val, tb)
                    mock_record.assert_called_once()
        finally:
            sys.excepthook = orig_hook
