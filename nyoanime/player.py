import os
import sys
import time
import shutil
import subprocess
import tempfile
import threading
import socket
import json
from typing import Dict, Optional, Any
from .utils import is_bundled, get_stream_referer
from .logger import logger

class MpvTracker:
    """
    High-performance real-time MPV IPC state tracker.
    Continuously queries and observes playback position, duration, percentage,
    paused state, and completion status.
    """
    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self.state = {
            'time_pos': 0.0,
            'duration': 0.0,
            'percent': 0.0,
            'paused': False,
            'completed': False
        }
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self):
        s = None
        is_windows = (os.name == 'nt')
        
        # Connect to MPV IPC socket (retry up to 30 times / 3 seconds)
        for _ in range(30):
            if self._stop_event.is_set():
                return
            try:
                if is_windows:
                    s = open(self.sock_path, 'r+b', buffering=0)
                    break
                else:
                    if os.path.exists(self.sock_path):
                        import socket
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        # Short read timeout keeps stop() responsive while MPV
                        # is busy seeking/rebuffering.
                        s.settimeout(0.15)
                        s.connect(self.sock_path)
                        break
            except Exception:
                pass
            time.sleep(0.1)

        if not s:
            return

        import json

        def send_data(payload: dict):
            try:
                raw = (json.dumps(payload) + "\n").encode('utf-8')
                if is_windows:
                    s.write(raw)
                    s.flush()
                else:
                    s.sendall(raw)
            except Exception:
                pass

        # Register observe_property subscriptions
        for sub_id, prop_name in [
            (1, "time-pos"),
            (2, "duration"),
            (3, "percent-pos"),
            (4, "pause"),
            (5, "eof-reached"),
        ]:
            send_data({"command": ["observe_property", sub_id, prop_name]})

        buffer = ""

        try:
            while not self._stop_event.is_set():
                # 1. Query properties via heartbeat request IDs
                send_data({"command": ["get_property", "time-pos"], "request_id": 101})
                send_data({"command": ["get_property", "duration"], "request_id": 102})
                send_data({"command": ["get_property", "percent-pos"], "request_id": 103})
                send_data({"command": ["get_property", "pause"], "request_id": 104})
                send_data({"command": ["get_property", "eof-reached"], "request_id": 105})

                # 2. Read available incoming responses
                try:
                    if is_windows:
                        chunk = s.read(2048)
                        if not chunk:
                            break
                        chunk_str = chunk.decode('utf-8', errors='ignore')
                    else:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        chunk_str = chunk.decode('utf-8', errors='ignore')

                    buffer += chunk_str
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except Exception:
                            continue

                        req_id = msg.get("request_id")
                        name = msg.get("name")
                        val = msg.get("data")

                        if val is not None:
                            if req_id == 101 or name == "time-pos":
                                try:
                                    self.state["time_pos"] = round(float(val), 1)
                                except (ValueError, TypeError):
                                    pass
                            elif req_id == 102 or name == "duration":
                                try:
                                    self.state["duration"] = round(float(val), 1)
                                except (ValueError, TypeError):
                                    pass
                            elif req_id == 103 or name == "percent-pos":
                                try:
                                    self.state["percent"] = round(float(val), 1)
                                except (ValueError, TypeError):
                                    pass
                            elif req_id == 104 or name == "pause":
                                self.state["paused"] = bool(val)
                            elif req_id == 105 or name == "eof-reached":
                                if bool(val):
                                    self.state["completed"] = True

                        if msg.get("event") == "end-file" and msg.get("reason") == "eof":
                            self.state["completed"] = True

                except (socket.timeout, TimeoutError):
                    pass
                except Exception:
                    break

                # 3. Calculate derived percentage and completion
                t_pos = self.state["time_pos"]
                t_dur = self.state["duration"]
                t_pct = self.state["percent"]

                if t_pct <= 0 and t_dur > 0 and t_pos > 0:
                    self.state["percent"] = round((t_pos / t_dur) * 100, 1)

                if self.state["percent"] >= 85 or (t_dur > 0 and t_dur - t_pos < 35):
                    self.state["completed"] = True

                time.sleep(0.4)

        finally:
            try:
                s.close()
            except Exception:
                pass

