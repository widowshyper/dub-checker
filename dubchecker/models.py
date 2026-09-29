"""Plain data types shared by the scanner, the online lookups and the UI."""
from __future__ import annotations

import functools
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum


class Cancelled(Exception):
    """Raised inside worker code when the user presses Stop."""


class Lang(Enum):
    """What an audio track is understood to be."""
    JAPANESE = "Japanese"
    KOREAN = "Korean"
    CHINESE = "Chinese"
    ENGLISH = "English"
    UNKNOWN = "Unknown"


# A show's own language: Japanese for anime, Korean for aeni, Chinese for donghua.
ORIGINAL_LANGS = (Lang.JAPANESE, Lang.KOREAN, Lang.CHINESE)


class FileStatus(Enum):
    """Generic labels; ``FileResult.status_text`` names the file's actual original language."""
    DUAL = "Dual audio"
    ORIGINAL_ONLY = "Original language only"
    ENGLISH_ONLY = "English only"
    UNLABELED = "Unlabeled audio"
    ERROR = "Couldn't read file"


STATUS_CODES: dict[FileStatus, str] = {FileStatus.DUAL: "d", FileStatus.ORIGINAL_ONLY: "o",
                                       FileStatus.ENGLISH_ONLY: "e", FileStatus.UNLABELED: "u", FileStatus.ERROR: "x"}
STATUS_BY_CODE: dict[str, FileStatus] = {code: status for status, code in STATUS_CODES.items()}


class Category(Enum):
    NEEDS_DUB = "Needs English Audio"
    NO_DUB = "No Dub Exists"
    FULLY_DUBBED = "Fully Dubbed"
    CHECK = "Check Manually"


_ID_TAG_RE = re.compile(
    r"\s*[\[{(]\s*(?:tvdb|tmdb|imdb|tvdbid|tmdbid|imdbid|anidb|anilist|mal)(?:id)?\s*[-_=: ]\s*[^\]})]*[\]})]",
    re.IGNORECASE)


def strip_id_tags(name: str) -> str:
    """Remove Plex/Sonarr id tags such as ``{tvdb-123}`` or ``[imdbid-tt123]``."""
    return re.sub(r"\s{2,}", " ", _ID_TAG_RE.sub("", name)).strip()


def season_key(folder_name: str, season: int | None) -> str:
    """The key used for manual matches; shared by folder and Sonarr scans."""
    return folder_name if season is None else f"{folder_name}|S{season}"


def season_sort_key(season: int | None) -> int:
    """Seasons in natural order: no season first, then 1, 2, ..., Specials last."""
    if season is None:
        return -1
    return 10_000 if season == 0 else season


@dataclass(frozen=True)
class VideoFile:
    path: str
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return re.split(r"[\\/]", self.path)[-1]


@dataclass
class AudioTrack:
    index: int
    language: str = ""   # the raw tag as found in the file
    title: str = ""
    codec: str = ""
    channels: str = ""
    lang: Lang = Lang.UNKNOWN
    ignored: bool = False  # e.g. a commentary track

    def to_dict(self) -> dict:
        return {"index": self.index, "language": self.language, "title": self.title,
                "codec": self.codec, "channels": self.channels}


@dataclass
class FileResult:
    path: str
    size: int = 0
    mtime: float = 0.0
    tracks: list[AudioTrack] = field(default_factory=list)
    status: FileStatus = FileStatus.UNLABELED
    error: str = ""
    source: str = "disk"  # "disk", "cache" or "sonarr"
    note: str = ""

    @property
    def name(self) -> str:
        return re.split(r"[\\/]", self.path)[-1]

    @property
    def original(self) -> Lang | None:
        """The show's own language in this file (Japanese, Korean or Chinese), if it has one."""
        return next((t.lang for t in self.tracks if not t.ignored and t.lang in ORIGINAL_LANGS), None)

    @property
    def status_text(self) -> str:
        """e.g. 'Japanese + English', 'Korean only', 'English only'."""
        language = self.original.value if self.original else "Original language"
        if self.status is FileStatus.DUAL:
            return f"{language} + English"
        if self.status is FileStatus.ORIGINAL_ONLY:
            return f"{language} only"
        return self.status.value


def language_word(files: list[FileResult]) -> str:
    """'Japanese' when all these files share that original language, else 'original-language'."""
    languages = {f.original for f in files}
    return languages.pop().value if len(languages) == 1 and None not in languages else "original-language"


@dataclass
class ShowGroup:
    """One season of one show: the unit that gets categorised."""
    key: str
    folder_name: str
    search_title: str
    year: int | None = None
    season: int | None = None
    files: list[VideoFile] = field(default_factory=list)

    @property
    def show_title(self) -> str:
        return self.search_title or strip_id_tags(self.folder_name)

    @property
    def season_label(self) -> str:
        if self.season is None:
            return ""
        return "Specials" if self.season == 0 else f"Season {self.season}"

    @property
    def display_title(self) -> str:
        label = self.season_label
        return f"{self.show_title} - {label}" if label else self.show_title


@dataclass
class AniListMatch:
    anilist_id: int
    title: str
    romaji: str = ""
    english: str = ""
    mal_id: int | None = None
    year: int | None = None
    format: str = ""
    episodes: int | None = None
    confidence: float = 0.0
    manual: bool = False
    note: str = ""

    @property
    def url(self) -> str:
        return f"https://anilist.co/anime/{self.anilist_id}"


@dataclass
class DubInfo:
    exists: bool | None  # None = couldn't check
    sources: list[str] = field(default_factory=list)
    incomplete: bool = False


@dataclass
class ShowResult:
    group: ShowGroup
    files: list[FileResult]
    category: Category
    match: AniListMatch | None = None
    dub: DubInfo | None = None
    notes: list[str] = field(default_factory=list)
    looked_up: bool = False
    stopped: bool = False

    @property
    def key(self) -> str:
        return self.group.key

    # A season's files don't change once it's been categorised (a re-check makes a new ShowResult),
    # so what the table asks for over and over is worked out once.
    @functools.cached_property
    def _counts(self) -> Counter[FileStatus]:
        return Counter(f.status for f in self.files)

    @functools.cached_property
    def audio_code(self) -> str:
        """One letter per episode, in order (see ``STATUS_CODES``), e.g. 'ddoe'. Used for the audio bars."""
        return "".join(STATUS_CODES[f.status] for f in self.files)

    def count(self, status: FileStatus) -> int:
        return self._counts[status]

    @property
    def episodes(self) -> int:
        return len(self.files)

    @property
    def dual(self) -> int:
        return self.count(FileStatus.DUAL)

    @property
    def original_only(self) -> int:
        return self.count(FileStatus.ORIGINAL_ONLY)
