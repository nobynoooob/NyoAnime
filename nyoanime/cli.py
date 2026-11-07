"""
nyoanime CLI Launcher
Fast, native, organic terminal interface with pure fzf and full-screen parity.
Zero details preview split, zero RTL Arabic season glitches.
Interactive action menu on selection: Play, Change Quality, Download, Batch Download.
"""

import sys
import subprocess
import shutil
import time
import re
from pathlib import Path
from typing import Optional, List, Dict, Any

from rich.console import Console
from rich.text import Text

from nyoanime.version import APP_VERSION
from nyoanime.config import DEFAULT_HEADER_ART, THEMES, POPULAR_GENRES
from nyoanime.models import QualityOption, AnimeResult
from nyoanime.providers import ProviderManager
from nyoanime.favorites import FavoritesManager
from nyoanime.downloader import (
    DownloadConfig,
    DownloadPlan,
    download_single_episode,
    execute_batch_download,
    parse_batch_ranges,
    parse_speed_limit,
    prompt_download_options,
)
from nyoanime.providers.language import (
    language_badge,
    mode_audio_lang,
    normalize_language_mode,
    provider_language_mode,
)
from nyoanime.providers.search import search_providers


def clean_season(season_str: Optional[str]) -> str:
    """Removes Arabic RTL characters from season text to prevent terminal alignment glitches."""
    if not season_str:
        return ""
    val = str(season_str).strip()
    if val.upper() in ["0", "N/A", "NONE", "UNKNOWN", "", "?"]:
        return ""
    s = val.replace("خريف", "Fall").replace("ربيع", "Spring").replace("صيف", "Summer").replace("شتاء", "Winter")
    m_year = re.search(r"\b(19\d{2}|20\d{2})\b", s)
    year = m_year.group(0) if m_year else ""
    for term in ["Fall", "Spring", "Summer", "Winter"]:
        if term in s:
            return f"{term} {year}".strip() if year else term
    return year or s


def clean_episodes(ep_str: Optional[str]) -> str:
    """Formats episode count, never displaying 'N/A' or '0'."""
    if not ep_str:
        return ""
    val = str(ep_str).strip()
    if val.upper() in ["?", "0", "N/A", "NONE", "UNKNOWN", ""]:
        return ""
    m = re.search(r"\d+", val)
    if m and int(m.group(0)) > 0:
        return f"{m.group(0)} eps"
    return ""


