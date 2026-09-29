"""Listing the library, cleaning titles and grouping files into seasons.

Performance note: folders are listed with ``os.scandir`` and the size and
modification time come from ``DirEntry.stat()``. On Windows those arrive with
the directory listing itself, so a cached rescan of an SMB share needs no
per-file network requests. Never call ``os.stat`` per file in this module.
"""
from __future__ import annotations

import logging
import ntpath
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date

from dubchecker.models import ShowGroup, VideoFile, season_key, season_sort_key, strip_id_tags

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm"})
SKIP_FOLDERS = frozenset({"@eadir", "#recycle", "$recycle.bin", "system volume information", "#snapshot"})
FILE_ATTRIBUTE_HIDDEN = 0x2
MAX_DEPTH = 6


# ---------------------------------------------------------------- listing

def natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def _is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith(".") or entry.name.casefold() in SKIP_FOLDERS:
        return True  # also covers macOS "._" resource files
    if os.name == "nt":
        try:  # served from the directory listing, no extra request
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & FILE_ATTRIBUTE_HIDDEN)
        except OSError:
            return False
    return False


@dataclass
class FolderListing:
    files: list[VideoFile] = field(default_factory=list)
    folders: list[tuple[str, str]] = field(default_factory=list)  # (name, path)


class FolderLister:
    """Lists one folder at a time and keeps count for the scan statistics."""

    def __init__(self) -> None:
        self.folders_listed = 0
        self.seconds = 0.0

    def __call__(self, path: str) -> FolderListing:
        started = time.monotonic()
        listing = FolderListing()
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if _is_hidden(entry):
                        continue
                    if entry.is_dir():
                        listing.folders.append((entry.name, entry.path))
                    elif os.path.splitext(entry.name)[1].lower() in VIDEO_EXTENSIONS and entry.is_file():
                        st = entry.stat()
                        listing.files.append(VideoFile(entry.path, st.st_size, st.st_mtime))
                except OSError as exc:
                    log.warning("Skipping %s: %s", entry.path, exc)
        listing.files.sort(key=lambda f: natural_key(f.name))
        listing.folders.sort(key=lambda item: natural_key(item[0]))
        self.folders_listed += 1
        self.seconds += time.monotonic() - started
        return listing


# ---------------------------------------------------------------- seasons

_SEASON_DIR_RE = re.compile(r"^(?:season|series|staffel|saison|temporada)[\s._-]*(\d{1,3})\b", re.I)
_SHORT_SEASON_DIR_RE = re.compile(r"^s[\s._-]*(\d{1,3})$", re.I)
_SPECIALS_DIR_RE = re.compile(r"^(?:specials?|extras?|ovas?|oads?)$", re.I)
_SXXEYY_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[\s._-]?e\d{1,4}(?![0-9])", re.I)


def season_from_folder(name: str) -> int | None:
    """``Season 02`` -> 2, ``S 3`` -> 3, ``Specials`` -> 0, anything else -> None."""
    name = strip_id_tags(name).strip()
    if _SPECIALS_DIR_RE.match(name):
        return 0
    match = _SEASON_DIR_RE.match(name) or _SHORT_SEASON_DIR_RE.match(name)
    return int(match.group(1)) if match else None


def season_from_filename(name: str) -> int | None:
    match = _SXXEYY_RE.search(name)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------- titles

_NOISE = (
    r"\d{3,4}p|4k|uhd|[xh][\s.]?26[45]|hevc|avc|xvid|divx|10[\s.-]?bits?|8[\s.-]?bits?|hi10p?|hdr(?:10)?"
    r"|blu[\s-]?ray|bdrip|brrip|bdremux|bd|remux|web[\s-]?dl|web[\s-]?rip|web|hdtv|dvdrip|dvd"
    r"|aac(?:[\s.]?[257][\s.][01])?|flac|e?-?ac-?3|ddp?(?:[\s.]?[257][\s.][01])?|dts(?:-hd)?|opus|truehd|atmos"
    r"|dual[\s._-]?audio|multi[\s._-]?subs?|multi[\s._-]?audio|batch|uncensored"
)
_NOISE_RE = re.compile(rf"(?<![a-z0-9])(?:{_NOISE})(?![a-z0-9])", re.I)
_RELEASE_SEASON_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})(?:[\s._-]?e\d{1,4})?(?![a-z0-9])", re.I)
_BRACKET_SEASON_RE = re.compile(r"[\[(]\s*season\s*(\d{1,2})\s*[\])]", re.I)
_PAREN_YEAR_RE = re.compile(r"\(\s*(19[5-9]\d|20[0-4]\d)\s*\)")
_BARE_YEAR_RE = re.compile(r"(?<![0-9])(19[5-9]\d|20\d\d)(?![0-9])")
_LATEST_YEAR = date.today().year + 2  # "Blade Runner 2049" is a title, not a year
_BRACKETS_RE = re.compile(r"\[[^\]]*\]|\{[^}]*\}|\([^)]*\)")
_TRAILING_GROUP_RE = re.compile(r"(?<=[a-z0-9])-[a-z0-9]+\s*$", re.I)
_SEASON_SUFFIX_RES = (
    re.compile(r"[\s:-]+season\s*(\d{1,2})\s*$", re.I),
    re.compile(r"\s+s(\d{1,2})\s*$", re.I),
    re.compile(r"[\s:-]+(\d{1,2})(?:st|nd|rd|th)\s+season\s*$", re.I),
)


