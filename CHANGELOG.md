# Changelog

All notable changes to **NyoAnime** are documented here.

---

## [2.1.1] - 2026-11-02

### Changed
- **Rebrand**: Project, package, commands (`nyoanime` / `nyo`), config directory (`~/.nyoanime`), assets, and docs are now NyoAnime.
- **Local-only operation**: Removed analytics, crash telemetry, remote broadcasts, remote changelog fetching, and the network updater.
- **Legacy support prompts removed**: Their shortcuts, settings toggles, and external links are gone.
- **Discord Rich Presence** is disabled by default and only connects when `NYOANIME_DISCORD_CLIENT_ID` is set.
- **Animeify** no longer fetches remote credentials; it uses local overrides or its public fallback API.

### Fixed
- **Python 3.9 compatibility** for the dependency installer type annotation.
- **Crash dumps** now remain entirely local under `~/.nyoanime/crashes`.

---

## [2.1.0]

### Added
- Stardima and Stardima Kids providers with Arabic audio selection.
- Multi-server extraction: backup, Uqload, Mixdrop, LuluStream, SaveFiles, Mp4upload, Forafile, Firestream.
- Offline classification for anime, cartoons, and kids titles.
- Dynamic provider-aware shortcut hints on the home screen.
- Tab / Shift+Tab navigation in Settings with instant reloads.

### Fixed
- MPV header normalization for signed CDN streams.
- Guarded keybind dispatch for providers without certain features.

---

## [2.0.1]

### Fixed
- Batch selection rendering, range bounds, and signed-link expiry.
- Serialized link resolution to avoid provider rate limits.
- Referer forwarding for downloads and `.m3u8` rejection.
- Resume/skip-existing behavior and cancellation summaries.

---

## [2.0.0]

### Added
- Multi-provider engine with Anime3rb, Anime Slayer, Animeify, HiAnime, Stardima, and Stardima Kids.
- Native chunked batch downloader with resume and live progress.
- Rewritten interactive `fzf` mode with Arabic RTL handling.
- MPV, VLC, Celluloid, IINA, and Syncplay support with MPV IPC resume.
