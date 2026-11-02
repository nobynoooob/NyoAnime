<div align="center">

  <h1>NyoAnime</h1>

  <p>
    <strong>Direct Terminal Anime Streamer &amp; Batch Downloader</strong><br>
    Stream and download anime in high definition with Arabic &amp; English subtitles directly from your terminal.
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+" />
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-22c55e?style=flat-square" alt="License" /></a>
  </p>

  <p>
    <strong><a href="README.ar.md">للتصفح باللغة العربية اضغط هنا</a></strong>
  </p>

  <hr />

  <p>
    <a href="#providers">Providers</a> &nbsp;•&nbsp;
    <a href="#installation">Installation</a> &nbsp;•&nbsp;
    <a href="#usage">Usage</a> &nbsp;•&nbsp;
    <a href="#keyboard-shortcuts">Shortcuts</a> &nbsp;•&nbsp;
    <a href="#configuration">Config</a> &nbsp;•&nbsp;
    <a href="#license">License</a>
  </p>

</div>

<br />

<div align="center">
  <img src="assets/showcase.gif" alt="NyoAnime showcase" />
</div>

---

## Features

- **Multi-provider engine** — Anime3rb, Anime Slayer, Animeify, HiAnime, Stardima, and Stardima Kids.
- **Native chunked downloader** — parallel ranges, pause/resume, live speed and ETA. No `aria2c` or `yt-dlp` required.
- **Lightweight interactive mode (`-i`)** — an `fzf`-driven search flow with Arabic RTL handling.
- **Multiple players** — MPV, VLC, Celluloid, IINA, and Syncplay. MPV IPC keeps exact-second resume positions.
- **Smart Arabic search** — normalization for hamza, taa marbuta, and Latinized titles.
- **Fully local config and history** — no analytics, telemetry, remote broadcasts, or update pings.

---

## Providers

Switch providers in Settings (`c` / `O`) or with the `-p` flag:

| Provider | CLI key | Language | Notes |
| :--- | :---: | :---: | :--- |
| **Anime3rb** | `anime3rb` | Arabic Sub | Direct CDN streams with quality selection |
| **Anime Slayer** | `anime_slayer` | Arabic Sub | Official API endpoints with backup servers |
| **Animeify** | `animeify` | Arabic Sub | Mirror servers and fast resolution switching |
| **HiAnime** | `hianime` | English Sub / Dub | English streams with dynamic referer resolution |
| **Stardima** | `stardima` | Arabic Dub | Multi-server streams with Arabic audio selection |
| **Stardima Kids** | `stardima_kids` | Arabic Dub | Dubbed cartoons and kids series |

---

## Installation

### Prerequisites

- **Python 3.9+** (`3.10`–`3.12` recommended)
- **MPV** — recommended player with timestamp resume
- **FFmpeg** — stream multiplexing
- **fzf** — required for `-i` interactive mode

### From source

```bash
git clone <repository-url> nyoanime
cd nyoanime
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Then launch:

```bash
nyoanime
# or the short alias
nyo
```

You can also run it without installing:

```bash
python3 main.py
```

---

## Usage

### Full terminal UI (default)

```bash
nyo
```

### Lightweight interactive search

```bash
nyo -i "One Piece"
nyo -i "ون بيس"
```

### Pick a provider

```bash
nyo -p anime3rb "Naruto"
nyo -p hianime "Attack on Titan"
nyo -p stardima "ناروتو"
nyo -p stardima_kids "سبونج بوب"
```

### Direct TUI search

```bash
nyo "Hunter x Hunter"
```

### Other flags

```bash
nyo -d              # verbose debug logging
nyo -v              # print version
nyo --help          # full flag reference
```

---

## Keyboard shortcuts

### Main TUI

| Key | Action |
| :---: | :--- |
| <kbd>↑</kbd> <kbd>↓</kbd> / <kbd>j</kbd> <kbd>k</kbd> | Navigate lists and menus |
| <kbd>Enter</kbd> | Search / confirm |
| <kbd>e</kbd> / <kbd>m</kbd> | Latest episodes / movies |
| <kbd>t</kbd> / <kbd>p</kbd> / <kbd>r</kbd> | Trending / popular / top rated |
| <kbd>g</kbd> / <kbd>s</kbd> | Genres / studios |
| <kbd>l</kbd> / <kbd>f</kbd> | History / favorites |
| <kbd>c</kbd> / <kbd>O</kbd> | Settings |
| <kbd>B</kbd> | Back |
| <kbd>Ctrl+C</kbd> / <kbd>Q</kbd> | Quit |

### MPV

| Key | Action |
| :---: | :--- |
| <kbd>Space</kbd> | Pause / resume |
| <kbd>←</kbd> / <kbd>→</kbd> | Seek 5 seconds |
| <kbd>[</kbd> / <kbd>]</kbd> | Playback speed |
| <kbd>F</kbd> | Fullscreen |
| <kbd>9</kbd> / <kbd>0</kbd> | Volume |
| <kbd>Q</kbd> | Stop and save resume position |

---

## Configuration

Preferences live at:

```text
~/.nyoanime/database/config.json
```

Open Settings with <kbd>c</kbd> or <kbd>O</kbd> to change:

- Active provider
- Stardima audio language (`dub`, `sub`, `both`)
- Default streaming and download quality
- Media player
- Auto-next episode
- Discord Rich Presence (disabled by default)
- Theme palette
- Download directory
- Debug logging

Discord Rich Presence only connects when `NYOANIME_DISCORD_CLIENT_ID` is set in your environment. No other network calls leave your machine beyond the providers you explicitly use.

---

## Development

```bash
pip install -r requirements.txt
python3 -m pytest -q
```

Build a standalone executable with `scripts/build.py` (PyInstaller required).

---

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).

<div align="center">
  <p>Created and maintained by <strong>NoBy</strong>.</p>
</div>
