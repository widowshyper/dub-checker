"""Labels, colours and helpers shared by the main window and the dialogs.

``ResultsModel`` holds the scan results and turns them into table rows; it has
no Tk code so it can be tested on its own.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from dubchecker.dub_sources import SOURCE_FILES
from dubchecker.models import Category, FileResult, FileStatus, Lang, ShowResult, season_sort_key

ASSETS = Path(__file__).resolve().parent / "assets"
INDENT = "  "  # two em spaces: season rows under their show, tracks under their file


@dataclass(frozen=True)
class GroupInfo:
    title: str
    description: str
    color: str  # a palette key
    empty: str


GROUPS: dict[Category, GroupInfo] = {
    Category.NEEDS_DUB: GroupInfo(
        "Needs English Audio", "An English dub exists, but some of your episodes only have the original "
                               "(Japanese, Korean or Chinese) audio.",
        "red", "Nothing here - no show is waiting for English audio."),
    Category.NO_DUB: GroupInfo(
        "No Dub Exists", "No English dub was found for these, so there's nothing to replace yet.",
        "grey", "Nothing here - every show you have that lacks English audio has a dub available."),
    Category.FULLY_DUBBED: GroupInfo(
        "Fully Dubbed", "Every episode already has English audio.",
        "green", "Nothing here yet."),
    Category.CHECK: GroupInfo(
        "Check Manually", "Dub Checker couldn't be sure about these - the notes say why.",
        "amber", "Nothing to check - Dub Checker was sure about everything."),
}

STATUS_COLORS: dict[FileStatus, str] = {  # palette keys, used for the audio bars and episode rows
    FileStatus.DUAL: "green",
    FileStatus.ENGLISH_ONLY: "blue",
    FileStatus.ORIGINAL_ONLY: "red",
    FileStatus.UNLABELED: "amber",
    FileStatus.ERROR: "grey",
}

# The key shown above the table.
LEGEND: tuple[tuple[FileStatus, str], ...] = (
    (FileStatus.DUAL, "Dual audio"),
    (FileStatus.ENGLISH_ONLY, "English only"),
    (FileStatus.ORIGINAL_ONLY, "Original language only (Japanese, Korean or Chinese)"),
    (FileStatus.UNLABELED, "Unlabeled"),
    (FileStatus.ERROR, "Couldn't read"),
)

EMPTY_BEFORE_SCAN = {
    "folder": "Choose your anime folder above, then click Scan library.",
    "sonarr": "Click Scan library to check the shows in Sonarr.",
}


# ---------------------------------------------------------------- cell text

def anilist_text(r: ShowResult) -> str:
    if r.match:
        return r.match.title
    if r.stopped:
        return "Not checked yet"
    if not r.looked_up and r.dub and SOURCE_FILES in r.dub.sources:
        return "Not needed - your files have English audio"
    return "No match found"


def confirmed_text(r: ShowResult) -> str:
    if r.dub is None:
        return "-"
    if r.dub.exists is None:
        return "Couldn't check"
    return ", ".join(r.dub.sources) if r.dub.sources else "-"


def match_text(r: ShowResult) -> str:
    if r.match is None:
        return "-"
    return "Your pick" if r.match.manual else f"{r.match.confidence:.0f}%"


def notes_text(r: ShowResult) -> str:
    return "; ".join(r.notes)


def season_name(r: ShowResult) -> str:
    return r.group.season_label or "Other episodes"


def dub_status_text(r: ShowResult) -> str:
    if r.dub is None:
        return "Not checked"
    if r.dub.exists is None:
        return "Couldn't check"
    if r.dub.exists:
        return "Exists - confirmed by " + ", ".join(r.dub.sources)
    return "No English dub found"


def match_info_text(r: ShowResult) -> str:
    if r.match is None:
        return anilist_text(r)
    if r.match.manual:
        return "You chose this match"
    return f"Matched automatically - {r.match.confidence:.0f}% sure"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


# ---------------------------------------------------------------- model

@dataclass
class ShowRow:
    show_id: str
    title: str
    seasons: list[ShowResult]      # seasons of this show in the current group
    all_seasons: list[ShowResult]  # every season of this show

    @property
    def single(self) -> bool:
        return len(self.all_seasons) == 1

    def total(self, attribute: str) -> int:
        return sum(getattr(s, attribute) for s in self.seasons)

    def seasons_text(self) -> str:
        n = len(self.all_seasons)
        return f"{n} seasons" if len(self.seasons) == n else f"{len(self.seasons)} of {n} seasons"

    def also_text(self, category: Category) -> str:
        """e.g. 'Also: Season 2 is in Fully Dubbed; Seasons 3 and 4 are in Check Manually'."""
        others: dict[Category, list[str]] = {}
        for s in self.all_seasons:
            if s.category is not category:
                others.setdefault(s.category, []).append(season_name(s))
        if not others:
            return ""
        parts = []
        for cat, names in others.items():
            if len(names) == 1:
                parts.append(f"{names[0]} is in {cat.value}")
            else:
                parts.append(f"{', '.join(names[:-1])} and {names[-1]} are in {cat.value}")
        return "Also: " + "; ".join(parts)


class ResultsModel:
    """Change results only through clear/add/replace, which keep the grouping by show up to date."""

    def __init__(self) -> None:
        self.results: dict[str, ShowResult] = {}
        self._shows: dict[str, list[ShowResult]] | None = None  # by_show(), until the next change

    def clear(self) -> None:
        self.results.clear()
        self._shows = None

    def add(self, result: ShowResult) -> None:
        self.results[result.key] = result
        self._shows = None

    def replace(self, result: ShowResult) -> bool:
        """Swap in a re-checked season, but only if it is still in the list."""
        if result.key not in self.results:
            return False
        self.add(result)
        return True

    def __len__(self) -> int:
        return len(self.results)

    def by_show(self) -> dict[str, list[ShowResult]]:
        """Show folder -> its seasons in season order. Worked out once per change, not per use."""
        if self._shows is None:
            shows: dict[str, list[ShowResult]] = {}
            for r in self.results.values():
                shows.setdefault(r.group.folder_name, []).append(r)
            for seasons in shows.values():
                seasons.sort(key=lambda s: season_sort_key(s.group.season))
            self._shows = shows
        return self._shows

    def show_count(self) -> int:
        return len(self.by_show())

    def counts(self) -> dict[Category, int]:
        """Number of shows (not seasons) in each group; a show can be in several."""
        counts = {c: 0 for c in Category}
        for seasons in self.by_show().values():
            for category in {s.category for s in seasons}:
                counts[category] += 1
        return counts

    def rows(self, category: Category, filter_text: str = "") -> list[ShowRow]:
        needle = filter_text.strip().casefold()
        rows = []
        for show_id, seasons in self.by_show().items():
            in_group = [s for s in seasons if s.category is category]
            if not in_group:
                continue
            row = ShowRow(show_id, seasons[0].group.show_title, in_group, seasons)
            if needle and not self._matches(row, needle):
                continue
            rows.append(row)
        rows.sort(key=lambda r: r.title.casefold())
        return rows

    @staticmethod
    def _matches(row: ShowRow, needle: str) -> bool:
        haystack = [row.title, row.show_id] + [s.match.title for s in row.seasons if s.match]
        haystack += [s.match.romaji for s in row.seasons if s.match]
        return any(needle in text.casefold() for text in haystack if text)


# ---------------------------------------------------------------- misc helpers

def asset_path(name: str) -> Path:
    return ASSETS / name


def open_url(url: str) -> None:
    webbrowser.open(url)


def open_path(path: Path | str) -> None:
    """Open a folder in Explorer / Finder / the file manager."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_file_location(path: str) -> bool:
    """Show a file in its folder (selected, where the system allows), or open a folder.

    Falls back to the parent folder if the file has gone. Returns False if nothing is there.
    """
    if os.path.isfile(path):
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            open_path(os.path.dirname(path))
        return True
    for folder in (path, os.path.dirname(path)):
        if folder and os.path.isdir(folder):
            open_path(folder)
            return True
    return False