@dataclass
class CleanTitle:
    title: str
    year: int | None = None
    season: int | None = None


def clean_title(name: str) -> CleanTitle:
    """Turn a folder or release name into a searchable title, year and season."""
    text = strip_id_tags(name).strip()
    year: int | None = None
    season: int | None = None

    match = _BRACKET_SEASON_RE.search(text)
    if match:
        season = int(match.group(1))
        text = text[:match.start()] + " " + text[match.end():]
    match = _PAREN_YEAR_RE.search(text)
    if match:
        year = int(match.group(1))
        text = text[:match.start()] + " " + text[match.end():]
    text = _BRACKETS_RE.sub(" ", text).strip()

    dotted = " " not in text and ("." in text or "_" in text)
    if dotted:
        text = re.sub(r"[._]+", " ", text)

    # Release-style names (noise words like 1080p, or S01E02) are cut at the first marker.
    noise = next((m for m in _NOISE_RE.finditer(text) if m.start() > 0), None)
    release_season = next((m for m in _RELEASE_SEASON_RE.finditer(text) if m.start() > 0), None)
    if noise or release_season:
        text = _TRAILING_GROUP_RE.sub("", text)  # "...x264-GROUP"; "86 EIGHTY-SIX" is not release style
        if release_season and season is None:
            season = int(release_season.group(1))
        text = text[:min(m.start() for m in (noise, release_season) if m)]

    if year is None:
        for match in _BARE_YEAR_RE.finditer(text):
            if text[:match.start()].strip() and int(match.group(1)) <= _LATEST_YEAR:  # not the first word
                year = int(match.group(1))
                text = text[:match.start()] if dotted else text[:match.start()] + " " + text[match.end():]
                break

    text = " ".join(text.split()).strip(" -_.:,")
    for pattern in _SEASON_SUFFIX_RES:
        match = pattern.search(text)
        if match and match.start() > 0:
            if season is None:
                season = int(match.group(1))
            text = text[:match.start()].strip(" -_.:,")
            break

    if not text:
        text = strip_id_tags(name).strip()
    return CleanTitle(text, year, season)


def parse_release_name(filename: str) -> CleanTitle:
    """Title and season of a loose episode file, using anitopy with a regex fallback."""
    stem = os.path.splitext(filename)[0]
    try:
        import anitopy
        parsed = anitopy.parse(filename) or {}
        title = parsed.get("anime_title")
        if title:
            cleaned = clean_title(str(title))
            season = parsed.get("anime_season")
            if isinstance(season, list):
                season = season[0] if season else None
            if season is not None and str(season).isdigit():
                cleaned.season = int(season)
            elif cleaned.season is None:
                cleaned.season = season_from_filename(filename)
            year = parsed.get("anime_year")
            if cleaned.year is None and year and str(year).isdigit():
                cleaned.year = int(year)
            return cleaned
    except Exception as exc:  # anitopy can choke on odd names
        log.debug("anitopy couldn't parse %r: %s", filename, exc)

    text = _BRACKETS_RE.sub(" ", stem).strip()
    if " " not in text:
        text = re.sub(r"[._]+", " ", text)
    season = season_from_filename(text)
    match = _SXXEYY_RE.search(text)
    if match:
        text = text[:match.start()]
    else:
        text = re.split(r"\s+-\s+", text)[0]
        text = re.sub(r"\s+(?:e|ep|episode)?\s*\d{1,4}(?:v\d)?\s*$", "", text, flags=re.I)
    cleaned = clean_title(text)
    if cleaned.season is None:
        cleaned.season = season
    return cleaned


# ---------------------------------------------------------------- library

@dataclass
class ShowFolder:
    name: str
    path: str
    listing: FolderListing | None = None           # reused when already listed
    loose_files: list[VideoFile] | None = None     # set for the loose-files batch


