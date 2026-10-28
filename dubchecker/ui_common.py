"""Labels, colours and helpers shared by the main window and the dialogs.

``ResultsModel`` holds the scan results and turns them into table rows; it has
no Tk code so it can be tested on its own.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from dubchecker.dub_sources import SOURCE_FILES
from dubchecker.models import (Category, FileResult, FileStatus, Lang, Rating, ShowResult, rating_from_match,
                               season_sort_key)

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

# The tabs: the four groups (by Category name) and Dub Coming - seasons from Needs English Audio whose
# missing dubs are all still on their way - plus English Only and Airing, which cut across them.
DUB_COMING = "DUB_COMING"
ENGLISH_ONLY = "ENGLISH_ONLY"
AIRING = "AIRING"
TAB_IDS: tuple[str, ...] = (Category.NEEDS_DUB.name, DUB_COMING,
                            *(c.name for c in Category if c is not Category.NEEDS_DUB), ENGLISH_ONLY, AIRING)
CROSS_TABS = frozenset({ENGLISH_ONLY, AIRING})  # a season can be in one of these as well as its group
TABS: dict[str, GroupInfo] = {
    **{c.name: info for c, info in GROUPS.items()},
    DUB_COMING: GroupInfo(
        "Dub Coming", "The English dub is on its way: these episodes aired recently, and dubs usually follow a "
                      "few weeks behind.",
        "purple", "Nothing here - no dub is on its way right now."),
    ENGLISH_ONLY: GroupInfo(
        "English Only", "Some episodes only have English audio. Replace them with dual-audio releases to get the "
                        "original language back too.",
        "blue", "Nothing here - no episode is missing its original-language audio."),
    AIRING: GroupInfo(
        "Airing", "Still airing or not out yet, so an English dub may still be on its way.",
        "teal", "Nothing airing - or air status hasn't been checked yet. Scan again, or choose where it comes "
                  "from in Settings > General > Air status."),
}

AIR_LABELS = {"RELEASING": "Airing", "NOT_YET_RELEASED": "Not yet aired", "HIATUS": "On hiatus",
              "FINISHED": "Finished", "CANCELLED": "Cancelled"}
_AIR_ORDER = {"RELEASING": 0, "NOT_YET_RELEASED": 1, "HIATUS": 2, "FINISHED": 3, "CANCELLED": 4}
# The Air status drop-down above the list, in config.AIR_FILTERS order.
AIR_FILTER_LABELS = {"all": "Any air status", **AIR_LABELS, "NONE": "Not checked"}


def ordered_tabs(saved_order: list[str]) -> list[str]:
    """Every tab, in the order the user dragged them into; new or unknown ones fall back to the default."""
    order = [tab for tab in dict.fromkeys(saved_order) if tab in TABS]
    return order + [tab for tab in TAB_IDS if tab not in order]


def air_matches(r: ShowResult, air_filter: str) -> bool:
    """Whether a season passes the Air status drop-down."""
    if air_filter == "all":
        return True
    status = r.air.status if r.air and r.air.status else "NONE"
    return status == air_filter


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
    (FileStatus.ORIGINAL_ONLY, "Original language only"),
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


def rating_of(r: ShowResult) -> Rating | None:
    """Sonarr's rating for the series, else AniList's average score for the season."""
    return r.rating or rating_from_match(r.match)


def show_rating(seasons: list[ShowResult]) -> float | None:
    """A whole show's rating: Sonarr's (one per series), else the average of its seasons' AniList scores."""
    ratings = [rating for rating in map(rating_of, seasons) if rating is not None]
    sonarr = [rating.value for rating in ratings if rating.source == "Sonarr"]
    values = sonarr[:1] or [rating.value for rating in ratings]
    return sum(values) / len(values) if values else None