def shared_folder(results: list[ShowResult]) -> str:
    """The folder holding all of a show's files, e.g. the show folder above its Season folders."""
    folders = sorted({os.path.dirname(f.path) for r in results for f in r.files})
    if not folders:
        return ""
    try:
        return os.path.commonpath(folders)
    except ValueError:  # files on different drives
        return folders[0]


NYAA_URL = "https://nyaa.si/"


def nyaa_query(title: str) -> str:
    """'<title> dual audio', cleaned so Nyaa doesn't read parts of the title as search operators.

    On Nyaa a word starting with '-' is excluded and '|' means "or", so "Frieren - Beyond..." would
    otherwise hide the results it's looking for.
    """
    words = re.sub(r"[|\"()]", " ", title).split()
    words = [w.lstrip("-") for w in words if w.strip("-")]
    return " ".join(words + ["dual", "audio"])


def nyaa_search_url(title: str) -> str:
    """A Nyaa search in the Anime category, most seeders first."""
    return NYAA_URL + "?" + urlencode({"f": "0", "c": "1_0", "q": nyaa_query(title), "s": "seeders", "o": "desc"})


def nyaa_titles(results: list[ShowResult]) -> list[str]:
    """The show's name, then AniList's romaji and English titles when they differ.

    Releases are often named with the romaji title ("Shingeki no Kyojin"), so that's worth a search too.
    """
    names: list[str] = []
    seen: set[str] = set()
    candidates = [results[0].group.show_title] if results else []
    for r in results:
        if r.match:
            candidates += [r.match.romaji, r.match.english]
    for name in candidates:
        key = " ".join(re.sub(r"[\W_]+", " ", name or "").casefold().split())
        if key and key not in seen:
            seen.add(key)
            names.append(name.strip())
    return names


def original_only_files(results: list[ShowResult]) -> list[str]:
    """File names of the episodes that need English audio."""
    return [f.name for r in results for f in r.files if f.status is FileStatus.ORIGINAL_ONLY]


def episode_label(f: FileResult) -> str:
    """The file name without its extension, e.g. 'Attack on Titan - S02E05'."""
    return os.path.splitext(f.name)[0]


def episode_note(f: FileResult) -> str:
    """What the Notes column says on an episode row."""
    if f.status is FileStatus.ERROR:
        return f"Couldn't read this file: {f.error}" if f.error else "Couldn't read this file"
    if f.note:
        return f"{f.status_text} - {f.note}"
    if f.status is FileStatus.UNLABELED:
        unknown = [str(n) for n, t in enumerate(f.tracks, 1) if not t.ignored and t.lang is Lang.UNKNOWN]
        if not unknown:
            return "No audio tracks"
        return f"Unlabeled - audio track {', '.join(unknown)} {'has' if len(unknown) == 1 else 'have'} no language"
    return f.status_text