def list_library(root: str, lister: FolderLister) -> list[ShowFolder]:
    """The shows in a library folder, A to Z, with loose files in the root last.

    If the chosen folder itself contains Season folders, it is a single show.
    """
    top = lister(root)
    if any(season_from_folder(name) is not None for name, _ in top.folders):
        name = ntpath.basename(root.rstrip("\\/")) if os.name == "nt" else os.path.basename(root.rstrip("/"))
        return [ShowFolder(name or root, root, listing=top)]
    shows = [ShowFolder(name, path) for name, path in top.folders]
    if top.files:
        shows.append(ShowFolder("Loose files", root, loose_files=top.files))
    return shows


def group_show(show: ShowFolder, lister: FolderLister) -> list[ShowGroup]:
    """Split one show folder into seasons."""
    if show.loose_files is not None:
        return group_loose_files(show.loose_files)

    cleaned = clean_title(show.name)
    by_season: dict[int | None, list[VideoFile]] = {}

    def walk(listing: FolderListing, folder_season: int | None, depth: int) -> None:
        for f in listing.files:
            season = folder_season
            if season is None:
                season = cleaned.season if cleaned.season is not None else season_from_filename(f.name)
            by_season.setdefault(season, []).append(f)
        if depth >= MAX_DEPTH:
            return
        for name, path in listing.folders:
            sub_season = season_from_folder(name)
            try:
                sub = lister(path)
            except OSError as exc:
                log.warning("Couldn't list %s: %s", path, exc)
                continue
            walk(sub, sub_season if sub_season is not None else folder_season, depth + 1)

    walk(show.listing if show.listing is not None else lister(show.path), None, 0)
    return [
        ShowGroup(key=season_key(show.name, season), folder_name=show.name, search_title=cleaned.title,
                  year=cleaned.year, season=season, files=files)
        for season, files in sorted(by_season.items(), key=lambda item: season_sort_key(item[0]))
    ]


def group_loose_files(files: list[VideoFile]) -> list[ShowGroup]:
    """Loose episode files: parse each name and group by title and season."""
    groups: dict[tuple[str, int | None], ShowGroup] = {}
    for f in files:
        parsed = parse_release_name(f.name)
        ident = (parsed.title.casefold(), parsed.season)
        if ident not in groups:
            groups[ident] = ShowGroup(key=season_key(parsed.title, parsed.season), folder_name=parsed.title,
                                      search_title=parsed.title, year=parsed.year, season=parsed.season)
        groups[ident].files.append(f)
    return sorted(groups.values(), key=lambda g: (natural_key(g.folder_name), season_sort_key(g.season)))


# ---------------------------------------------------------------- network shares

_NETWORK_FS = {"cifs", "smb3", "smbfs", "smb", "nfs", "nfs4", "afpfs", "fuse.sshfs", "davfs", "webdav"}


def _mount_for(path: str, mounts: list[tuple[str, str]]) -> str | None:
    """File system type of the longest mount point containing ``path``."""
    best, best_type = "", None
    for mount_point, fs_type in mounts:
        mp = mount_point.rstrip("/") or "/"
        if (path == mp or path.startswith(mp.rstrip("/") + "/") or mp == "/") and len(mp) > len(best):
            best, best_type = mp, fs_type
    return best_type


def parse_proc_mounts(text: str) -> list[tuple[str, str]]:
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mounts.append((parts[1].replace("\\040", " "), parts[2].lower()))
    return mounts


def parse_mount_output(text: str) -> list[tuple[str, str]]:
    """macOS ``mount`` lines: ``//user@nas/share on /Volumes/share (smbfs, nodev, ...)``."""
    mounts = []
    for line in text.splitlines():
        match = re.match(r".+? on (.+) \(([^,)]+)", line)
        if match:
            mounts.append((match.group(1), match.group(2).strip().lower()))
    return mounts


def is_network_path(path: str) -> bool:
    """True for UNC paths, mapped network drives and SMB/NFS mounts."""
    if not path:
        return False
    if path.startswith(("\\\\", "//")):
        return True
    try:
        if os.name == "nt":
            drive = ntpath.splitdrive(os.path.abspath(path))[0]
            if len(drive) != 2:
                return drive.startswith("\\\\")
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
        absolute = os.path.abspath(path)
        if sys.platform.startswith("linux"):
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                return _mount_for(absolute, parse_proc_mounts(fh.read())) in _NETWORK_FS
        if sys.platform == "darwin":
            output = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
            return _mount_for(absolute, parse_mount_output(output)) in _NETWORK_FS
    except (OSError, subprocess.SubprocessError, AttributeError) as exc:
        log.debug("Network check failed for %s: %s", path, exc)
    return False
