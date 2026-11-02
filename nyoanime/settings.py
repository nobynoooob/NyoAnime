import json
from pathlib import Path
from .storage import atomic_write_json

class SettingsManager:
    def __init__(self):
        self.config_file = self._get_config_path()
        self.settings = self._load_settings()

    def _get_config_path(self) -> Path:
        home_dir = Path.home()
        db_dir = home_dir / ".nyoanime" / "database"
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / "config.json"

    def _load_settings(self) -> dict:
        defaults = {
            "default_quality": "1080p",
            "default_download_quality": "1080p",
            "download_mode": "internal",
            "download_directory": "downloads",
            "download_parallel": 2,
            "download_speed_limit": "unlimited",
            "player": "mpv",
            "auto_next": False,
            "discord_rpc": False,
            "theme": "auto",
            "anime_provider": "anime3rb",
            "language_mode": "ar_sub",
            "auto_provider": True,
            "stardima_language": "dub",
            "stardima_include_unknown": False,
            "debug_logging": False,
        }

        if not self.config_file.exists():
            return defaults

        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                if not isinstance(saved, dict):
                    return defaults
        except (json.JSONDecodeError, IOError, OSError):
            return defaults

        merged = {**defaults, **saved}

        # Existing users predate language modes.  Preserve their provider choice
        # exactly instead of silently switching them to automatic selection.
        if "language_mode" not in saved:
            provider_id = str(saved.get("anime_provider") or "anime3rb").lower()
            if provider_id.startswith("hianime_dub"):
                merged["language_mode"] = "en_dub"
            elif provider_id.startswith("hianime"):
                merged["language_mode"] = "en_sub"
            else:
                merged["language_mode"] = "ar_sub"
            merged["auto_provider"] = False

        if merged.get("language_mode") not in ("ar_sub", "en_sub", "en_dub"):
            merged["language_mode"] = "ar_sub"
        if not isinstance(merged.get("auto_provider"), bool):
            merged["auto_provider"] = True
        return merged

    def save(self):
        try:
            atomic_write_json(self.config_file, self.settings, indent=4, ensure_ascii=False)
        except (IOError, OSError, ValueError, TypeError) as e:
            import sys
            print(f"Warning: Failed to save settings: {e}", file=sys.stderr)

    def reload(self):
        """Re-read config.json so long-lived providers see UI changes immediately."""
        self.settings = self._load_settings()

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value
        self.save()