def rating_text(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def rating_detail(r: ShowResult) -> str:
    """For the episode window, e.g. '8.6 out of 10, from Sonarr (1,234 votes)'."""
    rating = rating_of(r)
    if rating is None:
        return "No rating - Sonarr doesn't rate this show and AniList has no score for it"
    votes = f" ({plural(rating.votes, 'vote')})" if rating.votes else ""
    where = "Sonarr" if rating.source == "Sonarr" else "AniList's average score"
    return f"{rating.value:.1f} out of 10, from {where}{votes}"


def notes_text(r: ShowResult) -> str:
    return "; ".join(([dub_coming_note(r)] if r.dub_coming else []) + list(r.notes))


def short_date(when: float) -> str:
    """e.g. '3 Nov'."""
    local = time.localtime(when)
    return f"{local.tm_mday} {time.strftime('%b', local)}"


def dub_coming_note(r: ShowResult) -> str:
    if r.dub_expected:
        return f"Dub on its way - expected by {short_date(r.dub_expected)}"
    return "Dub on its way - still airing, and the dub is catching up"


def pending_note(r: ShowResult) -> str:
    """Added to an episode waiting for its dub."""
    return f"dub expected by {short_date(r.dub_expected)}" if r.dub_expected else "dub on its way"


def group_tab(r: ShowResult) -> str:
    """The tab a season's group is shown as: its category, or Dub Coming."""
    return DUB_COMING if r.dub_coming else r.category.name


def group_title(r: ShowResult) -> str:
    return TABS[group_tab(r)].title


def display_code(r: ShowResult) -> str:
    """``audio_code`` with "p" for the episodes still waiting for their dub, for the audio bars."""
    if not r.pending:
        return r.audio_code
    return "".join("p" if i in r.pending else letter for i, letter in enumerate(r.audio_code))


DAY = 86400


def mark_dub_timing(r: ShowResult, grace_days: int, now: float | None = None) -> bool:
    """Work out whether a Needs English Audio season's missing dubs are probably just not out yet, since
    dubs of airing shows usually follow a few weeks behind. Sets ``pending``, ``dub_coming`` and
    ``dub_expected``; returns whether ``dub_coming`` changed.

    * With air dates (``FileResult.aired_at``, from Sonarr or AniList): an original-only episode that aired
      less than ``grace_days`` ago is waiting for its dub. The season is Dub Coming when all of them are.
    * Without any: the season is Dub Coming while it's still airing, its dub has started (an episode with
      English comes first), and only the newest episodes lack English - the dub catching up.
    """
    was = r.dub_coming
    r.dub_coming, r.dub_expected, r.pending = False, None, frozenset()
    if grace_days <= 0 or r.category is not Category.NEEDS_DUB or r.stopped:
        return was
    now = time.time() if now is None else now
    waiting = [i for i, f in enumerate(r.files) if f.status is FileStatus.ORIGINAL_ONLY]
    if not waiting:
        return was
    dated = {i: r.files[i].aired_at for i in waiting if r.files[i].aired_at}
    if dated:
        recent = {i for i, aired in dated.items() if aired + grace_days * DAY > now}
        r.pending = frozenset(recent)
        if recent and len(recent) == len(waiting):  # every missing dub is recent; an undated one counts as missing
            r.dub_coming, r.dub_expected = True, int(max(dated.values()) + grace_days * DAY)
    elif r.air is not None and r.air.still_airing:
        first = waiting[0]
        started = any(f.status in (FileStatus.DUAL, FileStatus.ENGLISH_ONLY) for f in r.files[:first])
        if started and all(f.status is FileStatus.ORIGINAL_ONLY for f in r.files[first:]):
            r.dub_coming, r.pending = True, frozenset(waiting)
    return r.dub_coming != was


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

def in_tabs(r: ShowResult, tabs: set[str]) -> bool:
    """Whether a season is listed when these tabs are selected. No tabs selected lists everything."""
    if not tabs:
        return True
    return (group_tab(r) in tabs
            or (ENGLISH_ONLY in tabs and r.english_only > 0)
            or (AIRING in tabs and r.air is not None and r.air.still_airing))


def air_status_text(r: ShowResult, now: float | None = None) -> str:
    """e.g. 'Airing - ep 8 on 3 Oct', 'Finished', or '-' when it hasn't been checked."""
    air = r.air
    if air is None or not air.status:
        return "-"
    label = AIR_LABELS.get(air.status, air.status.title())
    if air.next_airing_at:
        when = time.localtime(air.next_airing_at)
        date = f"{when.tm_mday} {time.strftime('%b', when)}"
        verb = "on" if air.next_airing_at > (time.time() if now is None else now) else "aired"
        episode = f"ep {air.next_episode} " if air.next_episode else "next episode "
        return f"{label} - {episode}{verb} {date}"
    return label


def air_sort_key(r: ShowResult) -> tuple:
    """Airing shows first, soonest next episode first; unchecked last."""
    air = r.air
    return (_AIR_ORDER.get(air.status if air else "", 9), (air.next_airing_at or 0) if air else 0)


def show_air_status(seasons: list[ShowResult]) -> ShowResult | None:
    """The season whose air status speaks for the show: one still airing, else the latest checked one."""
    checked = [s for s in seasons if s.air and s.air.status]
    airing = [s for s in checked if s.air.still_airing]  # type: ignore[union-attr]
    return (airing or checked or [None])[-1]


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

    def also_text(self) -> str:
        """The seasons not listed here, e.g. 'Also: Season 2 is in Fully Dubbed; Seasons 3 and 4 are in ...'."""
        shown = {id(s) for s in self.seasons}
        others: dict[str, list[str]] = {}
        for s in self.all_seasons:
            if id(s) not in shown:
                others.setdefault(group_title(s), []).append(season_name(s))
        if not others:
            return ""
        parts = []
        for group, names in others.items():
            if len(names) == 1:
                parts.append(f"{names[0]} is in {group}")
            else:
                parts.append(f"{', '.join(names[:-1])} and {names[-1]} are in {group}")
        return "Also: " + "; ".join(parts)


class ResultsModel:
    """Change results only through clear/add/replace, which keep the grouping by show up to date.

    Shows in ``dismissed`` (show folder names) are left out of the tabs, their counts and the list.
    """

    def __init__(self) -> None:
        self.results: dict[str, ShowResult] = {}
        self._shows: dict[str, list[ShowResult]] | None = None  # by_show(), until the next change
        self.dismissed: frozenset[str] = frozenset()
        self.dub_grace_days = 0  # see mark_dub_timing; the main window sets it from the settings

    def clear(self) -> None:
        self.results.clear()
        self._shows = None

    def add(self, result: ShowResult) -> None:
        mark_dub_timing(result, self.dub_grace_days)
        self.results[result.key] = result
        self._shows = None

    def refresh_dub_timing(self, grace_days: int | None = None) -> bool:
        """Work out again which dubs are still on their way (time passes, or the setting changed).
        Returns whether any season moved between Needs English Audio and Dub Coming."""
        if grace_days is not None:
            self.dub_grace_days = grace_days
        now = time.time()
        changed = False
        for r in self.results.values():
            changed = mark_dub_timing(r, self.dub_grace_days, now) or changed
        return changed

    def replace(self, result: ShowResult) -> bool:
        """Swap in a re-checked season, but only if it is still in the list."""
        if result.key not in self.results:
            return False
        self.add(result)
        return True

    def remove(self, keys: list[str]) -> None:
        """Forget seasons, e.g. ones that are gone from the library."""
        for key in keys:
            self.results.pop(key, None)
        self._shows = None

    def shows_in_tab(self, tab: str) -> set[str]:
        """The shows (folder names) with a season under ``tab``, leaving out dismissed ones."""
        return {show_id for show_id, seasons in self.by_show().items()
                if show_id not in self.dismissed and any(in_tabs(s, {tab}) for s in seasons)}

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

    def counts(self, air_filter: str = "all") -> dict[str, int]:
        """Number of shows (not seasons) under each tab (see TAB_IDS); a show can be under several.
        With ``air_filter``, only seasons that pass it count, as in ``rows``."""
        counts = dict.fromkeys(TAB_IDS, 0)
        for show_id, seasons in self.by_show().items():
            if show_id in self.dismissed:
                continue
            seasons = [s for s in seasons if air_matches(s, air_filter)]
            for name in {group_tab(s) for s in seasons}:
                counts[name] += 1
            for tab in CROSS_TABS:
                if any(in_tabs(s, {tab}) for s in seasons):
                    counts[tab] += 1
        return counts

    def rows(self, tabs: set[str], filter_text: str = "", air_filter: str = "all") -> list[ShowRow]:
        """The shows with a season under any of ``tabs`` (none selected = every show) whose air status
        passes ``air_filter``. Dismissed shows are left out."""
        needle = filter_text.strip().casefold()
        rows = []
        for show_id, seasons in self.by_show().items():
            if show_id in self.dismissed:
                continue
            in_group = [s for s in seasons if in_tabs(s, tabs) and air_matches(s, air_filter)]
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


def english_only_files(results: list[ShowResult]) -> list[str]:
    """File names of the episodes that need their original-language audio back (a dual-audio release)."""
    return [f.name for r in results for f in r.files if f.status is FileStatus.ENGLISH_ONLY]


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
