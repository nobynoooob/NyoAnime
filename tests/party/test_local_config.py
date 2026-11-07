"""User-level Supabase config loading, precedence and private permissions."""
import stat
import sys

import pytest

import nyoanime.app as app_module
import nyoanime.party.local_config as local_config
import nyoanime.party.session_storage as session_storage
from nyoanime.app import NyoAnimeApp
from nyoanime.party.local_config import (
    load_local_env,
    save_supabase_config,
    write_local_env,
)
from nyoanime.party.session_storage import (
    DEFAULT_SESSION_FILENAME,
    resolve_session_path,
    set_session_override,
)
from nyoanime.party.supabase_config import (
    DEFAULT_SUPABASE_PUBLISHABLE_KEY,
    resolve_supabase_config,
)


def _isolate_env(monkeypatch):
    for key in (
        "SUPABASE_URL",
        "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_ANON_KEY",
        "NYOANIME_WATCH_TOGETHER_NAME",
        "NYOANIME_SESSION_DIR",
        "NYOANIME_SUPABASE_SESSION_PATH",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("UNRELATED", raising=False)


def test_load_local_env_parses_quotes_and_keeps_environment_overrides(
    monkeypatch, tmp_path
):
    _isolate_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# local config",
                'export SUPABASE_URL="https://file.supabase.co"',
                "SUPABASE_PUBLISHABLE_KEY='file-key'",
                "SUPABASE_ANON_KEY=anon-key # inline comment",
                "NYOANIME_WATCH_TOGETHER_NAME=Guest",
                "SUPABASE_SERVICE_ROLE_KEY=must-not-load",
                "UNRELATED=ignored",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert load_local_env(path) == path
    assert resolve_supabase_config() == ("https://env.supabase.co", "file-key")
    assert local_config.os.environ["NYOANIME_WATCH_TOGETHER_NAME"] == "Guest"
    assert "UNRELATED" not in local_config.os.environ
    assert "SUPABASE_SERVICE_ROLE_KEY" not in local_config.os.environ


def test_missing_local_env_changes_nothing(monkeypatch, tmp_path):
    _isolate_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")

    assert load_local_env(tmp_path / "missing.env") is None
    # Missing local files leave the bundled zero-config key in place while the
    # explicit environment URL still wins.
    assert resolve_supabase_config() == (
        "https://env.supabase.co",
        DEFAULT_SUPABASE_PUBLISHABLE_KEY,
    )
    assert local_config.os.environ["SUPABASE_URL"] == "https://env.supabase.co"


def test_save_supabase_config_merges_with_private_permissions(tmp_path):
    path = tmp_path / "profile" / ".env"
    write_local_env(
        {
            "NYOANIME_WATCH_TOGETHER_NAME": "Guest",
            "SUPABASE_URL": "https://old.supabase.co",
        },
        path=path,
    )

    saved = save_supabase_config(
        "https://new.supabase.co",
        "sb_publishable_secret_key",
        path=path,
    )

    assert saved == path
    text = path.read_text(encoding="utf-8")
    assert "SUPABASE_URL=https://new.supabase.co" in text
    assert "SUPABASE_PUBLISHABLE_KEY=sb_publishable_secret_key" in text
    assert "NYOANIME_WATCH_TOGETHER_NAME=Guest" in text
    assert "https://old.supabase.co" not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_setup_flow_saves_once_without_echoing_the_key(tmp_path):
    path = tmp_path / ".env"
    prompts = []

    def prompt(message):
        prompts.append(message)
        return ""

    def secret_prompt(message):
        prompts.append(message)
        return "sb_publishable_entered_once"

    saved = local_config.run_supabase_setup(
        path=path,
        prompt=prompt,
        secret_prompt=secret_prompt,
        default_url="https://example.supabase.co",
    )

    assert saved == path
    text = path.read_text(encoding="utf-8")
    assert "SUPABASE_URL=https://example.supabase.co" in text
    assert "SUPABASE_PUBLISHABLE_KEY=sb_publishable_entered_once" in text
    assert all("sb_publishable_entered_once" not in message for message in prompts)


def test_setup_rejects_invalid_configuration_without_creating_a_file(tmp_path):
    path = tmp_path / ".env"

    with pytest.raises(ValueError):
        save_supabase_config("ftp://example.supabase.co", "key", path=path)
    with pytest.raises(ValueError):
        save_supabase_config("https://example.supabase.co", "", path=path)
    with pytest.raises(ValueError):
        save_supabase_config(
            "https://example.supabase.co",
            "python main.py --setup-supabase",
            path=path,
        )

    assert not path.exists()


def test_publishable_key_validation_accepts_new_and_legacy_keys():
    from nyoanime.party.supabase_config import validate_publishable_key

    assert validate_publishable_key("sb_publishable_1234567890abcdef") is None
    assert validate_publishable_key("eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.sig") is None
    assert "command" in validate_publishable_key("python main.py --setup-supabase")


def test_cli_setup_wiring_prints_only_the_path(monkeypatch, tmp_path, capsys):
    saved = tmp_path / ".env"
    monkeypatch.setattr(
        local_config, "run_supabase_setup", lambda: saved
    )
    monkeypatch.setattr(sys, "argv", ["nyoanime", "--setup-supabase"])

    app = NyoAnimeApp.__new__(NyoAnimeApp)
    with pytest.raises(SystemExit) as excinfo:
        app.run()

    output = capsys.readouterr().out
    assert excinfo.value.code == 0
    assert str(saved) in output
    assert "Saved Supabase configuration" in output
    assert "sb_" not in output


def test_app_main_loads_local_config_before_client_initialization(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    _isolate_env(monkeypatch)
    write_local_env(
        {
            "SUPABASE_URL": "https://file.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "file-key",
        },
        path=tmp_path / ".nyoanime" / ".env",
    )

    captured = {}

    class FakeApp:
        def __init__(self):
            captured["config"] = resolve_supabase_config()

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(app_module, "NyoAnimeApp", FakeApp)
    app_module.main()

    assert captured == {
        "config": ("https://file.supabase.co", "file-key"),
        "ran": True,
    }


def test_guest_session_override_survives_local_config(monkeypatch, tmp_path):
    _isolate_env(monkeypatch)
    config_path = tmp_path / ".env"
    write_local_env(
        {
            "SUPABASE_URL": "https://file.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "file-key",
            "NYOANIME_SESSION_DIR": str(tmp_path / "from-file"),
        },
        path=config_path,
    )
    load_local_env(config_path)

    guest_dir = tmp_path / "guest"
    set_session_override(session_dir=guest_dir)
    assert resolve_session_path() == guest_dir / DEFAULT_SESSION_FILENAME

    exact = tmp_path / "exact.json"
    set_session_override(session_path=exact)
    assert resolve_session_path() == exact
    assert session_storage.session_path_from_environment() == exact
