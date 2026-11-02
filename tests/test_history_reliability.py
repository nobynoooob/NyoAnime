import json
from pathlib import Path

from nyoanime.history import HistoryManager


def _history(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return HistoryManager()


class TestProviderAwareHistory:
    def test_new_entries_keep_legacy_bare_key(self, tmp_path, monkeypatch):
        history = _history(tmp_path, monkeypatch)
        history.mark_watched(
            "naruto", "1", "Naruto",
            provider="anime3rb", language_mode="ar_sub",
        )
        assert "naruto" in history.history
        assert history.history["naruto"]["provider"] == "anime3rb"
        assert history.history["naruto"]["language_mode"] == "ar_sub"

    def test_same_slug_on_another_provider_does_not_overwrite(self, tmp_path, monkeypatch):
        history = _history(tmp_path, monkeypatch)
        history.mark_watched("naruto", "1", "Naruto", provider="anime3rb", language_mode="ar_sub")
        history.mark_watched("naruto", "3", "Naruto", provider="hianime", language_mode="en_sub")

        assert history.history["naruto"]["provider"] == "anime3rb"
        assert history.history["hianime:naruto"]["provider"] == "hianime"
        assert history.get_last_watched("naruto", provider="anime3rb") == "1"
        assert history.get_last_watched("naruto", provider="hianime") == "3"

    def test_progress_is_isolated_per_provider(self, tmp_path, monkeypatch):
        history = _history(tmp_path, monkeypatch)
        history.mark_watched("naruto", "1", "Naruto", time_pos=100, duration=1000, provider="anime3rb")
        history.mark_watched("naruto", "2", "Naruto", time_pos=500, duration=1000, provider="hianime")

        ar = history.get_episode_progress("naruto", "1", provider="anime3rb")
        en = history.get_episode_progress("naruto", "2", provider="hianime")
        assert ar["time_pos"] == 100
        assert en["time_pos"] == 500
        assert history.get_episode_progress("naruto", "2", provider="anime3rb")["time_pos"] == 0

    def test_history_list_exposes_language_mode_and_provider(self, tmp_path, monkeypatch):
        history = _history(tmp_path, monkeypatch)
        history.mark_watched("one", "2", "Show", provider="hianime_dub", language_mode="en_dub")
        items = history.get_history()
        assert items[0]["provider"] == "hianime_dub"
        assert items[0]["language_mode"] == "en_dub"
        assert items[0]["episode"] == "2"

    def test_legacy_history_is_preserved_on_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        path = tmp_path / ".nyoanime" / "database" / "history.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "legacy-show": {
                "episode": "7",
                "title": "Legacy Show",
                "time_pos": 120.0,
                "duration": 1440.0,
                "percent": 8,
            }
        }))

        history = HistoryManager()
        assert history.get_last_watched("legacy-show") == "7"
        entry = history.get_episode_progress("legacy-show", "7")
        assert entry["time_pos"] == 120.0
        assert history.history["legacy-show"]["language_mode"] == ""

    def test_remove_targets_the_selected_provider_entry(self, tmp_path, monkeypatch):
        history = _history(tmp_path, monkeypatch)
        history.mark_watched("naruto", "1", "Naruto", provider="anime3rb")
        history.mark_watched("naruto", "2", "Naruto", provider="hianime")

        history.remove("naruto", provider="hianime")
        assert "naruto" in history.history
        assert "hianime:naruto" not in history.history
