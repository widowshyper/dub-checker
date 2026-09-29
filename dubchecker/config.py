"""Settings, manual matches and the portable (drive-letter-free) path helpers.

Everything lives in ``UserData`` next to the program so the whole folder can be
moved between PCs or carried on a USB stick.
"""
from __future__ import annotations

import functools
import json
import logging
import ntpath
import os
import sys
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)


@functools.cache
def base_dir() -> Path:
    """The folder the program lives in (the exe's folder when frozen).

    Cached: resolve() asks the file system, and the portable-path helpers need this for every file.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    return base_dir() / "UserData"


@functools.cache
def app_drive() -> str:
    """The drive letter the app runs from, e.g. ``E:`` (empty off Windows)."""
    if os.name != "nt":
        return ""
    return ntpath.splitdrive(str(base_dir()))[0]


def _is_letter_drive(drive: str) -> bool:
    return len(drive) == 2 and drive[1] == ":"


def to_portable(path: str, drive: str | None = None) -> str:
    r"""``E:\Anime`` -> ``\Anime`` when the app itself runs from E:. UNC paths are unchanged."""
    if not path:
        return path
    drive = app_drive() if drive is None else drive
    if not _is_letter_drive(drive):
        return path
    path_drive, rest = ntpath.splitdrive(path)
    if _is_letter_drive(path_drive) and path_drive.upper() == drive.upper() and rest[:1] in ("\\", "/"):
        return rest
    return path


def from_portable(path: str, drive: str | None = None) -> str:
    r"""``\Anime`` -> ``F:\Anime`` when the app now runs from F:."""
    if not path:
        return path
    drive = app_drive() if drive is None else drive
    if not _is_letter_drive(drive) or path.startswith(("\\\\", "//")):
        return path
    if path[:1] in ("\\", "/"):
        return drive + path
    return path


def write_json(path: Path, data: object, compact: bool = False) -> None:
    """Write via a temp file so a crash never leaves half a file behind.

    Settings are indented so they're easy to read; big data files use ``compact``.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, **({"separators": (",", ":")} if compact else {"indent": 2}))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _coerce(value: object, default: object) -> object:
    """Convert a loaded JSON value to the type of the default, or keep the default."""
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(default, int):
            return int(value)  # type: ignore[arg-type]
        if isinstance(default, float):
            return float(value)  # type: ignore[arg-type]
        if isinstance(default, str):
            return "" if value is None else str(value)
    except (TypeError, ValueError):
        pass
    return default


@dataclass
class Config:
    scan_source: str = "folder"            # "folder" or "sonarr"
    library_path: str = ""
    theme: str = "system"                  # "system", "light" or "dark"
    confidence_threshold: int = 80
    cache_expiry_days: int = 30
    anilist_min_interval: float = 2.1
    probe_workers: int = 4
    network_workers_enabled: bool = False
    network_workers: int = 6
    pause_enabled: bool = False
    pause_every_files: int = 50
    pause_seconds: int = 30
    ignore_commentary_tracks: bool = True
    sonarr_url: str = "http://localhost:8989"
    sonarr_api_key: str = ""
    sonarr_anime_only: bool = True
    sonarr_read_unknown: bool = True
    sonarr_path_from: str = ""
    sonarr_path_to: str = ""
    window_geometry: str = ""

    _PORTABLE_FIELDS = ("library_path", "sonarr_path_to")

    @classmethod
    def load(cls, path: Path) -> Config:
        """Load settings; unknown keys are ignored and the file is rewritten so new keys appear."""
        data: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError) as exc:
                log.warning("Couldn't read %s (%s) - using default settings", path, exc)
        config = cls()
        for f in fields(cls):
            if f.name in data:
                setattr(config, f.name, _coerce(data[f.name], getattr(config, f.name)))
        for name in cls._PORTABLE_FIELDS:
            setattr(config, name, from_portable(getattr(config, name)))
        config.clamp()
        try:
            config.save(path)
        except OSError as exc:
            log.warning("Couldn't save %s: %s", path, exc)
        return config

    def clamp(self) -> None:
        """Keep numbers inside sensible limits even if the file was hand-edited."""
        self.confidence_threshold = min(100, max(1, self.confidence_threshold))
        self.cache_expiry_days = min(3650, max(1, self.cache_expiry_days))
        self.anilist_min_interval = min(60.0, max(0.0, self.anilist_min_interval))
        self.probe_workers = min(32, max(1, self.probe_workers))
        self.network_workers = min(32, max(1, self.network_workers))
        self.pause_every_files = min(100_000, max(1, self.pause_every_files))
        self.pause_seconds = min(3600, max(1, self.pause_seconds))
        if self.scan_source not in ("folder", "sonarr"):
            self.scan_source = "folder"
        if self.theme not in ("system", "light", "dark"):
            self.theme = "system"

    def save(self, path: Path) -> None:
        data = asdict(self)
        for name in self._PORTABLE_FIELDS:
            data[name] = to_portable(data[name])
        write_json(path, data)


class Overrides:
    """Manual matches chosen by the user: season key -> AniList ID. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, int] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                self._data = {str(k): int(v) for k, v in raw.items()}
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                log.warning("Couldn't read %s (%s) - starting without manual matches", path, exc)

    def get(self, key: str) -> int | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, anilist_id: int) -> None:
        with self._lock:
            self._data[key] = int(anilist_id)
            write_json(self._path, self._data)

    def remove(self, key: str) -> None:
        with self._lock:
            if self._data.pop(key, None) is not None:
                write_json(self._path, self._data)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