class PlayerManager:
    def __init__(self, rpc_manager=None, console=None):
        self.temp_mpv_path = None
        self.rpc_manager = rpc_manager
        self.console = console

    def get_mpv_path(self) -> Optional[str]:
        if is_bundled():
            exe_name = 'mpv.exe' if os.name == 'nt' else 'mpv'
            bundled_mpv = os.path.join(sys._MEIPASS, 'mpv', exe_name)
            if os.path.exists(bundled_mpv):
                if not self.temp_mpv_path or not os.path.exists(self.temp_mpv_path):
                    temp_dir = tempfile.mkdtemp(prefix='anime_browser_mpv_')
                    self.temp_mpv_path = os.path.join(temp_dir, exe_name)
                    shutil.copy2(bundled_mpv, self.temp_mpv_path)
                    
                    if os.name != 'nt':
                        st = os.stat(self.temp_mpv_path)
                        os.chmod(self.temp_mpv_path, st.st_mode | 0o111)
                        
                return self.temp_mpv_path
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            exe_name = 'mpv.exe' if os.name == 'nt' else 'mpv'
            
            dev_mpv = os.path.join(base_dir, 'mpv', exe_name)
            if os.path.exists(dev_mpv):
                return dev_mpv
            
            local_mpv = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mpv', exe_name)
            if os.path.exists(local_mpv):
                return local_mpv

            if shutil.which('mpv'):
                return 'mpv'
            
            return 'mpv'
        
        return 'mpv'

    def cleanup_temp_mpv(self):
        if self.temp_mpv_path and os.path.exists(self.temp_mpv_path):
            try:
                temp_dir = os.path.dirname(self.temp_mpv_path)
                shutil.rmtree(temp_dir, ignore_errors=True)
            except (OSError, PermissionError):
                pass

    def get_available_players(self) -> dict:
        players = {}
        
        # Check MPV
        mpv_path = self.get_mpv_path()
        if mpv_path == 'mpv':
            if shutil.which('mpv'):
                 players['MPV'] = 'mpv'
        elif os.path.exists(mpv_path):
            players['MPV'] = mpv_path

        # Check Celluloid (Linux GTK MPV frontend)
        celluloid_path = shutil.which('celluloid') or shutil.which('io.github.celluloid_player.Celluloid')
        if celluloid_path:
            players['Celluloid'] = celluloid_path

        # Check IINA (macOS MPV frontend)
        iina_path = shutil.which('iina-cli') or shutil.which('iina')
        if not iina_path and sys.platform == 'darwin':
            mac_paths = [
                "/Applications/IINA.app/Contents/MacOS/iina-cli",
                os.path.expanduser("~/Applications/IINA.app/Contents/MacOS/iina-cli")
            ]
            for p in mac_paths:
                if os.path.exists(p):
                    iina_path = p
                    break
        if iina_path:
            players['IINA'] = iina_path

        # Check Syncplay (Multi-user sync)
        syncplay_path = shutil.which('syncplay')
        if syncplay_path:
            players['Syncplay'] = syncplay_path

        # Check VLC
        vlc_path = shutil.which('vlc')
        if not vlc_path:
            if os.name == 'nt':
                paths = [
                    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"
                ]
                for p in paths:
                    if os.path.exists(p):
                        vlc_path = p
                        break
            elif sys.platform == 'darwin':
                paths = [
                    "/Applications/VLC.app/Contents/MacOS/VLC",
                    os.path.expanduser("~/Applications/VLC.app/Contents/MacOS/VLC")
                ]
                for p in paths:
                    if os.path.exists(p):
                        vlc_path = p
                        break
        if vlc_path:
            players['VLC'] = vlc_path

        # Check MPC-HC
        mpc_path = shutil.which('mpc-hc64') or shutil.which('mpc-hc')
        if not mpc_path and os.name == 'nt':
            paths = [
                r"C:\Program Files\MPC-HC\mpc-hc64.exe",
                r"C:\Program Files\MPC-HC\mpc-hc.exe",
                r"C:\Program Files (x86)\MPC-HC\mpc-hc.exe",
                r"C:\Program Files\K-Lite Codec Pack\MPC-HC64\mpc-hc64.exe"
            ]
            for p in paths:
                if os.path.exists(p):
                    mpc_path = p
                    break
        if mpc_path:
            players['MPC-HC'] = mpc_path

        return players

    def play(
        self,
        url: str,
        title: str,
        player_type: str = 'ask',
        start_time: float = 0.0,
        audio_lang: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        subtitle_url: Optional[str] = None,
        subtitle_lang: Optional[str] = None,
        audio_url: Optional[str] = None,
        subtitles_disabled: bool = False,
        hls_bitrate: Optional[int] = None,
    ) -> dict:
        available_players = self.get_available_players()
        default_result = {
            'time_pos': 0.0,
            'duration': 0.0,
            'percent': 0.0,
            'completed': False,
            'position_reported': False,
        }
        
        if not available_players:
            msg = "No video players found on your computer. Please download and install MPV or VLC Media Player."
            if self.console:
                from rich.text import Text
                self.console.print(Text(msg, style="bold red"))
                input("Press Enter to continue...")
            else:
                print(msg, file=sys.stderr)
                input("Press Enter to continue...")
            return default_result

        player_names = list(available_players.keys())
        selected_player = None

        req_p = (player_type or "").lower()
        if req_p in ('mpv', 'celluloid', 'iina', 'syncplay', 'vlc', 'mpc-hc', 'mpc'):
            for p_name in player_names:
                if req_p in p_name.lower():
                    selected_player = p_name
                    break

        if not selected_player:
            if len(player_names) == 1:
                selected_player = player_names[0]
            elif 'MPV' in available_players:
                selected_player = 'MPV'
            elif self.console:
                from rich.prompt import Prompt
                from rich.panel import Panel
                from rich.text import Text
                from rich.align import Align
                
                options_text = "\n".join([f"[{i+1}] {name}" for i, name in enumerate(player_names)])
                panel = Panel(options_text, title=Text("Select Video Player", style="bold cyan"), border_style="cyan", padding=(1, 4))
                self.console.print()
                self.console.print(Align.center(panel))
                
                choice = Prompt.ask(
                    "Enter the number of the player", 
                    choices=[str(i+1) for i in range(len(player_names))], 
                    default="1", 
                    console=self.console
                )
                selected_player = player_names[int(choice)-1]
            else:
                selected_player = player_names[0]

        try:
            if selected_player == 'MPV':
                return self._play_mpv(
                    url,
                    title,
                    available_players['MPV'],
                    start_time=start_time,
                    audio_lang=audio_lang,
                    headers=headers,
                    subtitle_url=subtitle_url,
                    subtitle_lang=subtitle_lang,
                    audio_url=audio_url,
                    subtitles_disabled=subtitles_disabled,
                    hls_bitrate=hls_bitrate,
                )
            elif selected_player == 'Celluloid':
                return self._play_celluloid(
                    url,
                    title,
                    available_players['Celluloid'],
                    start_time=start_time,
                    audio_lang=audio_lang,
                    headers=headers,
                    subtitle_url=subtitle_url,
                    subtitle_lang=subtitle_lang,
                    audio_url=audio_url,
                    subtitles_disabled=subtitles_disabled,
                )
            elif selected_player == 'IINA':
                return self._play_iina(
                    url,
                    title,
                    available_players['IINA'],
                    start_time=start_time,
                    audio_lang=audio_lang,
                    headers=headers,
                    subtitle_url=subtitle_url,
                    subtitle_lang=subtitle_lang,
                    audio_url=audio_url,
                    subtitles_disabled=subtitles_disabled,
                )
            elif selected_player == 'Syncplay':
                return self._play_syncplay(
                    url,
                    title,
                    available_players['Syncplay'],
                    start_time=start_time,
                    audio_lang=audio_lang,
                    headers=headers,
                    subtitle_url=subtitle_url,
                    subtitle_lang=subtitle_lang,
                    audio_url=audio_url,
                    subtitles_disabled=subtitles_disabled,
                )
            elif selected_player == 'VLC':
                return self._play_vlc(
                    url,
                    title,
                    available_players['VLC'],
                    start_time=start_time,
                    audio_lang=audio_lang,
                    headers=headers,
                    subtitle_url=subtitle_url,
                    subtitle_lang=subtitle_lang,
                    audio_url=audio_url,
                    subtitles_disabled=subtitles_disabled,
                )
            elif selected_player == 'MPC-HC':
                return self._play_mpc(url, title, available_players['MPC-HC'])
        except Exception as e:
            if self.console:
                from rich.text import Text
                self.console.print(Text(f"Error launching player: {str(e)}", style="bold red"))
                input("Press Enter to continue...")
            else:
                print(f"Error launching player: {str(e)}", file=sys.stderr)
                input("Press Enter to continue...")
        return default_result

    def _play_vlc(
        self,
        url: str,
        title: str,
        vlc_path: str = None,
        start_time: float = 0.0,
        audio_lang: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        subtitle_url: Optional[str] = None,
        subtitle_lang: Optional[str] = None,
        audio_url: Optional[str] = None,
        subtitles_disabled: bool = False,
    ) -> dict:
        if not vlc_path:
            vlc_path = self.get_available_players().get('VLC')
        
        if not vlc_path:
            raise FileNotFoundError("VLC not found")

        referer = get_stream_referer(url)
        header_map = {str(k): str(v) for k, v in (headers or {}).items() if v}
        vlc_args = [
            vlc_path,
            '--fullscreen',
            '--play-and-exit',
            '--meta-title', title,
            # Sidecar subtitles are passed explicitly via --input-slave.
            '--no-sub-autodetect-file',
        ]
        if start_time and start_time > 0:
            vlc_args.append(f'--start-time={int(start_time)}')
        if audio_lang:
            # VLC matches a single language token reliably; "eng,en" is not a list.
            vlc_args.append(f'--audio-language={audio_lang.split(",")[0].strip()}')
        if header_map.get('Referer'):
            vlc_args.extend(['--http-referrer', header_map['Referer']])
        elif referer:
            vlc_args.extend(['--http-referrer', referer])
        if header_map.get('User-Agent'):
            vlc_args.extend(['--http-user-agent', header_map['User-Agent']])
        # VLC 3 has no generic extra-header option, so Origin/X-* cannot be
        # forwarded here. Playback still works for the providers that only
        # check Referer/User-Agent.
        if subtitles_disabled:
            vlc_args.append('--sub-track=-1')
        elif subtitle_url and subtitle_lang:
            vlc_args.append(f'--sub-language={subtitle_lang.split(",")[0].strip()}')
        slaves = [value for value in (audio_url, None if subtitles_disabled else subtitle_url) if value]
        if slaves:
            vlc_args.append('--input-slave=' + '#'.join(slaves))
            
        vlc_args.append(url)
        
        subprocess.run(
            vlc_args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        # VLC's CLI cannot report the final playback position.
        return {
            'time_pos': 0.0,
            'duration': 0.0,
            'percent': 0.0,
            'completed': False,
            'position_reported': False,
        }

    def _play_celluloid(
        self,
        url: str,
        title: str,
        celluloid_path: str = None,
        start_time: float = 0.0,
        audio_lang: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        subtitle_url: Optional[str] = None,
        subtitle_lang: Optional[str] = None,
        audio_url: Optional[str] = None,
        subtitles_disabled: bool = False,
    ) -> dict:
        if not celluloid_path:
            celluloid_path = self.get_available_players().get('Celluloid')
        if not celluloid_path:
            raise FileNotFoundError("Celluloid not found")

        referer = get_stream_referer(url)
        args = [celluloid_path]
        args.append('--mpv-sub-auto=no')
        if start_time and start_time > 0:
            args.append(f'--mpv-start={start_time}')
        if audio_lang:
            args.append(f'--mpv-alang={audio_lang}')
        if subtitles_disabled:
            args.append('--mpv-sid=no')
            args.append('--mpv-sub-auto=no')
        elif subtitle_url:
            args.append(f'--mpv-sub-file={subtitle_url}')
            if subtitle_lang:
                args.append(f'--mpv-slang={subtitle_lang}')
        if audio_url:
            args.append(f'--mpv-audio-file={audio_url}')

        ua = None
        ref = referer
        extra_headers = []
        for header_name, header_value in (headers or {}).items():
            if not header_value:
                continue
            lower_k = str(header_name).strip().lower()
            if lower_k == 'user-agent':
                ua = str(header_value).strip()
            elif lower_k == 'referer':
                ref = str(header_value).strip()
            else:
                extra_headers.append(f"{header_name}: {header_value}")

        if ua:
            args.append(f'--mpv-user-agent={ua}')
        if ref:
            args.append(f'--mpv-referrer={ref}')
        for header_field in extra_headers:
            args.append(f'--mpv-http-header-fields={header_field}')

        args.append(url)
        
        subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {
            'time_pos': 0.0,
            'duration': 0.0,
            'percent': 0.0,
            'completed': False,
            'position_reported': False,
        }

    def _play_iina(
        self,
        url: str,
        title: str,
        iina_path: str = None,
        start_time: float = 0.0,
        audio_lang: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        subtitle_url: Optional[str] = None,
        subtitle_lang: Optional[str] = None,
        audio_url: Optional[str] = None,
        subtitles_disabled: bool = False,
    ) -> dict:
        if not iina_path:
            iina_path = self.get_available_players().get('IINA')
        if not iina_path:
            raise FileNotFoundError("IINA not found")

        referer = get_stream_referer(url)
        args = [iina_path, '--keep-running', '--mpv-sub-auto=no']
        if start_time and start_time > 0:
            args.append(f'--mpv-start={start_time}')
        if audio_lang:
            args.append(f'--mpv-alang={audio_lang}')
        if subtitles_disabled:
            args.append('--mpv-sid=no')
            args.append('--mpv-sub-auto=no')
        elif subtitle_url:
            args.append(f'--mpv-sub-files={subtitle_url}')
            if subtitle_lang:
                args.append(f'--mpv-slang={subtitle_lang}')
        if audio_url:
            args.append(f'--mpv-audio-files={audio_url}')

        ua = None
        ref = referer
        extra_headers = []
        for header_name, header_value in (headers or {}).items():
            if not header_value:
                continue
            lower_k = str(header_name).strip().lower()
            if lower_k == 'user-agent':
                ua = str(header_value).strip()
            elif lower_k == 'referer':
                ref = str(header_value).strip()
            else:
                extra_headers.append(f"{header_name}: {header_value}")

        if ua:
            args.append(f'--mpv-user-agent={ua}')
        if ref:
            args.append(f'--mpv-referrer={ref}')
        for header_field in extra_headers:
            args.append(f'--mpv-http-header-fields={header_field}')

        args.append(url)
        
        subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {
            'time_pos': 0.0,
            'duration': 0.0,
            'percent': 0.0,
            'completed': False,
            'position_reported': False,
        }

    def _play_syncplay(
        self,
        url: str,
        title: str,
        syncplay_path: str = None,
        start_time: float = 0.0,
        audio_lang: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        subtitle_url: Optional[str] = None,
        subtitle_lang: Optional[str] = None,
        audio_url: Optional[str] = None,
        subtitles_disabled: bool = False,
    ) -> dict:
        if not syncplay_path:
            syncplay_path = self.get_available_players().get('Syncplay')
        if not syncplay_path:
            raise FileNotFoundError("Syncplay not found")

        args = [syncplay_path, url]
        mpv_args = ['--sub-auto=no']
        if start_time and start_time > 0:
            mpv_args.append(f'--start={start_time}')
        if audio_lang:
            mpv_args.append(f'--alang={audio_lang}')
        if subtitles_disabled:
            mpv_args.append('--sid=no')
            mpv_args.append('--sub-auto=no')
        elif subtitle_url:
            mpv_args.append(f'--sub-file={subtitle_url}')
            if subtitle_lang:
                mpv_args.append(f'--slang={subtitle_lang}')
        if audio_url:
            mpv_args.append(f'--audio-file={audio_url}')

        referer = get_stream_referer(url)
        ua = None
        ref = referer
        extra_headers = []
        for header_name, header_value in (headers or {}).items():
            if not header_value:
                continue
            lower_k = str(header_name).strip().lower()
            if lower_k == 'user-agent':
                ua = str(header_value).strip()
            elif lower_k == 'referer':
                ref = str(header_value).strip()
            else:
                extra_headers.append(f"{header_name}: {header_value}")

        if ua:
            mpv_args.append(f'--user-agent={ua}')
        if ref:
            mpv_args.append(f'--referrer={ref}')
        for header_field in extra_headers:
            mpv_args.append(f'--http-header-fields={header_field}')

        if mpv_args:
            args.extend(['--'] + mpv_args)

        subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {
            'time_pos': 0.0,
            'duration': 0.0,
            'percent': 0.0,
            'completed': False,
            'position_reported': False,
        }

    @staticmethod
    def _build_mpv_args(
        mpv_path: str,
        url: str,
        title: str,
        ipc_sock_path: str,
        start_time: float = 0.0,
        audio_lang: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        subtitle_url: Optional[str] = None,
        subtitle_lang: Optional[str] = None,
        audio_url: Optional[str] = None,
        subtitles_disabled: bool = False,
        hls_bitrate: Optional[int] = None,
    ) -> list:
        """Shared MPV argv for solo playback and Watch Together rooms."""
        referer = get_stream_referer(url)
        args = [
            mpv_path,
            '--fullscreen',
            '--force-window=yes',
            '--keep-open=yes',
            '--ontop',
            # 20s of readahead is enough to hide network jitter without
            # making the demuxer chew through the whole episode on a seek.
            '--cache=yes',
            '--cache-on-disk=no',
            '--demuxer-max-bytes=150M',
            '--demuxer-max-back-bytes=50M',
            '--demuxer-readahead-secs=20',
            '--cache-secs=60',
            '--cache-pause=yes',
            '--cache-pause-wait=3',
            '--cache-pause-initial=no',
            '--demuxer-seekable-cache=yes',
            f'--input-ipc-server={ipc_sock_path}',
            '--hr-seek=yes',
            '--hr-seek-framedrop=yes',
            '--stream-buffer-size=16MiB',
            '--network-timeout=15',
            '--tls-verify=no',
            '--hwdec=auto-safe',
            '--vo=gpu',
            '--profile=fast',
            # Never auto-load an unrelated local subtitle; explicit tracks are
            # passed with --sub-file and embedded tracks use --slang/--sid.
            '--sub-auto=no',
            '--really-quiet',
            '--terminal=no',
            '--msg-level=all=no',
            f'--force-media-title={title}',
        ]

        args.append(f'--start={start_time}' if start_time and start_time > 0 else '--start=0')

        # Arabic-first for dubbed Stardima titles; keep the existing Japanese
        # default for providers that do not set an audio preference.
        args.append(f'--alang={audio_lang or "jpn,ja,eng,en"}')
        if hls_bitrate and hls_bitrate > 0:
            # Pin the variant the user picked when playing a master playlist
            # (required for in-stream audio rendition selection).
            args.append(f'--hls-bitrate={int(hls_bitrate)}')
        if subtitles_disabled:
            args.append('--sid=no')
            args.append('--sub-auto=no')
        elif subtitle_url:
            args.append(f'--sub-file={subtitle_url}')
            if subtitle_lang:
                args.append(f'--slang={subtitle_lang},eng,en')
        else:
            # Arabic-first providers keep their previous subtitle preference.
            default_slang = "ara,ar,eng,en" if (audio_lang or "").lower().startswith("ara") else "eng,en,ara,ar"
            args.append(f'--slang={subtitle_lang or default_slang}')
        # Only a separate external audio track belongs in --audio-file; HLS
        # renditions are selected in-stream through --alang.
        if audio_url:
            args.append(f'--audio-file={audio_url}')

        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        ref = referer
        extra_headers = []
        for header_name, header_value in (headers or {}).items():
            if not header_value:
                continue
            lower_k = str(header_name).strip().lower()
            if lower_k == 'user-agent':
                ua = str(header_value).strip()
            elif lower_k == 'referer':
                ref = str(header_value).strip()
            else:
                extra_headers.append(f"{header_name}: {header_value}")

        if ua:
            args.append(f'--user-agent={ua}')
        if ref:
            args.append(f'--referrer={ref}')
        for header_field in extra_headers:
            args.append(f'--http-header-fields={header_field}')

        args.append(url)
        return args

    def _play_mpv(
        self,
        url: str,
        title: str,
        mpv_path: str = None,
        start_time: float = 0.0,
        audio_lang: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        subtitle_url: Optional[str] = None,
        subtitle_lang: Optional[str] = None,
        audio_url: Optional[str] = None,
        subtitles_disabled: bool = False,
        hls_bitrate: Optional[int] = None,
    ) -> dict:
        if not mpv_path:
            mpv_path = self.get_available_players().get('MPV')

        if not mpv_path or (mpv_path != 'mpv' and not os.path.exists(mpv_path)):
            raise FileNotFoundError(f"MPV not found at: {mpv_path}")

        # Setup IPC Socket
        is_win = (os.name == 'nt')
        ipc_id = f"ani_mpv_{os.getpid()}_{int(time.time()*1000)}"
        if is_win:
            sock_path = f"\\\\.\\pipe\\{ipc_id}"
        else:
            sock_path = os.path.join(tempfile.gettempdir(), f"{ipc_id}.sock")
            if os.path.exists(sock_path):
                try:
                    os.remove(sock_path)
                except Exception:
                    pass

        mpv_args = self._build_mpv_args(
            mpv_path,
            url,
            title,
            sock_path,
            start_time=start_time,
            audio_lang=audio_lang,
            headers=headers,
            subtitle_url=subtitle_url,
            subtitle_lang=subtitle_lang,
            audio_url=audio_url,
            subtitles_disabled=subtitles_disabled,
            hls_bitrate=hls_bitrate,
        )

        from .utils import (
            restore_terminal_for_input,
            enter_raw_mode_after_input,
            flush_stdin,
            show_cursor,
            hide_cursor,
        )
        restore_terminal_for_input()
        show_cursor()
        # Drop mouse/arrow bursts queued while MPV was focused so the menu
        # does not replay them as navigation once playback ends.
        flush_stdin()

        tracker = MpvTracker(sock_path)
        tracker.start()

        logger.log_player("MPV", mpv_args)
        t_start = time.time()
        result = subprocess.run(
            mpv_args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )
        duration = time.time() - t_start
        logger.log_player("MPV", mpv_args, exit_code=result.returncode, duration=duration)
        if result.returncode != 0 and result.stderr:
            logger.error("MPV", f"Player error output: {result.stderr.strip()}")
        
        tracker.stop()
        flush_stdin()
        enter_raw_mode_after_input()
        hide_cursor()

        if not is_win and os.path.exists(sock_path):
            try:
                os.remove(sock_path)
            except Exception:
                pass

        if result.returncode != 0:
            if self.console:
                from rich.text import Text
                self.console.print(Text("⚠ Stream closed or unreachable. Select another server if playback failed.", style="bold yellow"))
                time.sleep(1.0)

        state = dict(tracker.state)
        state['position_reported'] = True
        return state

    def play_mpv_session(
        self,
        url: str,
        title: str,
        start_time: float = 0.0,
        audio_lang: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        subtitle_url: Optional[str] = None,
        subtitle_lang: Optional[str] = None,
        audio_url: Optional[str] = None,
        subtitles_disabled: bool = False,
        hls_bitrate: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Launch MPV in the background and return a tracker with its IPC socket.
        The returned dict contains 'tracker' (MpvTracker), 'sock_path' (str),
        and 'process' (subprocess.Popen). The caller is responsible for cleaning up.
        """
        if not hasattr(self, 'get_available_players'):
            raise RuntimeError("PlayerManager is not fully initialized")

        # Determine MPV binary path
        if not hasattr(self, 'mpv_path'):
            self.mpv_path = self.get_available_players().get('MPV')
        mpv_path = self.mpv_path or 'mpv'
        if mpv_path == 'mpv' and not shutil.which('mpv'):
            raise FileNotFoundError("MPV not found on PATH")
        if mpv_path != 'mpv' and not os.path.exists(mpv_path):
            raise FileNotFoundError(f"MPV not found at: {mpv_path}")

        # Setup IPC Socket
        is_win = (os.name == 'nt')
        ipc_id = f"ani_mpv_watch_{os.getpid()}_{int(time.time()*1000)}"
        if is_win:
            sock_path = f"\\\\.\\pipe\\{ipc_id}"
        else:
            sock_path = os.path.join(tempfile.gettempdir(), f"{ipc_id}.sock")
            if os.path.exists(sock_path):
                try:
                    os.remove(sock_path)
                except Exception:
                    pass

        mpv_args = self._build_mpv_args(
            mpv_path,
            url,
            title,
            sock_path,
            start_time=start_time,
            audio_lang=audio_lang,
            headers=headers,
            subtitle_url=subtitle_url,
            subtitle_lang=subtitle_lang,
            audio_url=audio_url,
            subtitles_disabled=subtitles_disabled,
            hls_bitrate=hls_bitrate,
        )

        from .utils import flush_stdin

        # Create tracker and start monitoring
        tracker = MpvTracker(sock_path)
        tracker.start()

        logger.log_player("MPV", mpv_args)
        t_start = time.time()
        # Launch MPV in background
        process = subprocess.Popen(
            mpv_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Clear queued mouse escapes so the room UI does not act on stale input.
        flush_stdin()
        duration = time.time() - t_start
        logger.log_player("MPV", mpv_args, exit_code=process.returncode, duration=duration)
        if process.returncode is not None and process.returncode != 0:
            logger.error("MPV", f"Player error output (returned immediately): {process.stderr if process.stderr else ''}")
            tracker.stop()
            raise RuntimeError("MPV exited immediately")

        # Return the tracker, socket path, and process handle
        return {
            'tracker': tracker,
            'sock_path': sock_path,
            'process': process,
        }

    @staticmethod
    def send_mpv_command(
        sock_path: str,
        command: list,
        request_id: Optional[int] = None,
        timeout: float = 1.5,
    ) -> Optional[Dict[str, Any]]:
        """Send a JSON command to MPV, never blocking longer than ``timeout``.

        Seeks are fire-and-forget: MPV may be busy demuxing and unable to reply
        immediately, so a slow/unresponsive player degrades to ``None`` instead
        of stalling the UI thread (or an asyncio worker) for seconds.
        """
        timeout = max(0.2, float(timeout))
        raw = (json.dumps({"command": command, "request_id": request_id}) + "\n").encode('utf-8')
        if os.name == 'nt':
            return PlayerManager._send_mpv_command_windows(
                sock_path, raw, request_id is not None, timeout
            )
        return PlayerManager._send_mpv_command_unix(
            sock_path, raw, command, request_id is not None, timeout
        )

    @staticmethod
    def _send_mpv_command_unix(
        sock_path: str,
        raw: bytes,
        command: list,
        wants_reply: bool,
        timeout: float,
    ) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(sock_path)
                s.sendall(raw)
                if not wants_reply:
                    return None

                data = b""
                while time.monotonic() < deadline and b"\n" not in data:
                    s.settimeout(max(0.05, deadline - time.monotonic()))
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    try:
                        return json.loads(data.decode('utf-8'))
                    except Exception:
                        return None
                return None
        except (socket.timeout, TimeoutError):
            logger.debug("MPV", f"IPC command timed out after {timeout:.1f}s: {command}")
            return None
        except Exception as e:
            raise RuntimeError(f"MPV IPC error: {e}")

    @staticmethod
    def _send_mpv_command_windows(
        sock_path: str,
        raw: bytes,
        wants_reply: bool,
        timeout: float,
    ) -> Optional[Dict[str, Any]]:
        """Named pipes have no read timeout; bound them with a worker thread."""
        outcome: Dict[str, Any] = {}

        def worker() -> None:
            try:
                with open(sock_path, 'r+b', buffering=0) as f:
                    f.write(raw)
                    f.flush()
                    if wants_reply:
                        outcome["reply"] = f.read(4096)
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            logger.debug("MPV", f"Windows IPC command timed out after {timeout:.1f}s")
            return None
        error = outcome.get("error")
        if error is not None:
            raise RuntimeError(f"Cannot reach MPV pipe {sock_path}: {error}")
        reply = outcome.get("reply")
        if reply:
            try:
                return json.loads(reply.decode('utf-8'))
            except Exception:
                return None
        return None

    def _play_mpc(self, url: str, title: str, mpc_path: str = None):
        if not mpc_path:
            mpc_path = self.get_available_players().get('MPC-HC')
            
        if not mpc_path:
            raise FileNotFoundError("MPC-HC not found")
            
        mpc_args = [
            mpc_path,
            url,
            '/fullscreen',
            '/play',
            '/close'
        ]
        
        subprocess.run(
            mpc_args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return {
            'time_pos': 0.0,
            'duration': 0.0,
            'percent': 0.0,
            'completed': False,
            'position_reported': False,
        }
