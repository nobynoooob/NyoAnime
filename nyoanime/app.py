import sys
import atexit
import re
from contextlib import contextmanager
from pathlib import Path
from rich.align import Align
from rich.panel import Panel
from rich.text import Text
from rich.box import HEAVY

from .config import COLOR_BORDER, COLOR_TITLE, GOODBYE_ART, POPULAR_GENRES
from .ui import UIManager
from .providers import ProviderManager
from .providers.language import providers_for_mode
from .providers.search import search_providers
from .player import PlayerManager
from .discord_rpc import DiscordRPCManager
from .models import AnimeResult, Episode, QualityOption
from .utils import show_cursor, hide_cursor
from .history import HistoryManager, format_seconds
from .settings import SettingsManager
from .favorites import FavoritesManager
from .deps import ensure_dependencies
from .cli import run_simple_cli
from .downloader import (
    DownloadConfig,
    DownloadPlan,
    download_single_episode,
    execute_batch_download,
    list_completed_downloads,
    parse_speed_limit,
    prompt_download_options,
    remove_completed_download,
    resolve_download_plan,
)
from .hls import probe_subtitle_url
from .providers.language import (
    mode_audio_lang,
    normalize_language_mode,
    provider_language_mode,
    provider_supports_mode,
)
from .logger import logger, install_global_exception_handler
import shutil
import argparse


def normalize_latest_episodes(episodes):
    """Normalize provider latest-feed entries into the dicts the latest menu expects."""
    normalized = []
    for episode in episodes or []:
        if isinstance(episode, Episode):
            normalized.append(
                {
                    "title": episode.title or f"Episode {episode.display_num}",
                    "slug": episode.anime_id or episode.url,
                    "ep_num": episode.display_num,
                    "url": episode.url,
                    "number": episode.number,
                }
            )
        elif isinstance(episode, AnimeResult):
            # Some providers' "latest" rail returns anime entries rather than
            # episode entries.  Treat them as episode 1 of that title.
            normalized.append(
                {
                    "title": episode.title_en or episode.title_ar or "Unknown",
                    "slug": episode.id,
                    "ep_num": 1,
                    "url": episode.id,
                    "number": "1",
                }
            )
        elif isinstance(episode, dict):
            normalized.append(episode)
    return normalized


