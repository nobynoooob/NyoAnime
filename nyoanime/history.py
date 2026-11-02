import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
from .storage import atomic_write_json


def format_seconds(seconds: Optional[float]) -> str:
    """Format seconds into HH:MM:SS or MM:SS format."""
    if not seconds or seconds < 0:
        return "00:00"
    try:
        sec_int = int(round(float(seconds)))
        hours = sec_int // 3600
        minutes = (sec_int % 3600) // 60
        secs = sec_int % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"
    except (ValueError, TypeError):
        return "00:00"


class HistoryManager:
    MAX_HISTORY_SIZE = 100

    def __init__(self):
        self.history_file = self._get_history_path()
        self.history = self._load_history()

    def _get_history_path(self) -> Path:
        home_dir = Path.home()
        db_dir = home_dir / ".nyoanime" / "database"
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / "history.json"

    def _get_legacy_paths(self) -> List[Path]:
        """Find legacy database paths used by versions 1.8.2, 1.8.4, and older."""
        home = Path.home()
        paths = [
            home / ".nyoanime" / "history.json",
            home / ".config" / "nyoanime" / "history.json",
            home / ".local" / "share" / "nyoanime" / "history.json",
            Path(__file__).resolve().parent.parent / "database" / "history.json"
        ]
        return [p for p in paths if p.exists() and p != self.history_file]

    def _load_history(self) -> Dict[str, Dict[str, Any]]:
        raw_data = None
        migrated = False

        if self.history_file.exists():
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    raw_data = json.load(f)
            except Exception:
                raw_data = None

        if not raw_data:
            for leg_path in self._get_legacy_paths():
                try:
                    with open(leg_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if data:
                            raw_data = data
                            migrated = True
                            break
                except Exception:
                    continue

        if not raw_data:
            return {}

        normalized: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_data, list):
            migrated = True
            for item in raw_data:
                if isinstance(item, dict):
                    aid = str(item.get('anime_id') or item.get('id') or '')
                    if aid:
                        normalized[aid] = self._normalize_entry(item)
        elif isinstance(raw_data, dict):
            for k, v in raw_data.items():
                if isinstance(v, dict):
                    normalized[str(k)] = self._normalize_entry(v)
                elif isinstance(v, (str, int, float)):
                    migrated = True
                    normalized[str(k)] = self._normalize_entry({
                        'episode': str(v),
                        'title': str(k),
                        'last_updated': datetime.now().isoformat()
                    })

        if migrated and normalized:
            try:
                atomic_write_json(self.history_file, normalized, indent=4, ensure_ascii=False)
            except Exception:
                pass

        return normalized

    def _normalize_entry(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Ensures all 2.0 fields exist with robust fallbacks for 1.8.x entries."""
        ep_str = str(raw.get('episode') or '1')
        title = str(raw.get('title') or 'Unknown')

        try:
            time_pos = round(float(raw.get('time_pos') or 0.0), 1)
        except (ValueError, TypeError):
            time_pos = 0.0

        try:
            duration = round(float(raw.get('duration') or 0.0), 1)
        except (ValueError, TypeError):
            duration = 0.0

        try:
            percent = int(round(float(raw.get('percent') or 0)))
            percent = max(0, min(100, percent))
        except (ValueError, TypeError):
            percent = 0

        completed = bool(raw.get('completed', False)) or (percent >= 90)
        poster_url = str(raw.get('poster_url') or '')
        provider = str(raw.get('provider') or '')
        language_mode = str(raw.get('language_mode') or '')
        last_updated = str(raw.get('last_updated') or datetime.now().isoformat())

        raw_eps_prog = raw.get('episodes_progress')
        episodes_prog = {}
        if isinstance(raw_eps_prog, dict):
            for ep_k, ep_v in raw_eps_prog.items():
                if isinstance(ep_v, dict):
                    try:
                        e_time = round(float(ep_v.get('time_pos') or 0.0), 1)
                        e_dur = round(float(ep_v.get('duration') or 0.0), 1)
                        e_pct = max(0, min(100, int(round(float(ep_v.get('percent') or 0)))))
                    except (ValueError, TypeError):
                        e_time, e_dur, e_pct = 0.0, 0.0, 0
                    episodes_prog[str(ep_k)] = {
                        'time_pos': e_time,
                        'duration': e_dur,
                        'percent': e_pct,
                        'completed': bool(ep_v.get('completed', False)) or (e_pct >= 90),
                        'last_updated': str(ep_v.get('last_updated') or last_updated)
                    }

        if ep_str and ep_str not in episodes_prog:
            episodes_prog[ep_str] = {
                'time_pos': time_pos,
                'duration': duration,
                'percent': percent,
                'completed': completed,
                'last_updated': last_updated
            }

        return {
            'episode': ep_str,
            'title': title,
            'time_pos': time_pos,
            'duration': duration,
            'percent': percent,
            'completed': completed,
            'poster_url': poster_url,
            'provider': provider,
            'language_mode': language_mode,
            'last_updated': last_updated,
            'episodes_progress': episodes_prog
        }

    def _entry_key(self, anime_id, provider: str = "") -> str:
        """Return the storage key for an anime, preserving legacy bare ids.

        Providers share slugs (e.g. ``naruto`` exists on Arabic and English
        sources), so a provider-qualified key is only introduced once a bare
        key is already owned by a different provider.  Existing history is
        therefore never migrated or lost.
        """
        base = str(anime_id)
        provider = str(provider or "")
        entry = self.history.get(base)
        if isinstance(entry, dict):
            owner = str(entry.get('provider') or '')
            if not provider or not owner or owner == provider:
                return base
            return f"{provider}:{base}"
        if provider:
            composite = f"{provider}:{base}"
            if isinstance(self.history.get(composite), dict):
                return composite
        return base

    def save_history(self):
        try:
            if len(self.history) > self.MAX_HISTORY_SIZE:
                sorted_items = sorted(
                    self.history.items(),
                    key=lambda x: str(x[1].get('last_updated') or ''),
                    reverse=True
                )
                self.history = dict(sorted_items[:self.MAX_HISTORY_SIZE])

            atomic_write_json(self.history_file, self.history, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Warning: Failed to save history: {e}", file=sys.stderr)

    def mark_watched(self, anime_id, episode_num, anime_title, time_pos: float = 0.0, duration: float = 0.0, percent: float = 0.0, poster_url: str = "", provider: str = "", language_mode: str = ""):
        anime_id_str = self._entry_key(anime_id, provider)
        ep_num_str = str(episode_num)

        try:
            t_pos = round(float(time_pos or 0.0), 1)
        except (ValueError, TypeError):
            t_pos = 0.0

        try:
            dur = round(float(duration or 0.0), 1)
        except (ValueError, TypeError):
            dur = 0.0

        if percent <= 0 and dur > 0 and t_pos > 0:
            percent = round((t_pos / dur) * 100, 1)

        try:
            percent_val = max(0, min(100, int(round(float(percent or 0)))))
        except (ValueError, TypeError):
            percent_val = 0

        completed = percent_val >= 90 or (dur > 0 and dur - t_pos < 30)

        entry = self.history.get(anime_id_str, {})
        if not isinstance(entry, dict):
            entry = {}

        episodes_prog = entry.get('episodes_progress', {})
        if not isinstance(episodes_prog, dict):
            episodes_prog = {}

        now_iso = datetime.now().isoformat()
        episodes_prog[ep_num_str] = {
            'time_pos': t_pos,
            'duration': dur,
            'percent': percent_val,
            'completed': completed,
            'last_updated': now_iso
        }

        self.history[anime_id_str] = {
            'episode': ep_num_str,
            'title': str(anime_title or entry.get('title', 'Unknown')),
            'time_pos': t_pos,
            'duration': dur,
            'percent': percent_val,
            'completed': completed,
            'poster_url': str(poster_url or entry.get('poster_url', '')),
            'provider': str(provider or entry.get('provider', '')),
            'language_mode': str(language_mode or entry.get('language_mode', '')),
            'last_updated': now_iso,
            'episodes_progress': episodes_prog
        }
        self.save_history()

    def get_last_watched(self, anime_id, provider: str = "") -> Optional[str]:
        data = self.history.get(self._entry_key(anime_id, provider))
        if isinstance(data, dict):
            ep = data.get('episode')
            return str(ep) if ep is not None else None
        return None

    def get_episode_progress(self, anime_id, episode_num, provider: str = "") -> dict:
        data = self.history.get(self._entry_key(anime_id, provider), {})
        if not isinstance(data, dict):
            return {'time_pos': 0.0, 'duration': 0.0, 'percent': 0, 'completed': False, 'time_str': '00:00'}

        episodes_prog = data.get('episodes_progress', {})
        ep_str = str(episode_num)
        if isinstance(episodes_prog, dict) and ep_str in episodes_prog:
            rec = dict(episodes_prog[ep_str])
            rec['time_str'] = format_seconds(rec.get('time_pos', 0.0))
            return rec

        if str(data.get('episode')) == ep_str:
            t_pos = data.get('time_pos', 0.0)
            return {
                'time_pos': t_pos,
                'duration': data.get('duration', 0.0),
                'percent': data.get('percent', 0),
                'completed': data.get('completed', False),
                'last_updated': data.get('last_updated', ''),
                'time_str': format_seconds(t_pos)
            }
        return {'time_pos': 0.0, 'duration': 0.0, 'percent': 0, 'completed': False, 'time_str': '00:00'}

    def get_all_episodes_progress(self, anime_id, provider: str = "") -> dict:
        data = self.history.get(self._entry_key(anime_id, provider), {})
        if not isinstance(data, dict):
            return {}

        episodes_prog = data.get('episodes_progress', {})
        result = {}
        if isinstance(episodes_prog, dict):
            for ep_k, ep_v in episodes_prog.items():
                if isinstance(ep_v, dict):
                    r = dict(ep_v)
                    r['time_str'] = format_seconds(r.get('time_pos', 0.0))
                    result[str(ep_k)] = r

        if 'episode' in data:
            active_ep = str(data['episode'])
            if active_ep not in result:
                t_pos = data.get('time_pos', 0.0)
                result[active_ep] = {
                    'time_pos': t_pos,
                    'duration': data.get('duration', 0.0),
                    'percent': data.get('percent', 0),
                    'completed': data.get('completed', False),
                    'last_updated': data.get('last_updated', ''),
                    'time_str': format_seconds(t_pos)
                }
        return result

    def remove(self, anime_id, provider: str = ""):
        aid = self._entry_key(anime_id, provider)
        if aid in self.history:
            del self.history[aid]
            self.save_history()

    def get_history(self) -> List[Dict[str, Any]]:
        items = []
        for anime_id, data in self.history.items():
            if not isinstance(data, dict):
                continue
            time_pos = float(data.get('time_pos') or 0.0)
            duration = float(data.get('duration') or 0.0)
            try:
                percent = int(float(data.get('percent') or 0))
            except (ValueError, TypeError):
                percent = 0
            completed = bool(data.get('completed', False)) or (percent >= 90)

            items.append({
                'anime_id': str(anime_id),
                'title': str(data.get('title') or 'Unknown'),
                'episode': str(data.get('episode') or '?'),
                'time_pos': time_pos,
                'duration': duration,
                'percent': percent,
                'completed': completed,
                'time_str': format_seconds(time_pos),
                'duration_str': format_seconds(duration),
                'last_updated': str(data.get('last_updated') or ''),
                'provider': str(data.get('provider') or ''),
                'language_mode': str(data.get('language_mode') or ''),
            })
        items.sort(key=lambda x: x['last_updated'], reverse=True)
        return items