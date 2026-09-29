# Dub Checker: Project Scope

**Version:** 2.5.1
**Repository:** https://github.com/widowshyper/dub-checker (public, MIT license)
**Note:** All code is 100% AI-generated.

## Purpose

Dub Checker is a desktop app that scans an anime library and reports which shows are missing English dub audio. It also reports whether an English dub exists at all, so you know what can be replaced.

## Feature set

**Scanning**
- Scans either a local folder ("Local files") or a Sonarr server.
- Reads the audio tracks of every episode file.
- Supports Japanese, Korean and Chinese originals.
- Caches files it has already read, so a rescan only reads new or changed files.
- Stop keeps partial results.
- While scanning, each step is named and explained in plain words.
- Hard drive care options:
  - how many files are read at once, with a separate setting for network shares;
  - optional breaks while reading.

**Results**
- Every season is sorted into one of four groups: Needs English Audio, No Dub Exists, Fully Dubbed, or Check Manually.
- The group cards act as tabs. You can select several at once, drag them into any order, and show or hide them with tick boxes.
- An Airing tab and an Air status column show whether a show is airing, not out yet, on hiatus or finished, with the next episode date.
- Shows open into seasons, and seasons open into individual episodes.
- A coloured audio bar shows one block per episode, with a legend:
  - green: dual audio;
  - blue: English only;
  - red: original language only;
  - amber: unlabeled;
  - grey: unreadable.
- An episode window lists every audio track of each file.
- Search, an Air status filter, sorting, and CSV export.
- Shows can be dismissed (hidden everywhere, even after a rescan) and brought back from a Dismissed shows window.
- Results, open rows and selected tabs are kept after a restart. Clear results removes them.

**Actions (right-click menu)**
- Fix a wrong AniList match.
- Open the file location (after a Local files scan).
- Open the show's page in Sonarr.
- Ask Sonarr to search for replacements of original-only episodes.
- Search Nyaa for "show name dual audio" in the web browser.
- Open the show's AniList page.
- Dismiss the show.

**Settings**
- Theme: system, light or dark.
- AniList match confidence threshold.
- Air status source: AniList, Sonarr or Off.
- Sonarr connection:
  - address and API key (encrypted on Windows);
  - anime-only filter;
  - path mapping;
  - a connection test.
- Clear saved online info.

## How it works

1. **Read files.** Every episode's audio tracks are read with MediaInfo (ffprobe as a fallback), or taken from Sonarr's media info. Results are cached by path, size and date.
2. **Group into seasons.** Files are grouped by show folder and season number.
3. **Decide each season.**
   - A season with any dual-audio file is decided from your files alone.
   - Every other season is matched on AniList by fuzzy title matching and sequel chains. A dub counts as existing if any one of these says so:
     - AniList's English voice cast;
     - the MAL-Dubs community list;
     - your own files.
4. **Air status (optional).** It comes from AniList, or from Sonarr's series list and calendar.
5. **Save.** Results are written to `UserData/last_scan.json` and reloaded on the next start.

## What it relies on

**Runtime**
- Python 3.10+ with tkinter. The portable Windows exe bundles its own Python.
- Python packages: `requests`, `rapidfuzz`, `anitopy`, `pymediainfo`, `sv-ttk`.
- MediaInfo (bundled in the exe). ffprobe is downloaded only if MediaInfo can't be used.
- Windows DPAPI to encrypt the Sonarr API key (plain text in a user-only file on macOS/Linux).

**Online services**
- AniList GraphQL API (rate-limited to about 28 requests a minute; answers are cached).
- The MAL-Dubs list on GitHub (refreshed weekly).
- Sonarr API v3 (optional; works with Sonarr v3, v4 and v5).
- nyaa.si (only opened in the browser; nothing is downloaded).

**Storage** (all in the portable `UserData` folder next to the program)
- `config.json`: settings
- `overrides.json`: manual matches
- `dismissed.json`: dismissed shows
- `cache.db`: SQLite cache
- `last_scan.json`: saved results
- `dubInfo.json`: MAL-Dubs list
- `logs/`: log files

**Build and test**
- PyInstaller (`build.bat`) produces the portable one-folder app and zip.
- A unittest suite of 245 tests uses synthetic MKV files, a mock Sonarr server and fake AniList clients.

## Out of scope

- Downloading or replacing files. Replacements go through Sonarr, and Nyaa is only a search link.
- Editing or remuxing audio tracks.
- Code signing, an installer, and auto-update.
- Other *arr apps, such as Radarr for anime movies.