class NyoAnimeWrapper:
    """Pure native fzf CLI interface for NyoAnime."""

    def __init__(self, api, player, history, settings, rpc):
        self.api = api
        self.player = player
        self.history = history
        self.settings_manager = settings
        self.rpc = rpc
        self.favorites = FavoritesManager()
        self.fzf_available = shutil.which('fzf') is not None
        self.console = Console()
        self.autoplay = bool(self.settings_manager.get("auto_next", False))

    def get_theme_color(self, key: str = "ascii") -> str:
        t_name = self.settings_manager.get("theme", "blue") or "blue"
        theme = THEMES.get(t_name, THEMES.get("auto", THEMES["blue"]))
        return theme.get(key, "#7eb3d4")

    def _get_rpc_status_text(self) -> Text:
        if not self.settings_manager.get('discord_rpc', False):
            return Text("")
        if getattr(self.rpc, 'connected', False):
            return Text("RPC: Connected", style="green")
        return Text("")

    def _launcher(
        self,
        items: List[str],
        prompt_text: str,
        multi: bool = False
    ) -> List[str]:
        """
        Native fzf launcher: pure, fast, organic list selection.
        No preview pane, no terminal split, no key collisions.
        """
        if not items:
            return []

        if not self.fzf_available:
            return self._fallback_launcher(items, prompt_text, multi=multi)

        args = [
            'fzf',
            '--ansi',
            '--reverse',
            '--cycle',
            '--height=85%',
            '--prompt', f"{prompt_text} > ",
            '--bind', 'esc:abort,left:abort,double-click:accept,enter:accept'
        ]

        if multi:
            args.append('-m')

        try:
            result = subprocess.run(
                args,
                input="\n".join(items),
                text=True,
                encoding='utf-8',
                stdout=subprocess.PIPE,
                stderr=None,
                check=False
            )
            if result.returncode != 0:
                return []

            lines = [l.rstrip('\r\n') for l in result.stdout.splitlines() if l.strip()]
            return lines
        except Exception:
            return self._fallback_launcher(items, prompt_text, multi=multi)

    def _fallback_launcher(
        self,
        items: List[str],
        prompt_text: str,
        multi: bool = False
    ) -> List[str]:
        """Native inline numbered menu when fzf is unavailable."""
        self.console.print(f"\n[bold cyan]─ {prompt_text} ─[/bold cyan]")
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        for i, item in enumerate(items, 1):
            clean_item = ansi_escape.sub('', item)
            self.console.print(f"  [cyan]{i:>2}.[/cyan] {clean_item}")

        try:
            self.console.print("\n[dim]Enter selection number (or 'b' to back):[/dim] ╰─> ", end="")
            choice = input().strip()
        except (KeyboardInterrupt, EOFError):
            return []

        if not choice or choice.lower() in ['b', 'back', 'q', 'quit']:
            return []

        selected = []
        for p in choice.split():
            if '-' in p:
                try:
                    s, e = map(int, p.split('-'))
                    for j in range(s, e + 1):
                        if 1 <= j <= len(items):
                            selected.append(items[j - 1])
                except ValueError:
                    pass
            elif p.isdigit():
                idx = int(p) - 1
                if 0 <= idx < len(items):
                    selected.append(items[idx])
        return selected

    def get_quality_preference(self, server_data: Any) -> List[QualityOption]:
        if isinstance(server_data, dict):
            qualities = server_data.get('Qualities')
            if qualities:
                return qualities
            current_ep_data = server_data.get('CurrentEpisode', {})
        else:
            current_ep_data = server_data or {}

        fallback_qualities = [
            QualityOption("1080p", 'FRFhdQ', "info"),
            QualityOption("720p", 'FRLink', "info"),
            QualityOption("480p", 'FRLowQ', "info"),
        ]
        return [q for q in fallback_qualities if current_ep_data.get(q.server_key)]

    def play_video(self, anime: Any, ep: Any, quality_override: Optional[str] = None) -> bool:
        server_data = None
        prov_name = getattr(self.api, 'name', 'current provider')
        with self.console.status(f"[bold blue]Fetching servers for Episode {ep.number}...[/bold blue]"):
            server_data = self.api.get_streaming_servers(anime.id, ep.number, getattr(anime, 'type', 'SERIES'))

        if not server_data:
            self.console.print(f"\n[bold red]✖ No streaming servers found for this episode on {prov_name}.[/bold red]")
            self.console.print("[dim]Tip: You can switch provider from the main menu by pressing 'p'.[/dim]\n")
            time.sleep(1.8)
            return False

        available = self.get_quality_preference(server_data)
        if not available:
            self.console.print(f"\n[bold red]✖ No streamable qualities available on {prov_name}.[/bold red]")
            time.sleep(1.8)
            return False

        selected_q = None
        if quality_override:
            for q in available:
                if quality_override.lower() in getattr(q, 'name', '').lower():
                    selected_q = q
                    break

        if not selected_q:
            default_q = self.settings_manager.get('default_quality', '1080p') or '1080p'
            for q in available:
                if default_q in getattr(q, 'name', ''):
                    selected_q = q
                    break
            if not selected_q:
                selected_q = available[0]

        direct_url = getattr(selected_q, 'direct_url', None)
        if not direct_url and isinstance(server_data, dict):
            current_ep_data = server_data.get('CurrentEpisode', {})
            direct_url = current_ep_data.get(selected_q.server_key)

        if not direct_url or not direct_url.startswith("http"):
            self.console.print("\n[bold red]✖ Failed to resolve direct video link.[/bold red]")
            time.sleep(1.8)
            return False

        provider_id = getattr(self.api, 'id', '') or getattr(self.api, 'name', '')
        start_time = 0.0
        ep_prog = self.history.get_episode_progress(anime.id, ep.display_num, provider=provider_id)
        if ep_prog and 0 < ep_prog.get('percent', 0) < 90 and ep_prog.get('time_pos', 0) > 10:
            start_time = ep_prog['time_pos']
        elif getattr(self.api, 'id', '') == 'anime3rb' or 'vid3rb.com' in direct_url:
            start_time = 5.0

        if self.rpc:
            self.rpc.update_watching(anime.title_en, str(ep.number), getattr(anime, 'thumbnail', ''))

        audio_lang = getattr(selected_q, 'audio_lang', '') or ''
        if not audio_lang:
            configured_mode = self.settings_manager.get('language_mode')
            language_mode = normalize_language_mode(configured_mode) if configured_mode else provider_language_mode(provider_id)
            audio_lang = mode_audio_lang(language_mode)
        else:
            configured_mode = self.settings_manager.get('language_mode')
            language_mode = normalize_language_mode(configured_mode) if configured_mode else provider_language_mode(provider_id)
        headers = getattr(selected_q, 'headers', None) or None
        playback_url = direct_url
        playback_bitrate = 0
        if getattr(selected_q, 'audio_tracks', None) and getattr(selected_q, 'master_url', ''):
            playback_url = selected_q.master_url
            playback_bitrate = int(getattr(selected_q, 'bandwidth', 0) or 0)

        player_type = self.settings_manager.get('player')
        playback_info = self.player.play(
            playback_url,
            f"{anime.title_en} - Episode {ep.number}",
            player_type=player_type,
            start_time=start_time,
            audio_lang=audio_lang or None,
            headers=headers,
            subtitle_url=getattr(selected_q, 'subtitle_url', '') or None,
            subtitle_lang=getattr(selected_q, 'subtitle_lang', '') or None,
            audio_url=getattr(selected_q, 'audio_url', '') or None,
            hls_bitrate=playback_bitrate or None,
        )

        playback_info = playback_info if isinstance(playback_info, dict) else {}
        position_reported = bool(playback_info.get('position_reported', False))
        pos = playback_info.get('time_pos', 0.0) if position_reported else 0.0
        dur = playback_info.get('duration', 0.0) if position_reported else 0.0
        pct = playback_info.get('percent', 0.0) if position_reported else 0.0

        self.history.mark_watched(
            anime.id,
            ep.display_num,
            anime.title_en,
            time_pos=pos,
            duration=dur,
            percent=pct,
            poster_url=getattr(anime, 'thumbnail', ''),
            provider=provider_id,
            language_mode=language_mode,
        )
        return True

    def download_video(self, anime: Any, ep: Any, quality_override: Optional[str] = None) -> bool:
        with self.console.status(f"[bold blue]Resolving download link for Episode {ep.number}...[/bold blue]"):
            server_data = self.api.get_streaming_servers(anime.id, ep.number, getattr(anime, 'type', 'SERIES'))

        if not server_data:
            prov_name = getattr(self.api, 'name', 'current provider')
            self.console.print(f"\n[bold red]✖ No download links found for this episode on {prov_name}.[/bold red]")
            self.console.print("[dim]Tip: You can switch provider from the main menu by pressing 'p'.[/dim]\n")
            time.sleep(1.8)
            return False

        available = self.get_quality_preference(server_data)
        if not available:
            self.console.print(f"\n[bold red]✖ No suitable download quality found on {prov_name}.[/bold red]")
            time.sleep(1.8)
            return False

        selected_q = available[0]
        if quality_override:
            for q in available:
                if quality_override.lower() in getattr(q, 'name', '').lower():
                    selected_q = q
                    break

        direct_url = getattr(selected_q, 'direct_url', None)
        if not direct_url and isinstance(server_data, dict):
            current_ep_data = server_data.get('CurrentEpisode', {})
            direct_url = current_ep_data.get(selected_q.server_key)

        if not direct_url or not direct_url.startswith("http"):
            self.console.print("[red]Failed to extract direct download link.[/red]")
            return False

        q_tag = getattr(selected_q, 'name', 'HD')
        match = re.search(r"\b(\d{3,4}p)\b", str(q_tag or ""))
        if match:
            q_tag = match.group(1)
        ext = str(getattr(selected_q, 'ext', 'mp4') or 'mp4').lstrip('.')
        d_dir = self.settings_manager.get('download_directory', 'downloads') or 'downloads'
        headers = getattr(selected_q, 'headers', None) or {}
        config = DownloadConfig(
            save_dir=Path(d_dir).expanduser(),
            quality=q_tag,
            speed_limit_bytes=parse_speed_limit(self.settings_manager.get('download_speed_limit')),
            subtitle_lang=getattr(selected_q, 'subtitle_lang', '') or None,
            provider=getattr(self.api, 'id', '') or getattr(self.api, 'name', ''),
        )
        configured_mode = self.settings_manager.get('language_mode')
        config.language_mode = (
            normalize_language_mode(configured_mode)
            if configured_mode
            else provider_language_mode(config.provider)
        )
        plan = DownloadPlan(
            url=direct_url,
            headers=dict(headers),
            quality=q_tag,
            ext=ext,
            subtitle_url=getattr(selected_q, 'subtitle_url', '') or '',
            subtitle_lang=getattr(selected_q, 'subtitle_lang', '') or '',
            subtitles=list(getattr(selected_q, 'subtitles', None) or []),
            master_url=getattr(selected_q, 'master_url', '') or '',
            audio_lang=getattr(selected_q, 'audio_lang', '') or '',
        )
        result = download_single_episode(
            api=self.api,
            anime=anime,
            ep=ep,
            config=config,
            console=self.console,
            plan=plan,
        )
        if not result.get("success"):
            self.console.print(f"\n[bold red]✖ Download failed:[/bold red] {result.get('error') or 'unknown error'}")
            return False

        provider_id = getattr(self.api, 'id', '') or getattr(self.api, 'name', '')
        configured_mode = self.settings_manager.get('language_mode')
        self.history.mark_watched(
            anime.id,
            ep.display_num,
            anime.title_en,
            poster_url=getattr(anime, 'thumbnail', ''),
            provider=provider_id,
            language_mode=(
                normalize_language_mode(configured_mode)
                if configured_mode
                else provider_language_mode(provider_id)
            ),
        )
        return True

    def batch_download(self, anime: Any, episodes: List[Any]):
        self.console.print(f"\n[bold cyan]Batch Download: {anime.title_en}[/bold cyan]")
        ep_values = [int(e.display_num) for e in episodes if str(e.display_num).isdigit()]
        first_ep = min(ep_values, default=1)
        last_ep = max(ep_values, default=len(episodes))
        self.console.print(f"[dim]Total available episodes: {first_ep} to {last_ep}[/dim]")
        try:
            prompt = self.console.input("Enter range (e.g. 1-12, 1 3 5) or 'all': ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return

        if not prompt:
            return

        if prompt in ('all', '*'):
            target_eps = episodes
        else:
            # Bound by the highest episode number, not the count: a season can start at ep 13.
            nums = set(parse_batch_ranges(prompt, max_eps=last_ep))
            target_eps = [e for e in episodes if str(e.display_num).isdigit() and int(e.display_num) in nums]

        if not target_eps:
            self.console.print("[yellow]No matching episodes specified.[/yellow]")
            return

        # Prompt user for download directory, parallel workers, speed limits, and quality
        default_dir = self.settings_manager.get('download_directory', 'downloads') or 'downloads'
        default_q = self.settings_manager.get('default_download_quality', '1080p') or '1080p'
        config = prompt_download_options(self.console, default_dir=default_dir, default_quality=default_q, batch_mode=True)
        if not config:
            return

        config.provider = getattr(self.api, 'id', '') or getattr(self.api, 'name', '')
        configured_mode = self.settings_manager.get('language_mode')
        config.language_mode = (
            normalize_language_mode(configured_mode)
            if configured_mode
            else provider_language_mode(config.provider)
        )

        execute_batch_download(
            api=self.api,
            anime=anime,
            episodes=target_eps,
            config=config,
            console=self.console,
            history_manager=self.history
        )

    def _search_all_providers(self, query: str) -> List[AnimeResult]:
        configured = self.settings_manager.get('language_mode')
        provider_id = getattr(self.api, 'id', '')
        mode = (
            normalize_language_mode(configured)
            if configured
            else provider_language_mode(provider_id)
        )
        return search_providers(query, mode=mode, timeout=5.0)

    def _process_anime_list(self, results: List[Any], title: str = "Select Anime"):
        if not results:
            return

        anime_map: Dict[str, Any] = {}
        display_lines: List[str] = []

        clean_titles = []
        for res in results:
            t = res.title_en or getattr(res, 'title', 'Unknown')
            if len(t) > 55:
                t = t[:52] + "..."
            clean_titles.append(t)

        max_len = max((len(t) for t in clean_titles), default=30)
        max_len = min(50, max_len)

        for idx, res in enumerate(results):
            t_str = clean_titles[idx]
            padding = " " * max(1, (max_len - len(t_str) + 3))

            meta = []
            ep_clean = clean_episodes(getattr(res, 'episodes', ''))
            if ep_clean:
                meta.append(ep_clean)

            season_clean = clean_season(getattr(res, 'premiered', ''))
            if season_clean:
                meta.append(season_clean)

            language = str(getattr(res, 'language', '') or '')
            if language == 'dub':
                meta.append('DUB')
            elif language == 'sub':
                meta.append('SUB')

            meta_str = ""
            if meta:
                meta_inner = " \033[90m|\033[0m ".join(meta)
                meta_str = f"\033[90m[\033[0m {meta_inner} \033[90m]\033[0m"

            score_str = ""
            score = getattr(res, 'score', '')
            if score and str(score) not in ["0", "N/A", "None", "", "?"]:
                score_str = f"  \033[33m★ {score}\033[0m"

            fav_str = " \033[35m♥\033[0m" if self.favorites.is_favorite(res.id) else ""

            line = f"\033[36m{t_str}\033[0m{padding}{meta_str}{score_str}{fav_str}"
            display_lines.append(line)
            anime_map[line] = res
            anime_map[line.strip()] = res

            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            clean_line = ansi_escape.sub('', line)
            anime_map[clean_line] = res
            anime_map[clean_line.strip()] = res

        while True:
            selected_lines = self._launcher(display_lines, title)
            if not selected_lines:
                break

            sel_text = selected_lines[0]
            clean = ansi_escape.sub('', sel_text).strip()
            selected_anime = (
                anime_map.get(sel_text) or
                anime_map.get(sel_text.strip()) or
                anime_map.get(clean)
            )

            if not selected_anime:
                for idx, res in enumerate(results):
                    if clean_titles[idx] in clean:
                        selected_anime = res
                        break

            if not selected_anime:
                continue

            target_api = self.api
            provider_id = getattr(selected_anime, "provider_id", "")
            if provider_id:
                target_api = ProviderManager.get_provider(provider_id)
            previous_api = self.api
            self.api = target_api
            try:
                self._select_episodes(selected_anime)
            finally:
                if self.api is target_api:
                    self.api = previous_api

    def _select_episodes(self, anime: Any):
        if self.rpc:
            self.rpc.update_viewing_anime(anime.title_en, getattr(anime, 'thumbnail', ''))

        if getattr(anime, "_from_latest", False):
            refresh_cache = getattr(self.api, "refresh_anime_cache", None)
            if callable(refresh_cache):
                refresh_cache(anime.id)

        with self.console.status(f"[bold blue]Fetching episodes for {anime.title_en}...[/bold blue]"):
            episodes = self.api.get_episodes(anime.id)

        if not episodes:
            self.console.print("[bold red]No episodes found.[/bold red]")
            time.sleep(1.0)
            return

        provider_id = getattr(self.api, 'id', '') or getattr(self.api, 'name', '')
        last_watched = self.history.get_last_watched(anime.id, provider=provider_id)

        while True:
            ep_map: Dict[str, Any] = {}
            ep_lines: List[str] = []

            for ep in episodes:
                prog = self.history.get_episode_progress(anime.id, ep.display_num, provider=provider_id)
                pct = prog.get('percent', 0)
                is_last = str(ep.number) == str(last_watched)

                tags = []
                if is_last:
                    tags.append("\033[33m★ Recent\033[0m")
                if prog.get('completed'):
                    tags.append(f"\033[32m[✓ {pct}%]\033[0m")
                elif pct > 0:
                    tags.append(f"\033[36m[{pct}%]\033[0m")

                tags_str = ("  " + " ".join(tags)) if tags else ""
                line = f"\033[1mEpisode {str(ep.number):>3}\033[0m{tags_str}"
                ep_lines.append(line)

                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                clean_line = ansi_escape.sub('', line)
                ep_map[line] = ep
                ep_map[line.strip()] = ep
                ep_map[clean_line] = ep
                ep_map[clean_line.strip()] = ep
                ep_map[str(ep.number)] = ep
                ep_map[str(ep.display_num)] = ep

            prompt = f"{anime.title_en} (1-{len(episodes)})"
            selected_lines = self._launcher(ep_lines, prompt, multi=True)

            if not selected_lines:
                break

            queue = []
            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            for line in selected_lines:
                clean = ansi_escape.sub('', line).strip()
                ep_obj = None

                # 1. Regex match for episode number: "Episode   1", "Episode 12", "Episode 1.5", etc.
                m = re.search(r'Episode\s+([0-9.]+)', clean, re.IGNORECASE)
                if m:
                    num_str = m.group(1)
                    ep_obj = next((e for e in episodes if str(e.number) == num_str or str(e.display_num) == num_str), None)

                # 2. Map lookup fallback
                if not ep_obj:
                    ep_obj = ep_map.get(line) or ep_map.get(line.strip()) or ep_map.get(clean)

                if ep_obj:
                    queue.append(ep_obj)

            if not queue:
                continue

            # Interactive action menu for selected episode(s)
            ep_label = f"Episode {queue[0].display_num}" if len(queue) == 1 else f"{len(queue)} Episodes"
            action_opts = [
                "▶ Play",
                "⚙ Change Quality",
                "📥 Download",
                "📦 Batch Download",
                "♥ Favorite Toggle",
                "← Back"
            ]

            action_sel = self._launcher(action_opts, f"{anime.title_en} • {ep_label}")
            if not action_sel:
                continue

            act = action_sel[0]

            if "Back" in act:
                continue

            if "Favorite" in act:
                if self.favorites.is_favorite(anime.id):
                    self.favorites.remove(anime.id)
                    self.console.print(f"[dim]Removed from favorites: {anime.title_en}[/dim]")
                else:
                    self.favorites.add(anime.id, anime.title_en, getattr(anime, 'thumbnail', ''))
                    self.console.print(f"[magenta]♥ Added to favorites: {anime.title_en}[/magenta]")
                time.sleep(0.5)
                continue

            if "Batch" in act:
                self.batch_download(anime, episodes)
                continue

            if "Download" in act:
                for q_ep in queue:
                    self.download_video(anime, q_ep)
                continue

            current_quality = None
            if "Quality" in act:
                # Fetch available qualities to let user pick
                with self.console.status("[bold blue]Resolving stream qualities...[/bold blue]"):
                    s_data = self.api.get_streaming_servers(anime.id, queue[0].number, getattr(anime, 'type', 'SERIES'))
                avail_q = self.get_quality_preference(s_data) if s_data else []
                if avail_q:
                    q_choices = [getattr(q, 'name', 'HD') for q in avail_q]
                    q_sel = self._launcher(q_choices, "Select Stream Quality")
                    if q_sel:
                        current_quality = q_sel[0]
                        sub_act = self._launcher(["▶ Play with " + current_quality, "📥 Download with " + current_quality, "← Back"], "Action")
                        if sub_act and "Download" in sub_act[0]:
                            for q_ep in queue:
                                self.download_video(anime, q_ep, quality_override=current_quality)
                            continue
                        elif not sub_act or "Back" in sub_act[0]:
                            continue
                else:
                    self.console.print("[yellow]Could not pre-fetch quality options. Playing with default...[/yellow]")
                    time.sleep(0.8)

            # Playback Loop
            played_idx = 0

            while played_idx < len(queue):
                ep = queue[played_idx]
                played_ok = self.play_video(anime, ep, current_quality)
                played_idx += 1

                if not played_ok:
                    break

                if played_idx < len(queue):
                    continue

                if self.autoplay:
                    next_ep_num = self._get_next_ep_num(episodes, ep)
                    if next_ep_num:
                        next_ep = next((e for e in episodes if e.number == next_ep_num), None)
                        if next_ep:
                            self.console.print(f"[cyan]Autoplaying Episode {next_ep.number}...[/cyan]")
                            queue = [next_ep]
                            played_idx = 0
                            continue

                # Interactive post-playback menu
                action_opts = ["▶ Next Episode", "🔁 Replay", "◀ Previous Episode", "📋 Select Episode", "← Back"]
                act_lines = self._launcher(action_opts, f"Episode {ep.number} Finished")
                if act_lines:
                    act_post = act_lines[0]
                    if "Next" in act_post:
                        next_ep_num = self._get_next_ep_num(episodes, ep)
                        if next_ep_num:
                            next_ep = next((e for e in episodes if e.number == next_ep_num), None)
                            if next_ep:
                                queue = [next_ep]
                                played_idx = 0
                                continue
                    elif "Replay" in act_post:
                        queue = [ep]
                        played_idx = 0
                        continue
                    elif "Previous" in act_post:
                        prev_ep_num = self._get_prev_ep_num(episodes, ep)
                        if prev_ep_num:
                            prev_ep = next((e for e in episodes if e.number == prev_ep_num), None)
                            if prev_ep:
                                queue = [prev_ep]
                                played_idx = 0
                                continue
                    elif "Select" in act_post:
                        break
                break

    def _get_next_ep_num(self, episodes: List[Any], current_ep: Any) -> Optional[str]:
        # Prefer list order: handles specials such as 6.5 and multi-season
        # numbering without assuming numeric contiguity.
        try:
            index = episodes.index(current_ep)
            if index + 1 < len(episodes):
                return str(episodes[index + 1].number)
        except (ValueError, AttributeError):
            pass
        try:
            cur = float(current_ep.number)
            for ep in episodes:
                if float(ep.number) == cur + 1:
                    return str(ep.number)
        except Exception:
            pass
        return None

    def _get_prev_ep_num(self, episodes: List[Any], current_ep: Any) -> Optional[str]:
        try:
            index = episodes.index(current_ep)
            if index > 0:
                return str(episodes[index - 1].number)
        except (ValueError, AttributeError):
            pass
        try:
            cur = float(current_ep.number)
            if cur > 1:
                for ep in episodes:
                    if float(ep.number) == cur - 1:
                        return str(ep.number)
        except Exception:
            pass
        return None

    def _show_history(self):
        items = self.history.get_history()
        if not items:
            self.console.print("\n[yellow]Watch history is empty.[/yellow]")
            time.sleep(1.0)
            return

        lines = []
        map_h = {}
        for h in items:
            t = h.get('title', 'Unknown')
            ep_n = h.get('episode') or h.get('last_episode') or '?'
            pct = h.get('percent', 0)
            pct_str = f"[✓ {pct}%]" if pct >= 90 else (f"[{pct}%]" if pct > 0 else "")
            badge = language_badge(h.get('language_mode')) if h.get('language_mode') else ''
            badge_str = f" \033[35m[{badge}]\033[0m" if badge else ""
            line = f"\033[36m{t[:50]}\033[0m{badge_str}  \033[33mEp {ep_n}\033[0m  \033[32m{pct_str}\033[0m"
            lines.append(line)
            map_h[line] = h

            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            clean_line = ansi_escape.sub('', line)
            map_h[clean_line] = h

        sel = self._launcher(lines, "Watch History")
        if sel:
            chosen = map_h.get(sel[0]) or map_h.get(re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', sel[0]))
            if chosen:
                item_id = str(chosen.get('anime_id', ''))
                item_provider = str(chosen.get('provider') or '')
                if item_provider:
                    canonical = ProviderManager.get_canonical_id(item_provider)
                    if item_id.startswith(f"{canonical}:"):
                        item_id = item_id.split(":", 1)[1]
                    if canonical and canonical != getattr(self.api, 'id', ''):
                        ProviderManager.set_active_provider(canonical)
                        self.settings_manager.set('anime_provider', canonical)
                        self.settings_manager.set('language_mode', provider_language_mode(canonical))
                        self.api = ProviderManager.get_provider(canonical)
                mock_anime = AnimeResult(
                    id=item_id,
                    title_en=chosen.get('title', ''),
                    title_jp='',
                    type='SERIES',
                    episodes='',
                    status='',
                    genres='',
                    mal_id='0',
                    relation_id='',
                    score='',
                    rank='',
                    popularity='',
                    rating='',
                    premiered='',
                    creators='',
                    duration='',
                    thumbnail=chosen.get('poster_url', '')
                )
                self._select_episodes(mock_anime)

    def _show_favorites(self):
        favs = self.favorites.get_favorites()
        if not favs:
            self.console.print("\n[yellow]No favorite anime saved yet.[/yellow]")
            time.sleep(1.0)
            return

        lines = []
        map_f = {}
        for f in favs:
            t = f.get('title', 'Unknown')
            line = f"\033[35m♥\033[0m \033[36m{t[:55]}\033[0m"
            lines.append(line)
            map_f[line] = f

            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            clean_line = ansi_escape.sub('', line)
            map_f[clean_line] = f

        sel = self._launcher(lines, "Favorites")
        if sel:
            chosen = map_f.get(sel[0]) or map_f.get(re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', sel[0]))
            if chosen:
                mock_anime = AnimeResult(
                    id=chosen.get('id', ''),
                    title_en=chosen.get('title', ''),
                    title_jp='',
                    type='SERIES',
                    episodes='',
                    status='',
                    genres='',
                    mal_id='0',
                    relation_id='',
                    score='',
                    rank='',
                    popularity='',
                    rating='',
                    premiered='',
                    creators='',
                    duration='',
                    thumbnail=chosen.get('thumbnail', '')
                )
                self._select_episodes(mock_anime)

    def _switch_provider(self):
        provs = ProviderManager.list_providers()
        current_id = ProviderManager.get_active_provider_id()
        p_lines = []
        p_map = {}
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        for p in provs:
            active_badge = " \033[32m[Active]\033[0m" if p['id'] == current_id else ""
            desc = f" ({p['description']})" if p.get('description') else ""
            line = f"\033[1m{p['name']}\033[0m [{p['id']}]{active_badge}{desc}"
            p_lines.append(line)
            clean_line = ansi_escape.sub('', line)
            p_map[line] = p['id']
            p_map[clean_line] = p['id']
            p_map[p['id']] = p['id']

        sel = self._launcher(p_lines, "Select Provider")
        if sel:
            clean = ansi_escape.sub('', sel[0]).strip()
            p_id = p_map.get(sel[0]) or p_map.get(clean)
            if not p_id:
                m = re.search(r'\[([a-zA-Z0-9_-]+)\]', clean)
                if m:
                    p_id = m.group(1)
            if p_id:
                from .providers.language import provider_language_mode
                canonical = ProviderManager.get_canonical_id(p_id)
                ProviderManager.set_active_provider(canonical)
                self.api = ProviderManager.get_provider(canonical)
                self.settings_manager.set("anime_provider", canonical)
                self.settings_manager.set("language_mode", provider_language_mode(canonical))
                self.settings_manager.set("auto_provider", False)
                self.console.print(f"\n[green]✔ Switched active provider to: {self.api.name}[/green]")
                time.sleep(0.8)

    def _switch_language(self):
        from .providers.language import LANGUAGE_MODES, language_mode_label

        current = self.settings_manager.get("language_mode")
        lines = []
        mapping = {}
        for mode in LANGUAGE_MODES:
            badge = " \033[32m[Active]\033[0m" if mode == current else ""
            label = language_mode_label(mode)
            line = f"\033[1m{label}\033[0m [{mode}]{badge}"
            lines.append(line)
            mapping[line] = mode
            mapping[label] = mode
            mapping[mode] = mode

        selected = self._launcher(lines, "Preferred Anime Language")
        if not selected:
            return
        mode = mapping.get(selected[0])
        if not mode:
            match = re.search(r"\[([a-z_]+)\]", selected[0])
            mode = match.group(1) if match else None
        if not mode:
            return

        settings = self.settings_manager
        settings.set("language_mode", mode)
        provider_id = ProviderManager.resolve_active_provider(
            settings, mode=mode, preferred_provider=settings.get("anime_provider"), auto=True
        )
        settings.set("anime_provider", provider_id)
        settings.set("auto_provider", True)
        ProviderManager.set_active_provider(provider_id)
        self.api = ProviderManager.get_provider(provider_id)
        self.console.print(
            f"\n[green]✔ Switched language to: {language_mode_label(mode)} ({self.api.name})[/green]"
        )
        time.sleep(0.8)

    def _show_settings_menu(self):
        """Native fzf settings dialog for -i CLI mode."""
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        while True:
            cur_prov = getattr(self.api, 'name', self.settings_manager.get('anime_provider', 'anime3rb'))
            cur_player = self.settings_manager.get('player', 'mpv')
            cur_stream_q = self.settings_manager.get('default_quality', '1080p')
            cur_dl_q = self.settings_manager.get('default_download_quality', '1080p')
            cur_dl_dir = self.settings_manager.get('download_directory', 'downloads')
            cur_auto = "ON" if self.settings_manager.get('auto_next', False) else "OFF"
            cur_rpc = "ON" if self.settings_manager.get('discord_rpc', True) else "OFF"
            cur_theme = self.settings_manager.get('theme', 'auto')
            from .providers.language import language_mode_label
            cur_lang = language_mode_label(self.settings_manager.get('language_mode'))
            cur_auto_provider = "ON" if self.settings_manager.get('auto_provider', True) else "OFF"

            options = [
                f"\033[36m1. Provider:\033[0m           {cur_prov}",
                f"\033[36m2. Video Player:\033[0m       {cur_player}",
                f"\033[36m3. Streaming Quality:\033[0m  {cur_stream_q}",
                f"\033[36m4. Download Quality:\033[0m   {cur_dl_q}",
                f"\033[36m5. Download Directory:\033[0m {cur_dl_dir}",
                f"\033[36m6. Autoplay Next:\033[0m      {cur_auto}",
                f"\033[36m7. Discord RPC:\033[0m        {cur_rpc}",
                f"\033[36m8. Theme:\033[0m              {cur_theme}",
                f"\033[36m9. Language:\033[0m           {cur_lang}",
                f"\033[36m10. Auto Provider:\033[0m      {cur_auto_provider}",
                "← Back to Main Menu"
            ]

            sel = self._launcher(options, "Settings & Preferences")
            if not sel or "Back" in sel[0]:
                break

            choice = ansi_escape.sub('', sel[0])
            if "Auto Provider:" in choice:
                new_auto = not bool(self.settings_manager.get("auto_provider", True))
                self.settings_manager.set("auto_provider", new_auto)
                if new_auto:
                    provider_id = ProviderManager.resolve_active_provider(self.settings_manager, auto=True)
                    self.settings_manager.set("anime_provider", provider_id)
                    ProviderManager.set_active_provider(provider_id)
                    self.api = ProviderManager.get_provider(provider_id)
                self.console.print(
                    f"\nAuto Provider: {'[green]ON[/green]' if new_auto else '[yellow]OFF[/yellow]'}"
                )
                time.sleep(0.6)
            elif "Language:" in choice:
                self._switch_language()
            elif "Provider:" in choice:
                self._switch_provider()
            elif "Video Player:" in choice:
                players = ["mpv", "vlc", "celluloid", "iina", "syncplay", "mpc-hc"]
                active_tag = "\033[32m[Active]\033[0m"
                p_lines = [f"{p} {active_tag if p == cur_player else ''}".strip() for p in players]
                p_sel = self._launcher(p_lines, "Select Video Player")
                if p_sel:
                    chosen_p = ansi_escape.sub('', p_sel[0]).split()[0]
                    self.settings_manager.set("player", chosen_p)
                    self.console.print(f"[green]✔ Set Video Player to: {chosen_p}[/green]")
                    time.sleep(0.6)
            elif "Streaming Quality:" in choice:
                qualities = ["1080p", "720p", "480p", "360p", "auto"]
                active_tag = "\033[32m[Active]\033[0m"
                q_lines = [f"{q} {active_tag if q == cur_stream_q else ''}".strip() for q in qualities]
                q_sel = self._launcher(q_lines, "Select Default Streaming Quality")
                if q_sel:
                    chosen_q = ansi_escape.sub('', q_sel[0]).split()[0]
                    self.settings_manager.set("default_quality", chosen_q)
                    self.console.print(f"[green]✔ Set Default Streaming Quality to: {chosen_q}[/green]")
                    time.sleep(0.6)
            elif "Download Quality:" in choice:
                qualities = ["1080p", "720p", "480p", "360p"]
                active_tag = "\033[32m[Active]\033[0m"
                q_lines = [f"{q} {active_tag if q == cur_dl_q else ''}".strip() for q in qualities]
                q_sel = self._launcher(q_lines, "Select Default Download Quality")
                if q_sel:
                    chosen_q = ansi_escape.sub('', q_sel[0]).split()[0]
                    self.settings_manager.set("default_download_quality", chosen_q)
                    self.console.print(f"[green]✔ Set Default Download Quality to: {chosen_q}[/green]")
                    time.sleep(0.6)
            elif "Download Directory:" in choice:
                self.console.print(f"\n[cyan]Current download directory:[/cyan] [bold]{cur_dl_dir}[/bold]")
                try:
                    new_dir = input("Enter new path (or press Enter to keep current): ").strip()
                    if new_dir:
                        self.settings_manager.set("download_directory", new_dir)
                        self.console.print(f"[green]✔ Download directory updated to: {new_dir}[/green]")
                        time.sleep(0.6)
                except (KeyboardInterrupt, EOFError):
                    pass
            elif "Autoplay Next:" in choice:
                self._toggle_autoplay()
            elif "Discord RPC:" in choice:
                current_rpc = bool(self.settings_manager.get("discord_rpc", True))
                new_rpc = not current_rpc
                self.settings_manager.set("discord_rpc", new_rpc)
                if not new_rpc and self.rpc and getattr(self.rpc, 'connected', False):
                    try:
                        self.rpc.close()
                    except Exception:
                        pass
                elif new_rpc and self.rpc and not getattr(self.rpc, 'connected', False):
                    try:
                        self.rpc.connect()
                    except Exception:
                        pass
                status_str = "[green]Enabled[/green]" if new_rpc else "[yellow]Disabled[/yellow]"
                self.console.print(f"\nDiscord RPC is now: {status_str}")
                time.sleep(0.6)
            elif "Theme:" in choice:
                from . import config as config_mod
                theme_options = ["auto"] + [t for t in THEMES if t != "auto"]
                active_tag = "\033[32m[Active]\033[0m"
                t_lines = [f"{t} {active_tag if t == cur_theme else ''}".strip() for t in theme_options]
                t_sel = self._launcher(t_lines, "Select Color Theme")
                if t_sel:
                    chosen_t = ansi_escape.sub('', t_sel[0]).split()[0]
                    self.settings_manager.set("theme", chosen_t)
                    import importlib
                    importlib.reload(config_mod)
                    for mod_name, mod in list(sys.modules.items()):
                        if mod is None or mod is config_mod:
                            continue
                        if not (mod_name == "nyoanime" or mod_name.startswith('nyoanime.') or mod_name == '__main__'):
                            continue
                        for attr_name in dir(config_mod):
                            if attr_name.startswith('COLOR_') or attr_name in [
                                'HEADER_ART', 'GOODBYE_ART', 'THEMES', 'selected_theme',
                            ]:
                                if hasattr(mod, attr_name):
                                    setattr(mod, attr_name, getattr(config_mod, attr_name))
                    self.console.print(f"[green]✔ Theme switched to: {chosen_t}[/green]")
                    time.sleep(0.6)

    def _toggle_autoplay(self):
        self.autoplay = not self.autoplay
        self.settings_manager.set("auto_next", self.autoplay)
        status_str = "[green]Enabled[/green]" if self.autoplay else "[yellow]Disabled[/yellow]"
        self.console.print(f"\nAutoplay Next Episode is now: {status_str}")
        time.sleep(0.8)

    def _print_header(self):
        title_col = self.get_theme_color("title")
        border_col = self.get_theme_color("border")
        width = self.console.width

        if width >= 64:
            for idx, line in enumerate(DEFAULT_HEADER_ART.strip("\n").splitlines()):
                col = title_col if idx == 0 else border_col
                self.console.print(f" [bold {col}]{line}[/bold {col}]")
        else:
            self.console.print(f"  [bold {title_col}]NYO ANIME[/bold {title_col}]")

        active_prov = getattr(self.api, 'name', 'Anime3rb')
        for suffix in (" (English Sub)", " (English Dub)", " (English)", " Provider"):
            active_prov = active_prov.replace(suffix, "")
        active_prov = active_prov.strip()
        from .providers.language import language_mode_label
        mode_label = language_mode_label(self.settings_manager.get("language_mode"))

        meta = Text("  [ ", style=f"dim {border_col}")
        meta.append(APP_VERSION, style=f"bold {title_col}")
        meta.append(" • ", style=f"dim {border_col}")
        meta.append("by NoBy", style="dim")
        meta.append(" • ", style=f"dim {border_col}")
        meta.append(f"{active_prov} • {mode_label}", style=f"bold {title_col}")
        meta.append(" ]", style=f"dim {border_col}")
        self.console.print(meta)

    def run(self, query: Optional[str] = None) -> Optional[str]:
        border_color = self.get_theme_color("border")

        print()
        self._print_header()

        while True:
            if '-i' not in sys.argv:
                cols = shutil.get_terminal_size().columns
                if cols >= 64:
                    return "SWITCH_TO_TUI"

            if not query:
                rpc_text = self._get_rpc_status_text()
                if rpc_text.plain:
                    self.console.print(Text("  ") + rpc_text)

                margin = "  "
                prov_shortcuts = []
                supports_fn = getattr(self.api, "supports", lambda f: True)
                if supports_fn("latest"):
                    prov_shortcuts.append("[cyan]E[/cyan]: Latest")
                if supports_fn("movies"):
                    prov_shortcuts.append("[cyan]M[/cyan]: Movies")
                if supports_fn("trending"):
                    prov_shortcuts.append("[cyan]T[/cyan]: Trending")
                if supports_fn("popular"):
                    prov_shortcuts.append("[cyan]P[/cyan]: Popular")
                if supports_fn("top_rated"):
                    prov_shortcuts.append("[cyan]R[/cyan]: Top Rated")
                if supports_fn("genres"):
                    prov_shortcuts.append("[cyan]G[/cyan]: Genres")
                if supports_fn("studios"):
                    prov_shortcuts.append("[cyan]S[/cyan]: Studios")

                for i in range(0, len(prov_shortcuts), 5):
                    self.console.print(margin + "  ".join(prov_shortcuts[i:i+5]), style="dim")

                self.console.print(
                    margin + "[cyan]F[/cyan]: Favs    [cyan]H[/cyan]: History  [cyan]C[/cyan]: Provider",
                    style="dim"
                )
                self.console.print(
                    margin + f"[cyan]A[/cyan]: Autoplay ({'ON' if self.autoplay else 'OFF'})  [cyan]O[/cyan]: Settings  [cyan]Q[/cyan]: Quit",
                    style="dim"
                )
                print()

                try:
                    self.console.print(f"  [{border_color}]╭─  Search[/{border_color}]")
                    self.console.print(f"  [{border_color}]╰─>[/{border_color}] ", end="")
                    query = input().strip()
                except (KeyboardInterrupt, EOFError):
                    print()
                    return None

                if not query:
                    continue

                if query.lower() in ['q', 'quit', 'exit']:
                    return None

            cmd = query.lower()
            query = None

            if cmd == 'e' and supports_fn('latest'):
                with self.console.status("[bold blue]Fetching latest episodes...[/bold blue]"):
                    eps = self.api.get_latest_episodes(limit=50)
                if eps:
                    provider_id = str(getattr(self.api, 'id', '') or '')
                    latest_language = ''
                    if provider_id.startswith('stardima'):
                        latest_language = str(self.settings_manager.get('stardima_language', 'dub') or '')
                        if latest_language == 'both':
                            latest_language = ''
                    latest_content = str(getattr(self.api, 'content_scope', '') or '')
                    animes = [
                        AnimeResult(
                            id=e.anime_id or e.url,
                            title_en=e.title or f"Episode {e.number}",
                            title_jp='',
                            type='SERIES',
                            episodes='',
                            status='',
                            genres='',
                            mal_id='0',
                            relation_id='',
                            score='',
                            rank='',
                            popularity='',
                            rating='',
                            premiered='',
                            creators='',
                            duration=e.duration,
                            thumbnail=e.thumbnail,
                            language=latest_language,
                            content_type=latest_content,
                        ) for e in eps
                    ]
                    for latest_anime in animes:
                        latest_anime._from_latest = True
                    self._process_anime_list(animes, "Latest Episodes")
                continue

            if cmd == 'm' and supports_fn('movies'):
                with self.console.status("[bold blue]Fetching movies...[/bold blue]"):
                    movies = self.api.get_movies(limit=40)
                if movies:
                    self._process_anime_list(movies, "Anime Movies")
                continue

            if cmd == 't' and supports_fn('trending'):
                with self.console.status("[bold blue]Fetching trending anime...[/bold blue]"):
                    trending = self.api.get_trending_anime(limit=40)
                if trending:
                    self._process_anime_list(trending, "Trending Anime")
                continue

            if cmd == 'p' and supports_fn('popular'):
                with self.console.status("[bold blue]Fetching popular anime...[/bold blue]"):
                    pinned = self.api.get_pinned_anime(limit=40)
                if pinned:
                    self._process_anime_list(pinned, "Popular Anime")
                continue

            if cmd == 'r' and supports_fn('top_rated'):
                with self.console.status("[bold blue]Fetching top rated anime...[/bold blue]"):
                    top = self.api.get_top_rated_anime(limit=40)
                if top:
                    self._process_anime_list(top, "Top Rated Anime")
                continue

            if cmd == 'f':
                self._show_favorites()
                continue

            if cmd == 'h':
                self._show_history()
                continue

            if cmd == 'a':
                self._toggle_autoplay()
                continue

            if cmd == 'c':
                self._switch_provider()
                continue

            if cmd in ['o', 'settings', 'config']:
                self._show_settings_menu()
                continue

            if cmd == 'g' and supports_fn('genres'):
                genres = POPULAR_GENRES
                sel_g = self._launcher(genres, "Select Genre")
                if sel_g:
                    g_slug = sel_g[0].lower().replace(" ", "-")
                    with self.console.status(f"[bold blue]Fetching {sel_g[0]} anime...[/bold blue]"):
                        g_results = self.api.get_genre_anime(g_slug, limit=40)
                    if g_results:
                        self._process_anime_list(g_results, f"Genre: {sel_g[0]}")
                continue

            if cmd == 's' and supports_fn('studios'):
                from nyoanime.config import POPULAR_STUDIOS_MAP
                studios = list(POPULAR_STUDIOS_MAP.keys())
                sel_s = self._launcher(studios, "Select Studio")
                if sel_s:
                    s_name = sel_s[0]
                    with self.console.status(f"[bold blue]Fetching anime from {s_name}...[/bold blue]"):
                        s_results = self.api.get_studio_anime(s_name, limit=40)
                    if s_results:
                        self._process_anime_list(s_results, f"Studio: {s_name}")
                continue

            # Standard Search Query
            with self.console.status(f"[bold blue]Searching for '{cmd}'...[/bold blue]"):
                results = self._search_all_providers(cmd)

            if not results:
                self.console.print(f"[yellow]No anime found matching '{cmd}'.[/yellow]")
                time.sleep(1.0)
                continue

            self._process_anime_list(results, f"Results for '{cmd}'")


def run_simple_cli(query: Optional[str] = None, deps: Optional[Dict[str, Any]] = None):
    """Entry point for standalone simple CLI runner."""
    from nyoanime.providers import ProviderManager
    from nyoanime.player import PlayerManager
    from nyoanime.history import HistoryManager
    from nyoanime.settings import SettingsManager
    from nyoanime.discord_rpc import DiscordRPCManager

    if deps:
        api = deps.get('api') or ProviderManager.get_provider()
        player = deps.get('player') or PlayerManager()
        history = deps.get('history') or HistoryManager()
        settings = deps.get('settings') or SettingsManager()
        rpc = deps.get('rpc') or DiscordRPCManager()
    else:
        settings = SettingsManager()
        provider_id = ProviderManager.sync_active_provider(settings)
        api = ProviderManager.get_provider(provider_id)
        rpc = DiscordRPCManager()
        player = PlayerManager(rpc_manager=rpc)
        history = HistoryManager()

    try:
        from nyoanime.logger import install_global_exception_handler
        install_global_exception_handler()
    except Exception:
        pass

    wrapper = NyoAnimeWrapper(api, player, history, settings, rpc)
    return wrapper.run(query)