class NyoAnimeApp:
    def __init__(self):
        install_global_exception_handler()
        atexit.register(show_cursor)
        hide_cursor()
        self.ui = UIManager()
        self.settings = SettingsManager()
        if self.settings.get("debug_logging"):
            logger.enable()
        self.provider = None
        self.api = None
        self._sync_active_provider()
        self.rpc = DiscordRPCManager()
        self.player = PlayerManager(rpc_manager=self.rpc, console=self.ui.console)
        self.history = HistoryManager()
        self.favorites = FavoritesManager()
        self.version_info = None
        self.current_mode = "tui"
        self.force_cli = False
        self._cleaned_up = False

    def _sync_active_provider(self):
        """Re-resolve the provider from language settings and refresh both handles."""
        provider_id = ProviderManager.sync_active_provider(self.settings)
        self.provider = ProviderManager.get_provider(provider_id)
        self.api = self.provider
        return provider_id

    def run(self):
        parser = argparse.ArgumentParser(
            description="NyoAnime: A CLI tool to browse and watch anime in Arabic and English.",
            formatter_class=argparse.RawTextHelpFormatter
        )
        parser.add_argument('-i', '--interactive', action='store_true', help="Force minimal interactive CLI mode")
        parser.add_argument(
            '-p', '--provider',
            choices=ProviderManager.get_provider_choices(),
            help="Specify active anime provider (Arabic: anime3rb, anime_slayer, animeify, stardima; English: hianime, hianime_dub)"
        )
        parser.add_argument('-d', '--debug', action='store_true', help="Enable verbose debug logging to file and console")
        parser.add_argument('-v', '--version', action='store_true', help="Show version information")
        parser.add_argument(
            '--session-dir',
            metavar='DIR',
            help=(
                "Use an isolated Supabase session directory.\n"
                "Lets a second NyoAnime instance sign in as a different participant."
            ),
        )
        parser.add_argument(
            '--session-path',
            metavar='FILE',
            help="Use an exact Supabase session file instead of the default path.",
        )
        parser.add_argument(
            '--setup-supabase',
            action='store_true',
            help=(
                "Save SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY to ~/.nyoanime/.env.\n"
                "Prompts once; the key is entered with hidden input."
            ),
        )
        parser.add_argument('query', nargs='*', help="Anime name to search for")
        
        args = parser.parse_args()

        if args.session_path or args.session_dir:
            from .party.session_storage import set_session_override

            set_session_override(
                session_dir=args.session_dir,
                session_path=args.session_path,
            )

        if args.setup_supabase:
            from .party.local_config import run_supabase_setup

            try:
                config_path = run_supabase_setup()
            except (EOFError, KeyboardInterrupt):
                print("\nSetup cancelled.")
                sys.exit(1)
            except ValueError as exc:
                print(f"Setup failed: {exc}")
                sys.exit(1)
            print(f"Saved Supabase configuration to {config_path}")
            print("Start NyoAnime again to use it.")
            sys.exit(0)
        
        if args.debug:
            logger.enable(console_mirror=True)
            logger.debug("APP", "CLI flag --debug specified, verbose console mirror enabled")

        if args.provider:
            from .providers.language import provider_language_mode

            provider_id = ProviderManager.get_canonical_id(args.provider)
            self.settings.set("anime_provider", provider_id)
            self.settings.set("language_mode", provider_language_mode(provider_id))
            self.settings.set("auto_provider", False)
            self._sync_active_provider()
            logger.debug("APP", f"CLI flag --provider specified, active provider set to {provider_id}")
            
        if args.version:
            from .version import __version__
            print(f"NyoAnime v{__version__}")
            sys.exit(0)

        self.force_cli = args.interactive
        initial_query = " ".join(args.query) if args.query else None
        
        logger.debug("APP", f"App starting | interactive={args.interactive} | debug={args.debug} | provider={ProviderManager.get_active_provider_id()} | query={initial_query}")

        if not ensure_dependencies():
            print("\n[!] Cannot start without required dependencies.")
            input("Press ENTER to exit...")
            sys.exit(1)
        
        atexit.register(self.cleanup)
        
        import threading
        rpc_connected = {'status': None}
        
        if self.settings.get('discord_rpc'):
            def connect_rpc():
                rpc_connected['status'] = self.rpc.connect()
            threading.Thread(target=connect_rpc, daemon=True).start()
        
        
        def run_cleanup_bg():
            try:
                from .storage import cleanup_temporary_files
                cleanup_temporary_files()
            except Exception:
                pass
        threading.Thread(target=run_cleanup_bg, daemon=True).start()

        self.rpc_status = rpc_connected

        # Show changelog modal on first run or after update
        try:
            from .changelog import render_changelog_popup
            render_changelog_popup(self.ui.console)
        except Exception:
            pass

        try:
            self.unified_loop(initial_query)
        except KeyboardInterrupt:
            self.handle_exit()
        except Exception as e:
            self.handle_error(e)
        finally:
            self.cleanup()

    def unified_loop(self, query=None):
        while True:
            is_narrow = shutil.get_terminal_size().columns < 64
            
            if self.force_cli or is_narrow:
                self.current_mode = "cli"
                result = self.run_cli_mode(query)
                query = None # Clear query after first run
                if result == "SWITCH_TO_TUI":
                    if self.force_cli:
                         pass
                    continue
                break
            else:
                self.current_mode = "tui"
                result = self.run_tui_mode(query)
                query = None # Clear query after first run
                if result == "SWITCH_TO_CLI":
                    continue
                break

    def run_cli_mode(self, query=None):
        deps = {
            'api': self.api,
            'player': self.player,
            'history': self.history,
            'settings': self.settings,
            'rpc': self.rpc,
            'favorites': self.favorites
        }
        return run_simple_cli(query, deps=deps)

    def run_tui_mode(self, initial_query=None):
        query_override = initial_query
        
        while True:
            if '-i' not in sys.argv and shutil.get_terminal_size().columns < 64:
                return "SWITCH_TO_CLI"

            # Always synchronize active provider directly from settings before home screen/actions
            self._sync_active_provider()

            if query_override:
                action, payload = "search", query_override
                query_override = None
            else:
                provider_name = getattr(self.api, 'name', 'Unknown')
                action, payload = self.ui.home_screen_menu(
                    rpc_status=getattr(self, 'rpc_status', None),
                    version_info=self.version_info,
                    active_provider=provider_name,
                    provider=self.api,
                )

            if action in ["quit", "exit"] or action is None:
                break

            results = []

            if action == "latest":
                if hasattr(self.api, "supports") and not self.api.supports("latest"):
                    self.ui.render_message("Info", f"{getattr(self.api, 'name', 'Provider')} does not support latest episodes.", "info")
                    continue
                self.handle_latest_episodes()
                continue

            elif action == "movies":
                if hasattr(self.api, "supports") and not self.api.supports("movies"):
                    self.ui.render_message("Info", f"{getattr(self.api, 'name', 'Provider')} does not support anime movies.", "info")
                    continue
                self.handle_movies()
                continue

            elif action == "top_rated":
                if hasattr(self.api, "supports") and not self.api.supports("top_rated"):
                    self.ui.render_message("Info", f"{getattr(self.api, 'name', 'Provider')} does not support top rated anime.", "info")
                    continue
                self.handle_top_rated()
                continue

            elif action == "trending":
                if hasattr(self.api, "supports") and not self.api.supports("trending"):
                    self.ui.render_message("Info", f"{getattr(self.api, 'name', 'Provider')} does not support trending anime.", "info")
                    continue
                self.handle_trending()
                continue

            elif action == "popular":
                if hasattr(self.api, "supports") and not self.api.supports("popular"):
                    self.ui.render_message("Info", f"{getattr(self.api, 'name', 'Provider')} does not support popular anime.", "info")
                    continue
                self.handle_popular()
                continue

            elif action == "genres":
                if hasattr(self.api, "supports") and not self.api.supports("genres"):
                    self.ui.render_message("Info", f"{getattr(self.api, 'name', 'Provider')} does not support genres.", "info")
                    continue
                self.handle_genres()
                continue

            elif action == "studios":
                if hasattr(self.api, "supports") and not self.api.supports("studios"):
                    self.ui.render_message("Info", f"{getattr(self.api, 'name', 'Provider')} does not support studios.", "info")
                    continue
                self.rpc.update_studios()
                self.handle_studios()
                continue

            elif action == "history":
                self.rpc.update_history()
                self.handle_history()
                continue

            elif action == "favorites":
                self.rpc.update_favorites()
                self.handle_favorites()
                continue

            elif action == "downloads":
                self.handle_downloads()
                continue

            elif action == "settings":
                self.rpc.update_settings()
                self.ui.settings_menu(self.settings)
                self._sync_active_provider()
                continue

            elif action == "changelog":
                from .changelog import render_changelog_popup
                render_changelog_popup(self.ui.console, force=True)
                continue

            elif action == "search":
                search_query = payload
                if not search_query:
                    continue
                self.rpc.update_searching()
                results = self._search_all_providers(search_query)
                if not results:
                    q_clean = re.sub(r'[:\-!?,._\(\)\[\]]', ' ', search_query).strip()
                    q_clean = re.sub(r'\s+', ' ', q_clean)
                    variants = []
                    if q_clean and q_clean.lower() != search_query.lower():
                        variants.append(q_clean)
                    ar_norm = search_query.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ة', 'ه').replace('ى', 'ي')
                    if ar_norm != search_query and ar_norm not in variants:
                        variants.append(ar_norm)
                    words = q_clean.split()
                    if len(words) > 3:
                        variants.append(' '.join(words[:3]))

                    for alt_q in variants:
                        results = self._search_all_providers(alt_q)
                        if results:
                            break

                if not results:
                    self.ui.render_message(
                        "✗ No Anime Found", 
                        f"No anime matching '{search_query}' was found.\n\nTry:\n• Checking spelling\n• Using English or Romaji name\n• Using Arabic title", 
                        "error"
                    )
                    continue
                self.handle_anime_selection(results)
                continue

            elif action == "watchtogether":
                # Start Watch Together mode
                from .ui_watchtogether import WatchTogetherUI

                try:
                    # One UI manager so Watch Together shares the live theme,
                    # logo and menu language with the rest of NyoAnime. The
                    # browse callback reuses the normal search/anime/episode
                    # menus without tearing down the room transport.
                    wwt_ui = WatchTogetherUI(
                        self.ui,
                        browse_callback=self.browse_anime_for_watch_together,
                    )
                    wwt_ui.handle_watch_together(self.api)
                except KeyboardInterrupt:
                    continue
                except Exception:
                    logger.error("WATCH", "Watch Together session failed", exc_info=True)
                    self.ui.render_message(
                        "Watch Together closed",
                        "Something went wrong. Please try again.",
                        "error",
                    )
                    continue
                # After Watch Together exits, return to main menu
                continue

    def handle_anime_selection_with_lazy_load(self, results, load_more_callback):
        while True:
            anime_idx = self.ui.anime_selection_menu(results, load_more_callback=load_more_callback, api=self.api)
            
            if anime_idx == -1:
                sys.exit(0)
            if anime_idx is None:
                return
            
            selected_anime = results[anime_idx]

            self.rpc.update_viewing_anime(selected_anime.title_en, selected_anime.thumbnail)
            
            with self._using_provider(self._api_for_anime(selected_anime)):
                episodes = self.ui.run_with_loading(
                    "Loading episodes & poster...",
                    lambda a=selected_anime: self._fetch_episodes_and_poster(a)
                )

                if not episodes:
                    self.ui.render_message(
                        "✗ No Episodes",
                        f"No episodes found for '{selected_anime.title_en}'.",
                        "error"
                    )
                    continue

                self.handle_episode_selection(selected_anime, episodes)

    def handle_latest_episodes(self):
        episodes = self.ui.run_with_loading(
            "Fetching latest episodes...",
            self.api.get_latest_episodes,
            40
        )
        if not episodes:
            self.ui.render_message("Info", "No recent episodes found.", "info")
            return

        # The latest-episodes menu is dict-based; providers return Episode
        # objects.  Normalize before rendering so every provider works.
        episodes = normalize_latest_episodes(episodes)

        while True:
            res = self.ui.latest_episodes_menu(episodes)
            if res is None:
                break
            idx, act = res
            ep_info = episodes[idx]
            if isinstance(ep_info, Episode):
                ep_info = {
                    "title": ep_info.title or f"Episode {ep_info.display_num}",
                    "slug": ep_info.anime_id or ep_info.url,
                    "ep_num": ep_info.display_num,
                }

            # Latest feeds can contain an episode added after the provider's
            # detail/episode cache was warmed; refresh just this title.
            refresh_cache = getattr(self.api, "refresh_anime_cache", None)
            if callable(refresh_cache):
                refresh_cache(ep_info["slug"])

            selected_anime = self.ui.run_with_loading(
                f"Loading {ep_info['title']}...",
                self.api.get_anime_details,
                ep_info["slug"]
            )
            if not selected_anime:
                continue

            all_episodes = self.ui.run_with_loading(
                "Loading episode list...",
                self.api.get_episodes,
                selected_anime.id
            )
            if not all_episodes:
                continue

            initial_idx = None
            # Prefer the exact episode id; this disambiguates shows whose
            # episode numbers reset each season.
            latest_url = ep_info.get("url")
            if latest_url:
                for index, episode in enumerate(all_episodes):
                    if getattr(episode, "url", "") == latest_url:
                        initial_idx = index
                        break
            if initial_idx is None:
                initial_idx = self._find_episode_index(all_episodes, ep_info["ep_num"])
            self.handle_episode_selection(selected_anime, all_episodes, initial_idx=initial_idx)

    def handle_movies(self):
        results = self.ui.run_with_loading(
            "Fetching anime movies...",
            self.api.get_movies,
            0,
            20
        )
        if results:
            def load_more_movies(current_count):
                return self.api.get_movies(current_count, 20)
            self.handle_anime_selection_with_lazy_load(results, load_more_movies)
        else:
            self.ui.render_message("Info", "No anime movies found.", "info")

    def handle_top_rated(self):
        results = self.ui.run_with_loading(
            "Fetching top-rated anime (9-10)...",
            self.api.get_top_rated_anime,
            0,
            20,
            "9-10"
        )
        if results:
            def load_more_top(current_count):
                return self.api.get_top_rated_anime(current_count, 20, "9-10")
            self.handle_anime_selection_with_lazy_load(results, load_more_top)
        else:
            self.ui.render_message("Info", "No top-rated anime found.", "info")

    def handle_trending(self):
        self.rpc.update_trending()
        results = self.ui.run_with_loading(
            "Fetching trending anime...",
            self.api.get_trending_anime,
            0,
            20
        )
        if results:
            def load_more_trending(current_count):
                return self.api.get_trending_anime(current_count, 20)
            self.handle_anime_selection_with_lazy_load(results, load_more_trending)
        else:
            self.ui.render_message("Info", "No trending anime found.", "info")

    def handle_popular(self):
        self.rpc.update_popular()
        results = self.ui.run_with_loading(
            "Fetching spotlight & popular anime...",
            self.api.get_pinned_anime
        )
        if not results:
            results = self.ui.run_with_loading(
                "Fetching popular anime...",
                self.api.get_trending_anime,
                0,
                20
            )
        if results:
            def load_more_popular(current_count):
                return self.api.get_trending_anime(current_count, 20)
            self.handle_anime_selection_with_lazy_load(results, load_more_popular)
        else:
            self.ui.render_message("Info", "No popular anime found.", "info")

    def handle_genres(self):
        self.rpc.update_genres()
        selected_genre = self.ui.genre_selection_menu(POPULAR_GENRES)
        if selected_genre:
            genre_name = selected_genre["name_en"] if isinstance(selected_genre, dict) else str(selected_genre)
            genre_slug = selected_genre.get("slug", genre_name.lower()) if isinstance(selected_genre, dict) else genre_name.lower()

            results = self.ui.run_with_loading(
                f"Fetching {genre_name} anime...",
                self.api.get_genre_anime,
                genre_slug,
                0,
                20
            )
            if results:
                def load_more_genre(current_count):
                    return self.api.get_genre_anime(genre_slug, current_count, 20)
                self.handle_anime_selection_with_lazy_load(results, load_more_genre)
            else:
                self.ui.render_message("Info", f"No anime found for genre: {genre_name}", "info")

    def handle_studios(self):
        studios = [
            "Toei Animation", "Sunrise", "Madhouse", "Production I.G", "J.C.Staff", 
            "TMS Entertainment", "Studio Pierrot", "Studio Deen", "A-1 Pictures", 
            "Bones", "Kyoto Animation", "MAPPA", "Wit Studio", "ufotable", 
            "White Fox", "David Production", "Shaft", "Trigger", "CloverWorks", 
            "Lerche", "P.A. Works", "CoMix Wave Films", "Gainax", "Studio Ghibli"
        ]
        studios.sort()
        
        selected_studio = self.ui.selection_menu(studios, title="Select Studio")
        if selected_studio:
            results = self.ui.run_with_loading(
                f"Fetching {selected_studio} anime...",
                self.api.get_studio_anime,
                selected_studio,
                0,
                20
            )
            if results:
                def load_more_studio(current_count):
                    return self.api.get_studio_anime(selected_studio, current_count, 20)
                self.handle_anime_selection_with_lazy_load(results, load_more_studio)
            else:
                self.ui.render_message("Info", f"No anime found for studio: {selected_studio}", "info")

    def handle_history(self):
        while True:
            history_items = self.history.get_history()
            if not history_items:
                self.ui.render_message("Info", "No history found.", "info")
                return

            result = self.ui.history_menu(history_items)
            if result is None:
                break
            
            selected_idx, action = result
            item = history_items[selected_idx]
            
            if action == 'remove':
                self.history.remove(item['anime_id'], provider=item.get('provider', ''))
                continue
            elif action == 'resume':
                self.resume_anime(item, direct_watch=True)
            elif action == 'episodes':
                self.resume_anime(item, direct_watch=False)

    def _find_episode_index(self, episodes, episode_value):
        if episode_value is None:
            return 0

        target_raw = str(episode_value).strip()
        if not target_raw:
            return 0

        for idx, ep in enumerate(episodes):
            if str(ep.display_num) == target_raw or str(ep.number) == target_raw:
                return idx

        try:
            target_float = float(target_raw)
            for idx, ep in enumerate(episodes):
                try:
                    if float(ep.display_num) == target_float:
                        return idx
                except (TypeError, ValueError):
                    continue
        except (TypeError, ValueError):
            pass

        return 0

    def resume_anime(self, history_item, direct_watch=True):
        item_provider = str(history_item.get('provider') or '')
        target_anime_id = str(history_item.get('anime_id') or history_item.get('id') or "")
        if item_provider:
            canonical = ProviderManager.get_canonical_id(item_provider)
            if target_anime_id.startswith(f"{canonical}:"):
                target_anime_id = target_anime_id.split(":", 1)[1]
            if canonical and canonical != getattr(self.provider, 'id', ''):
                # Resume on the provider that actually owns this entry.
                self.settings.set('anime_provider', canonical)
                self.settings.set('language_mode', provider_language_mode(canonical))
                self._sync_active_provider()

        results = self.ui.run_with_loading("Resuming...", self.api.search_anime, history_item['title'])
        if not results:
            self.ui.render_message("Error", "Could not find anime details.", "error")
            return

        selected_anime = None
        for res in results:
            if target_anime_id and str(res.id) == target_anime_id:
                selected_anime = res
                break
        
        if not selected_anime:
            selected_anime = results[0] # Fallback

        self.rpc.update_viewing_anime(selected_anime.title_en, selected_anime.thumbnail)
        episodes = self.ui.run_with_loading(
            "Loading episodes...", 
            self.api.get_episodes, 
            selected_anime.id
        )
        
        if episodes:
            initial_idx = self._find_episode_index(episodes, history_item.get('episode'))
            if direct_watch:
                target_ep = episodes[initial_idx]
                server_data = self.ui.run_with_loading(
                    f"Loading servers for Ep {target_ep.display_num}...",
                    self.api.get_streaming_servers,
                    selected_anime.id, 
                    target_ep.number,
                    selected_anime.type
                )
                if not server_data:
                    self.ui.render_message("Error", "No servers available for this episode.", "error")
                    return

                action_taken = self.handle_quality_selection(selected_anime, target_ep, server_data)
                if not action_taken:
                    # User cancelled quality selection -> return cleanly to caller
                    return

                if action_taken == "watch":
                    current_idx = initial_idx
                    while True:
                        auto_next = self.settings.get('auto_next')
                        if auto_next:
                            if current_idx + 1 < len(episodes):
                                current_idx += 1
                                selected_ep = episodes[current_idx]
                                s_data = self.ui.run_with_loading(
                                    "Loading servers...",
                                    self.api.get_streaming_servers,
                                    selected_anime.id,
                                    selected_ep.number,
                                    selected_anime.type
                                )
                                if s_data:
                                    act = self.handle_quality_selection(selected_anime, selected_ep, s_data)
                                    if act == "watch":
                                        continue
                            else:
                                self.ui.render_message("Info", "No more episodes!", "info")
                            break
                        
                        next_action = self.ui.post_watch_menu(selected_anime.title_en, str(episodes[current_idx].display_num))
                        if next_action == "Next Episode":
                            if current_idx + 1 < len(episodes):
                                current_idx += 1
                                selected_ep = episodes[current_idx]
                                s_data = self.ui.run_with_loading(
                                    "Loading servers...",
                                    self.api.get_streaming_servers,
                                    selected_anime.id,
                                    selected_ep.number,
                                    selected_anime.type
                                )
                                if s_data:
                                    act = self.handle_quality_selection(selected_anime, selected_ep, s_data)
                                    if act == "watch":
                                        continue
                            else:
                                self.ui.render_message("Info", "No more episodes!", "info")
                            break
                        elif next_action == "Previous Episode":
                            if current_idx > 0:
                                current_idx -= 1
                                selected_ep = episodes[current_idx]
                                s_data = self.ui.run_with_loading(
                                    "Loading servers...",
                                    self.api.get_streaming_servers,
                                    selected_anime.id,
                                    selected_ep.number,
                                    selected_anime.type
                                )
                                if s_data:
                                    act = self.handle_quality_selection(selected_anime, selected_ep, s_data)
                                    if act == "watch":
                                        continue
                            else:
                                self.ui.render_message("Info", "This is the first episode.", "info")
                            break
                        elif next_action == "Replay":
                            selected_ep = episodes[current_idx]
                            s_data = self.ui.run_with_loading(
                                "Loading servers...",
                                self.api.get_streaming_servers,
                                selected_anime.id,
                                selected_ep.number,
                                selected_anime.type
                            )
                            if s_data:
                                act = self.handle_quality_selection(selected_anime, selected_ep, s_data, start_time=0.001)
                                if act == "watch":
                                    continue
                            break
                        elif next_action == "Back to Episodes":
                            self.handle_episode_selection(selected_anime, episodes, initial_idx=current_idx)
                            return
                        else:
                            # User pressed Back/ESC/q: return cleanly to History
                            return
                    return
            else:
                self.handle_episode_selection(selected_anime, episodes, initial_idx=initial_idx)

    def handle_favorites(self):
        while True:
            fav_items = self.favorites.get_all()
            if not fav_items:
                self.ui.render_message("Info", "No favorites added yet.", "info")
                return

            result = self.ui.favorites_menu(fav_items)
            if result is None:
                break
            
            idx, action = result
            item = fav_items[idx]
            
            if action == 'remove':
                self.favorites.remove(item['anime_id'])
                continue
            elif action == 'watch':
                try:
                    self.resume_anime(item)
                except Exception as e:
                    self.ui.render_message("Error", f"Failed to resume anime: {str(e)}", "error")

    def handle_downloads(self):
        directory = Path(self._get_download_directory()).expanduser()
        while True:
            items = list_completed_downloads(directory)
            if not items:
                self.ui.render_message(
                    "Downloads",
                    f"No completed downloads in {directory}",
                    "info",
                )
                return

            labels = []
            for item in items:
                sub_count = len(item["subtitles"])
                extra = f"  •  {sub_count} subtitle(s)" if sub_count else ""
                labels.append(f"{item['name']}  ({item['size_label']}){extra}")

            selected = self.ui.selection_menu(labels, "Downloads")
            if selected is None:
                return

            target = items[labels.index(selected)]
            try:
                answer = self.ui.console.input(
                    f"[yellow]Delete '{target['name']}'? [y/N]: [/yellow]"
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            if answer not in ("y", "yes"):
                continue
            if remove_completed_download(target["path"]):
                self.ui.render_message("Deleted", target["name"], "info")
            else:
                self.ui.render_message("Error", f"Could not delete {target['name']}", "error")

    def _resolve_watch_together_playback(self, api, anime, episode, language_mode):
        """Resolve a direct stream for the picked episode without launching a player."""
        episode_number = (
            getattr(episode, 'number', None)
            or getattr(episode, 'display_num', '')
        )
        try:
            server_data = api.get_streaming_servers(
                getattr(anime, 'id', ''),
                episode_number,
                getattr(anime, 'type', 'SERIES') or 'SERIES',
            )
        except Exception as exc:
            logger.debug("WATCH", f"Could not resolve Watch Together stream: {exc}")
            return None, "Couldn't resolve a stream for that episode."

        if not isinstance(server_data, dict):
            return None, "No stream servers available for that episode."

        qualities = list(server_data.get('Qualities') or [])
        if not qualities:
            return None, "No video streams found for that episode."

        preferred = str(self._get_default_download_quality() or '').lower()
        choice = None
        for quality in qualities:
            label = f"{getattr(quality, 'name', '')} {getattr(quality, 'res', '')}".lower()
            if preferred and preferred in label:
                choice = quality
                break
        if choice is None:
            def height(item):
                match = re.search(r"(\d{3,4})p", f"{getattr(item, 'res', '')} {getattr(item, 'name', '')}")
                return int(match.group(1)) if match else 0
            choice = max(qualities, key=height)

        current = server_data.get('CurrentEpisode') or {}
        direct_url = (
            getattr(choice, 'direct_url', '')
            or getattr(choice, 'url', '')
            or current.get(getattr(choice, 'server_key', ''), '')
        )
        if not str(direct_url).startswith('http'):
            return None, "Couldn't resolve a direct video stream."

        audio_tracks = list(getattr(choice, 'audio_tracks', []) or [])
        master_url = getattr(choice, 'master_url', '') or ''
        playback_url = master_url if audio_tracks and master_url else direct_url
        bitrate = int(getattr(choice, 'bandwidth', 0) or 0) if playback_url == master_url else 0

        subtitle_url = getattr(choice, 'subtitle_url', '') or ''
        subtitle_lang = getattr(choice, 'subtitle_lang', '') or ''
        audio_lang = getattr(choice, 'audio_lang', '') or ''
        display = str(getattr(episode, 'display_num', '') or episode_number)
        title = f"{getattr(anime, 'title_en', '') or 'Episode'} - Ep {display}"
        quality_match = re.search(r"\b(\d{3,4}p)\b", str(getattr(choice, 'name', '')))
        quality_tag = quality_match.group(1) if quality_match else str(getattr(choice, 'name', ''))

        playback = {
            'url': str(playback_url),
            'title': title,
            'episode_number': display,
            'episode_title': str(getattr(episode, 'title', '') or ''),
            'headers': dict(getattr(choice, 'headers', None) or {}),
            'start_time': 0.0,
            'audio_lang': audio_lang,
            'subtitle_url': subtitle_url,
            'subtitle_lang': subtitle_lang,
            'audio_url': getattr(choice, 'audio_url', '') or '',
            'subtitles_disabled': False,
            'hls_bitrate': bitrate,
            'quality': quality_tag,
            'provider_id': getattr(api, 'id', ''),
            'anime_id': getattr(anime, 'id', ''),
        }
        return playback, None

    def browse_anime_for_watch_together(self, api):
        """Pick an anime + episode with NyoAnime's normal browser menus.

        Called from the Watch Together room. The room's transport and
        subscriptions stay alive while this runs; returning ``None`` means the
        user backed out and should land back in the room.
        """
        if api is None:
            return None

        from .utils import restore_terminal_for_input, enter_raw_mode_after_input

        self.ui.clear()
        self.ui.console.print(
            "\n  [bold]Browse anime[/bold]  "
            "[dim]The room stays open while you pick.[/dim]\n"
        )
        restore_terminal_for_input()
        try:
            query = self.ui.console.input(
                "  [bold]Search[/bold] [dim](blank to return to the room):[/dim] "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        finally:
            enter_raw_mode_after_input()

        if not query:
            return None

        results = self.ui.run_with_loading("Searching...", api.search_anime, query)
        if not results:
            self.ui.render_message(
                "No anime found",
                f"No anime matching '{query}'. Try another spelling or title.",
                "info",
            )
            return None

        provider_id = getattr(api, "id", "")
        while True:
            anime_idx = self.ui.anime_selection_menu(results, api=api)
            if anime_idx is None or anime_idx == -1:
                return None

            anime = results[anime_idx]
            episodes = self.ui.run_with_loading(
                "Loading episodes...",
                api.get_episodes,
                anime.id,
            )
            if not episodes:
                self.ui.render_message(
                    "No episodes",
                    f"No episodes found for '{anime.title_en}'.",
                    "error",
                )
                continue

            last_watched = self.history.get_last_watched(anime.id, provider=provider_id)
            progress = self.history.get_all_episodes_progress(anime.id, provider=provider_id)
            is_favorite = self.favorites.is_favorite(anime.id)
            details = {
                "score": anime.score,
                "rank": anime.rank,
                "popularity": anime.popularity,
                "rating": anime.rating,
                "type": anime.type,
                "episodes": anime.episodes,
                "status": anime.status,
                "studio": anime.creators,
                "genres": anime.genres,
                "language": getattr(anime, "language", ""),
                "provider_language": getattr(api, "language", ""),
                "content_type": getattr(anime, "content_type", ""),
            }

            while True:
                ep_idx = self.ui.episode_selection_menu(
                    anime.title_en,
                    episodes,
                    self.rpc,
                    anime.thumbnail,
                    last_watched_ep=last_watched,
                    is_favorite=is_favorite,
                    anime_details=details,
                    default_download_quality=self._get_default_download_quality(),
                    download_mode=self._get_download_mode(),
                    download_path=self._get_download_directory(),
                    episodes_progress=progress,
                )
                if ep_idx is None:
                    break
                if ep_idx == -1:
                    return None
                if ep_idx == "toggle_fav":
                    if is_favorite:
                        self.favorites.remove(anime.id)
                    else:
                        self.favorites.add(anime.id, anime.title_en, anime.thumbnail)
                    is_favorite = not is_favorite
                    continue
                if isinstance(ep_idx, tuple) or ep_idx == "batch_mode":
                    # Downloads aren't part of picking the room's episode.
                    continue
                try:
                    episode = episodes[int(ep_idx)]
                except (TypeError, ValueError, IndexError):
                    continue

                language_mode = self._language_mode()
                try:
                    playback, playback_error = self.ui.run_with_loading(
                        "Resolving stream...",
                        lambda: self._resolve_watch_together_playback(
                            api, anime, episode, language_mode
                        ),
                    )
                except Exception as exc:
                    logger.debug("WATCH", f"Stream resolution failed: {exc}")
                    playback, playback_error = None, (
                        "Couldn't resolve a stream for that episode."
                    )

                return {
                    "anime": anime,
                    "episode": episode,
                    "provider_id": provider_id,
                    "language_mode": language_mode,
                    "playback": playback,
                    "playback_error": playback_error,
                }

    def handle_anime_selection(self, results):
        while True:
            anime_idx = self.ui.anime_selection_menu(results, api=self.api)
            
            if anime_idx == -1:
                sys.exit(0)
            if anime_idx is None:
                return
            
            selected_anime = results[anime_idx]

            self.rpc.update_viewing_anime(selected_anime.title_en, selected_anime.thumbnail)

            with self._using_provider(self._api_for_anime(selected_anime)):
                episodes = self.ui.run_with_loading(
                    "Loading episodes & poster...",
                    lambda a=selected_anime: self._fetch_episodes_and_poster(a)
                )

                if not episodes:
                    self.ui.render_message(
                        "✗ No Episodes",
                        f"No episodes found for '{selected_anime.title_en}'",
                        "error"
                    )
                    continue

                back_pressed = self.handle_episode_selection(selected_anime, episodes)
            if not back_pressed:
                break
    

    def _fetch_episodes_and_poster(self, selected_anime):
        # Fetch rich anime details and update model
        detailed = self.api.get_anime_details(selected_anime.id)
        if detailed:
            selected_anime.score = detailed.score
            selected_anime.genres = detailed.genres
            selected_anime.status = detailed.status
            selected_anime.premiered = detailed.premiered
            selected_anime.creators = detailed.creators
            selected_anime.synopsis = detailed.synopsis
            if detailed.thumbnail:
                selected_anime.thumbnail = detailed.thumbnail

        eps = self.api.get_episodes(selected_anime.id) or []

        if selected_anime.thumbnail:
            screen_height = self.ui.console.height
            target_height = min(screen_height, 50)
            poster_height = target_height - 8
            if poster_height > 0:
                self.ui._generate_poster_ansi(selected_anime.thumbnail, poster_height)
        return eps

    def handle_episode_selection(self, selected_anime, episodes, initial_idx=0):
        current_idx = max(0, min(int(initial_idx or 0), len(episodes) - 1))
        provider_id = getattr(self.provider, 'id', '')

        while True:
            last_watched = self.history.get_last_watched(selected_anime.id, provider=provider_id)
            episodes_prog = self.history.get_all_episodes_progress(selected_anime.id, provider=provider_id)
            is_fav = self.favorites.is_favorite(selected_anime.id)
            default_download_quality = self._get_default_download_quality()
            download_mode = self._get_download_mode()
            
            anime_details = {
                'score': selected_anime.score,
                'rank': selected_anime.rank,
                'popularity': selected_anime.popularity,
                'rating': selected_anime.rating,
                'type': selected_anime.type,
                'episodes': selected_anime.episodes,
                'status': selected_anime.status,
                'studio': selected_anime.creators,
                'genres': selected_anime.genres,
                'language': getattr(selected_anime, 'language', ''),
                'provider_language': getattr(self.api, 'language', ''),
                'content_type': getattr(selected_anime, 'content_type', ''),
            }

            ep_idx = self.ui.episode_selection_menu(
                selected_anime.title_en, 
                episodes, 
                self.rpc, 
                selected_anime.thumbnail,
                last_watched_ep=last_watched,
                is_favorite=is_fav,
                anime_details=anime_details,
                default_download_quality=default_download_quality,
                download_mode=download_mode,
                download_path=self._get_download_directory(),
                initial_selected=current_idx,
                episodes_progress=episodes_prog
            )
            
            if ep_idx == -1:
                sys.exit(0)
            elif ep_idx is None:
                self.rpc.update_browsing()
                return True
            elif isinstance(ep_idx, tuple) and ep_idx[0] == 'download_current':
                target_idx = ep_idx[1]
                if 0 <= target_idx < len(episodes):
                    self.download_episode_with_defaults(selected_anime, episodes[target_idx])
                continue
            elif ep_idx == 'toggle_fav':
                if is_fav:
                    self.favorites.remove(selected_anime.id)
                else:
                    self.favorites.add(selected_anime.id, selected_anime.title_en, selected_anime.thumbnail)
                continue
            elif ep_idx == 'batch_mode':
                self.handle_batch_download(selected_anime, episodes)
                continue
            
            current_idx = ep_idx
            
            while True:
                selected_ep = episodes[current_idx]
                available_modes = getattr(selected_ep, 'available_modes', None) or []
                requested_mode = getattr(self.api, 'audio_mode', '')
                if available_modes and requested_mode in ('sub', 'dub') and requested_mode not in available_modes:
                    label = "English Dub" if requested_mode == 'dub' else "English Sub"
                    self.ui.render_message(
                        "✗ Unavailable",
                        f"{label} is not available for Episode {selected_ep.display_num}.",
                        "info",
                    )
                    break
                
                server_data = self.ui.run_with_loading(
                    "Loading servers...",
                    self.api.get_streaming_servers,
                    selected_anime.id, 
                    selected_ep.number,
                    selected_anime.type
                )
                
                if not server_data:
                    self.ui.render_message(
                        "✗ No Servers", 
                        "No servers available for this episode.",
                        "error"
                    )
                    break
                
                action_taken = self.handle_quality_selection(selected_anime, selected_ep, server_data)
                
                if action_taken == "watch":
                    auto_next = self.settings.get('auto_next')
                    if auto_next:
                        if current_idx + 1 < len(episodes):
                            current_idx += 1
                            continue
                        else:
                            self.ui.render_message("Info", "No more episodes!", "info")
                            break

                    next_action = self.ui.post_watch_menu(selected_anime.title_en, str(selected_ep.display_num))
                    
                    if next_action == "Next Episode":
                        if current_idx + 1 < len(episodes):
                            current_idx += 1
                            continue
                        else:
                            self.ui.render_message("Info", "No more episodes!", "info")
                            break
                    elif next_action == "Previous Episode":
                        if current_idx > 0:
                            current_idx -= 1
                            continue
                        else:
                            self.ui.render_message("Info", "This is the first episode.", "info")
                            break
                    elif next_action == "Replay":
                        continue
                    else:
                        break
                elif action_taken == "download":
                    # For downloads, return directly to the episodes list instead of watch navigation.
                    break
                else:
                    break

    def _get_default_download_quality(self):
        return self.settings.get('default_download_quality') or self.settings.get('default_quality') or "1080p"

    def _get_download_mode(self):
        return (self.settings.get('download_mode') or "internal").lower()

    def _get_download_directory(self):
        return self.settings.get('download_directory') or "downloads"

    def _language_mode(self):
        configured = self.settings.get('language_mode')
        provider_id = getattr(self.provider, 'id', '')
        if configured and provider_supports_mode(provider_id, configured):
            return normalize_language_mode(configured)
        return provider_language_mode(provider_id) if provider_id else normalize_language_mode(configured)

    def _search_all_providers(self, query):
        """Search every provider compatible with the current mode concurrently."""
        mode = self._language_mode()
        targets = providers_for_mode(mode)
        progress = {"done": 0, "total": len(targets), "found": 0}

        def on_done(provider_id, count):
            progress["done"] += 1
            progress["found"] += count

        def status():
            if progress["total"] <= 1:
                return ""
            return f"{progress['done']}/{progress['total']} sources · {progress['found']} results"

        return self.ui.run_with_loading(
            "Searching...",
            search_providers,
            query,
            mode=mode,
            timeout=5.0,
            on_provider_done=on_done,
            status_getter=status,
        )

    def _api_for_anime(self, anime):
        """Return the provider that produced a concurrent-search result."""
        provider_id = getattr(anime, "provider_id", "") or ""
        if not provider_id:
            return self.api
        canonical = ProviderManager.get_canonical_id(provider_id)
        if canonical == getattr(self.api, "id", ""):
            return self.api
        return ProviderManager.get_provider(canonical)

    @contextmanager
    def _using_provider(self, api):
        """Temporarily browse with a result's own provider, then restore."""
        if api is None or api is self.api:
            yield
            return
        previous = (self.api, self.provider)
        self.api = self.provider = api
        try:
            yield
        finally:
            self.api, self.provider = previous

    def _download_config(self, quality=None, subtitle_lang=None):
        speed = parse_speed_limit(self.settings.get('download_speed_limit'))
        return DownloadConfig(
            save_dir=Path(self._get_download_directory()).expanduser(),
            max_parallel=2,
            speed_limit_bytes=speed,
            quality=quality or self._get_default_download_quality(),
            subtitle_lang=subtitle_lang,
            provider=getattr(self.provider, 'id', ''),
            language_mode=self._language_mode(),
        )

    def resolve_default_download_target(self, selected_anime, selected_ep, show_loading=False):
        """Resolve the default-quality :class:`DownloadPlan` for an episode."""
        if show_loading:
            with self.ui.console.status("[bold blue]Resolving stream...[/bold blue]"):
                plan = resolve_download_plan(
                    self.api,
                    selected_anime,
                    selected_ep,
                    self._get_default_download_quality(),
                )
        else:
            plan = resolve_download_plan(
                self.api,
                selected_anime,
                selected_ep,
                self._get_default_download_quality(),
            )
        if not plan:
            return None, "Could not resolve a download link for this episode."
        return plan, None

    def download_episode_with_defaults(self, selected_anime, selected_ep):
        plan, error = self.resolve_default_download_target(selected_anime, selected_ep)
        if error:
            self.ui.render_message("✗ Download Error", error, "error")
            return False

        result = download_single_episode(
            api=self.api,
            anime=selected_anime,
            ep=selected_ep,
            config=self._download_config(quality=plan.quality),
            console=self.ui.console,
            plan=plan,
        )

        return self._finish_single_download(selected_anime, selected_ep, result)

    def _finish_single_download(self, selected_anime, selected_ep, result):
        if not result.get("success"):
            error = result.get("error") or "Unknown download failure."
            if error.startswith("cancelled"):
                self.ui.render_message("Download Paused", error, "info")
            else:
                self.ui.render_message("✗ Download Error", error, "error")
            return False

        if not result.get("skipped"):
            self.history.mark_watched(
                selected_anime.id,
                selected_ep.display_num,
                selected_anime.title_en,
                poster_url=getattr(selected_anime, 'thumbnail', ''),
                provider=getattr(self.provider, 'id', ''),
                language_mode=self._language_mode(),
            )
            subtitle = result.get("subtitle")
            if subtitle:
                self.ui.console.print(f"[dim]Subtitle saved: {subtitle}[/dim]")
        return True

    def handle_batch_download(self, selected_anime, episodes):
        selected_indices = self.ui.batch_selection_menu(episodes)
        if not selected_indices:
            return

        target_eps = [episodes[idx] for idx in selected_indices]
        download_directory = self._get_download_directory()
        default_q = self.settings.get("default_download_quality", "1080p") or "1080p"

        from .utils import restore_terminal_for_input, enter_raw_mode_after_input
        restore_terminal_for_input()
        config = prompt_download_options(
            self.ui.console,
            default_dir=download_directory,
            default_quality=default_q,
            batch_mode=True
        )
        if config:
            config.provider = getattr(self.provider, 'id', '')
            config.language_mode = self._language_mode()
            execute_batch_download(
                api=self.api,
                anime=selected_anime,
                episodes=target_eps,
                config=config,
                console=self.ui.console,
                history_manager=self.history
            )
            try:
                self.ui.console.input("[dim]Press Enter to return to menu...[/dim]")
            except (KeyboardInterrupt, EOFError):
                pass

        enter_raw_mode_after_input()

    def _collect_subtitles(self, available):
        merged = []
        seen = set()
        for quality in available:
            for track in getattr(quality, 'subtitles', None) or []:
                if not isinstance(track, dict):
                    continue
                src = str(track.get('src') or '').strip()
                if not src.startswith('http') or src in seen:
                    continue
                seen.add(src)
                merged.append(dict(track))
        return merged

    def _filter_valid_subtitles(self, subtitles, headers=None):
        """Drop expired/broken subtitle URLs before showing them to the user."""
        if not subtitles:
            return []
        from concurrent.futures import ThreadPoolExecutor

        def _check(track):
            ok, _reason = probe_subtitle_url(track.get('src', ''), headers=headers, timeout=6.0)
            return track, ok

        try:
            with ThreadPoolExecutor(max_workers=min(4, len(subtitles))) as pool:
                results = list(pool.map(_check, subtitles))
        except Exception:
            return subtitles
        valid = [track for track, ok in results if ok]
        dropped = len(subtitles) - len(valid)
        if dropped:
            logger.debug("SUBTITLE", f"Dropped {dropped} unreachable subtitle track(s)")
        return valid

    @staticmethod
    def _collect_audio_tracks(available):
        merged = []
        seen = set()
        for quality in available:
            for track in getattr(quality, 'audio_tracks', None) or []:
                if not isinstance(track, dict):
                    continue
                key = (
                    str(track.get('lang') or ''),
                    str(track.get('label') or ''),
                    str(track.get('src') or ''),
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(dict(track))
        return merged

    @staticmethod
    def _audio_lang_for_track(track, fallback=''):
        lang = str((track or {}).get('lang') or '').strip()
        if not lang:
            return fallback
        short = lang.split('-', 1)[0].lower()
        return f"{lang},{short}" if short and short != lang.lower() else lang

    @staticmethod
    def _default_audio_index(audio_tracks, language_mode=''):
        if not audio_tracks:
            return None
        wanted = (
            mode_audio_lang(language_mode).split(",", 1)[0] if language_mode else ''
        )
        if wanted:
            for idx, track in enumerate(audio_tracks):
                lang = str(track.get('lang') or '').lower()
                if lang.startswith(wanted[:2]):
                    return idx
        for idx, track in enumerate(audio_tracks):
            if track.get('default'):
                return idx
        return 0

    def handle_quality_selection(self, selected_anime, selected_ep, server_data, start_time=None):
        current_ep_data = server_data.get('CurrentEpisode', {})
        available = server_data.get('Qualities')
        if not available:
            qualities = [
                QualityOption("480p", 'FRLowQ', "info"),
                QualityOption("720p", 'FRLink', "info"),
                QualityOption("1080p", 'FRFhdQ', "info"),
            ]
            available = [q for q in qualities if current_ep_data.get(q.server_key)]

        if not available:
            self.ui.render_message(
                "✗ No Links",
                "No video streams found for this episode.",
                "error"
            )
            return None

        probe_headers = getattr(available[0], 'headers', None) or None
        subtitles = self._filter_valid_subtitles(
            self._collect_subtitles(available), headers=probe_headers
        )
        audio_tracks = self._collect_audio_tracks(available)
        subtitle_choice = 0 if subtitles else None
        language_mode = self._language_mode()
        audio_choice = self._default_audio_index(audio_tracks, language_mode)

        while True:
            result = self.ui.quality_selection_menu(
                selected_anime.title_en,
                selected_ep.display_num,
                available,
                self.rpc,
                selected_anime.thumbnail,
                subtitle_options=subtitles,
                subtitle_index=subtitle_choice if subtitle_choice is not None else len(subtitles),
                audio_options=audio_tracks,
                audio_index=audio_choice if audio_choice is not None else 0,
            )

            if result == -1:
                sys.exit(0)
            if result is None:
                return None

            idx, action = result
            if action == 'subtitle' and subtitles:
                picked = self.ui.track_selection_menu(
                    selected_anime.title_en,
                    selected_ep.display_num,
                    subtitles,
                    selected_index=subtitle_choice if subtitle_choice is not None else len(subtitles),
                    kind="Subtitle",
                    allow_off=True,
                )
                if picked is not None:
                    subtitle_choice = None if picked >= len(subtitles) else picked
                continue
            if action == 'audio' and audio_tracks:
                picked = self.ui.track_selection_menu(
                    selected_anime.title_en,
                    selected_ep.display_num,
                    audio_tracks,
                    selected_index=audio_choice or 0,
                    kind="Audio",
                )
                if picked is not None:
                    audio_choice = picked
                continue
            break

        quality = available[idx]
        chosen_subtitle = subtitles[subtitle_choice] if subtitle_choice is not None and subtitle_choice < len(subtitles) else None
        subtitle_url = str((chosen_subtitle or {}).get('src') or '')
        subtitle_lang = str((chosen_subtitle or {}).get('lang') or '')
        direct_url = (
            getattr(quality, 'direct_url', '')
            or getattr(quality, 'url', '')
            or current_ep_data.get(getattr(quality, 'server_key', ''))
        )

        audio_lang = getattr(quality, 'audio_lang', '') or ''
        if audio_tracks and audio_choice is not None and audio_choice < len(audio_tracks):
            audio_lang = self._audio_lang_for_track(audio_tracks[audio_choice], audio_lang)
        if not audio_lang:
            audio_lang = mode_audio_lang(language_mode)

        headers = getattr(quality, 'headers', None) or None
        ext = str(getattr(quality, 'ext', 'mp4') or 'mp4').lstrip('.')
        quality_match = re.search(r"\b(\d{3,4}p)\b", quality.name)
        quality_tag = quality_match.group(1) if quality_match else quality.name

        if not (direct_url and direct_url.startswith("http")):
            self.ui.render_message(
                "✗ Error",
                "Failed to resolve direct video stream for this quality.",
                "error"
            )
            return None

        if action == 'download':
            plan = DownloadPlan(
                url=direct_url,
                headers=dict(headers or {}),
                quality=quality_tag,
                ext=ext,
                subtitle_url=subtitle_url,
                subtitle_lang=subtitle_lang,
                subtitles=subtitles,
                master_url=getattr(quality, 'master_url', '') or '',
                audio_lang=audio_lang,
            )
            result = download_single_episode(
                api=self.api,
                anime=selected_anime,
                ep=selected_ep,
                config=self._download_config(
                    quality=quality_tag,
                    subtitle_lang=subtitle_lang,
                ),
                console=self.ui.console,
                plan=plan,
            )
            if self._finish_single_download(selected_anime, selected_ep, result):
                return "download"
            return None

        player_type = self.settings.get('player')

        actual_start = 0.0
        if start_time is not None and start_time > 0:
            actual_start = start_time
        else:
            ep_prog = self.history.get_episode_progress(
                selected_anime.id, selected_ep.display_num, provider=getattr(self.provider, 'id', '')
            )
            if ep_prog and 0 < ep_prog.get('percent', 0) < 90 and ep_prog.get('time_pos', 0) > 10:
                actual_start = ep_prog['time_pos']
            elif getattr(self.provider, 'id', '') == 'anime3rb' or 'vid3rb.com' in direct_url:
                # Skip 5-second intro bumper for Anime3rb provider only
                actual_start = 5.0

        subtitles_disabled = bool(subtitles) and subtitle_choice is None
        # A master playlist is required for native HLS audio-rendition
        # selection; without separate renditions keep the chosen variant.
        playback_url = direct_url
        playback_bitrate = 0
        if audio_tracks and getattr(quality, 'master_url', ''):
            playback_url = quality.master_url
            playback_bitrate = int(getattr(quality, 'bandwidth', 0) or 0)
        time_hint = f" • Resuming at {format_seconds(actual_start)}" if (actual_start > 0) else ""
        self.ui.render_now_playing(
            selected_anime.title_en,
            f"Episode {selected_ep.display_num}{time_hint}",
            quality.name,
        )

        self.rpc.update_watching(selected_anime.title_en, str(selected_ep.display_num), selected_anime.thumbnail)

        try:
            playback_info = self.player.play(
                playback_url,
                f"{selected_anime.title_en} - Ep {selected_ep.display_num} ({quality.name})",
                player_type=player_type,
                start_time=actual_start,
                audio_lang=audio_lang or None,
                headers=headers,
                subtitle_url=subtitle_url or None,
                subtitle_lang=subtitle_lang or None,
                audio_url=getattr(quality, 'audio_url', '') or None,
                subtitles_disabled=subtitles_disabled,
                hls_bitrate=playback_bitrate or None,
            )
        except FileNotFoundError as error:
            self.ui.render_message("✗ Player Missing", str(error), "error")
            return None
        except Exception as error:
            self.ui.render_message("✗ Playback Error", f"Could not launch player: {error}", "error")
            return None
        self.ui.clear()

        playback_info = playback_info if isinstance(playback_info, dict) else {}
        pos = playback_info.get('time_pos', 0.0)
        dur = playback_info.get('duration', 0.0)
        pct = playback_info.get('percent', 0.0)
        position_reported = bool(playback_info.get('position_reported', False))

        # Only persist a position when the player actually reported one. VLC,
        # Celluloid, IINA, Syncplay and MPC-HC cannot report their final
        # position, so their history entry just records the episode as started.
        if position_reported:
            self.history.mark_watched(
                selected_anime.id,
                selected_ep.display_num,
                selected_anime.title_en,
                time_pos=pos,
                duration=dur,
                percent=pct,
                poster_url=getattr(selected_anime, 'thumbnail', ''),
                provider=getattr(self.provider, 'id', ''),
                language_mode=language_mode,
            )
        else:
            self.history.mark_watched(
                selected_anime.id,
                selected_ep.display_num,
                selected_anime.title_en,
                poster_url=getattr(selected_anime, 'thumbnail', ''),
                provider=getattr(self.provider, 'id', ''),
                language_mode=language_mode,
            )
        self.rpc.update_selecting_episode(selected_anime.title_en, selected_anime.thumbnail)
        return "watch"

    def handle_exit(self):
        self.ui.clear()
        
        panel = Panel(
            Text("👋 Goodbye! See you next time.", justify="center", style="bold white"),
            title=f"[bold {COLOR_TITLE}]Session Ended[/bold {COLOR_TITLE}]",
            box=HEAVY,
            padding=(1, 4),
            border_style=COLOR_BORDER,
            width=min(50, self.ui.console.width - 4)
        )
        
        self.ui.print(Align.center(panel, vertical="middle", height=self.ui.console.height))

    def handle_error(self, e):
        self.ui.clear()
        self.ui.console.print_exception()
        
        panel = Panel(
            Text(f"✗ Unexpected error: {e}", justify="center", style="bold #ff6b6b"),
            title="[bold #ff6b6b]Critical Error[/bold #ff6b6b]",
            box=HEAVY,
            padding=(1, 4),
            border_style="#ff6b6b",
            width=min(60, self.ui.console.width - 4)
        )
        
        self.ui.print(Align.center(panel, vertical="middle", height=self.ui.console.height))
        input("\nPress ENTER to exit...")

    def cleanup(self):
        if self._cleaned_up:
            return
        self._cleaned_up = True

        try:
            self.rpc.disconnect()
        except Exception:
            pass
        
        try:
            self.player.cleanup_temp_mpv()
        except Exception:
            pass
        
        # Only show TUI goodbye if we are NOT in CLI mode
        if self.current_mode != "cli":
            self.ui.clear()
            from .config import COLOR_ASCII
            
            self.ui.print("\n" * 2)
            self.ui.print(Align.center(Text(GOODBYE_ART, style=COLOR_ASCII)))
            self.ui.print("\n")
        show_cursor()


def main():
    install_global_exception_handler()

    # Must happen before any Watch Together client resolves Supabase config.
    from .party.local_config import load_local_env

    load_local_env()

    home_dir = Path.home()
    db_dir = home_dir / ".nyoanime" / "database"
    db_dir.mkdir(parents=True, exist_ok=True)
    
    app = NyoAnimeApp()
    app.run()


if __name__ == "__main__":
    main()
