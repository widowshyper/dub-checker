"""Keeping the last scan's results between runs (``UserData/last_scan.json``).

Saved when a scan finishes, when Fix match updates a season, and when the window
closes; deleted by Clear results. File paths are stored without the app's drive
letter, like the library path, so the list survives a USB stick changing letter.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from dubchecker.config import from_portable, to_portable, write_json
from dubchecker.models import (AirInfo, AniListMatch, AudioTrack, Category, DubInfo, FileResult, FileStatus, Lang,
                               Rating, ShowGroup, ShowResult, VideoFile, air_from_match)

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
_RENAMED_STATUSES = {"JAPANESE_ONLY": "ORIGINAL_ONLY"}  # saves from before Korean and Chinese support


@dataclass
class SavedScan:
    results: list[ShowResult]
    source: str = "folder"          # "folder" (local files) or "sonarr"
    finished_at: float = field(default_factory=time.time)
    stopped: bool = False
    tabs: list[str] = field(default_factory=lambda: [Category.NEEDS_DUB.name])  # the selected tabs
    expanded: list[str] = field(default_factory=list)          # shows with their seasons listed
    expanded_seasons: list[str] = field(default_factory=list)  # seasons with their episodes listed


# ---------------------------------------------------------------- to JSON

def _track_to_dict(t: AudioTrack) -> dict:
    data = {k: v for k, v in t.to_dict().items() if v != ""}  # empty fields are filled back in on load
    data["lang"] = t.lang.name
    if t.ignored:
        data["ignored"] = True
    return data


def _file_to_dict(f: FileResult) -> dict:
    data = {"path": to_portable(f.path), "size": f.size, "mtime": f.mtime, "status": f.status.name,
            "tracks": [_track_to_dict(t) for t in f.tracks]}
    for name, default in (("error", ""), ("source", "disk"), ("note", ""), ("aired_at", None)):
        if getattr(f, name) != default:
            data[name] = getattr(f, name)
    return data


def _result_to_dict(r: ShowResult) -> dict:
    g = r.group
    return {"group": {"key": g.key, "folder_name": g.folder_name, "search_title": g.search_title, "year": g.year,
                      "season": g.season},
            "files": [_file_to_dict(f) for f in r.files], "category": r.category.name,
            "match": asdict(r.match) if r.match else None, "dub": asdict(r.dub) if r.dub else None,
            "notes": list(r.notes), "looked_up": r.looked_up, "stopped": r.stopped,
            "air": asdict(r.air) if r.air else None, "rating": asdict(r.rating) if r.rating else None}


def save_scan(path: Path, scan: SavedScan) -> None:
    write_json(path, {"version": FORMAT_VERSION, "source": scan.source, "finished_at": scan.finished_at,
                      "stopped": scan.stopped, "tabs": list(scan.tabs), "expanded": sorted(scan.expanded),
                      "expanded_seasons": sorted(scan.expanded_seasons),
                      "results": [_result_to_dict(r) for r in scan.results]}, compact=True)


# ---------------------------------------------------------------- from JSON

def _known(cls: type, data: dict) -> dict:
    """Only the keys the dataclass still has, so older or newer files still load."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def _file_from_dict(d: dict) -> FileResult:
    tracks = [AudioTrack(index=int(t.get("index", n)), language=t.get("language", ""), title=t.get("title", ""),
                         codec=t.get("codec", ""), channels=t.get("channels", ""),
                         lang=Lang[t.get("lang", "UNKNOWN")], ignored=bool(t.get("ignored")))
              for n, t in enumerate(d.get("tracks") or [])]
    status = _RENAMED_STATUSES.get(d["status"], d["status"])
    return FileResult(path=from_portable(d["path"]), size=int(d.get("size") or 0), mtime=float(d.get("mtime") or 0),
                      tracks=tracks, status=FileStatus[status], error=d.get("error", ""),
                      source=d.get("source", "disk"), note=d.get("note", ""),
                      aired_at=int(d["aired_at"]) if d.get("aired_at") else None)


def _result_from_dict(d: dict) -> ShowResult:
    files = [_file_from_dict(f) for f in d["files"]]
    g = d["group"]
    group = ShowGroup(key=g["key"], folder_name=g["folder_name"], search_title=g.get("search_title", ""),
                      year=g.get("year"), season=g.get("season"),
                      files=[VideoFile(f.path, f.size, f.mtime) for f in files])
    match = AniListMatch(**_known(AniListMatch, d["match"])) if d.get("match") else None
    dub = DubInfo(**_known(DubInfo, d["dub"])) if d.get("dub") else None
    if "air" in d:
        air = AirInfo(**_known(AirInfo, d["air"])) if d["air"] else None
    else:  # saved by 2.3.0, which kept air status inside the AniList match
        air = air_from_match(match)
    rating = Rating(**_known(Rating, d["rating"])) if d.get("rating") else None
    return ShowResult(group, files, Category[d["category"]], match=match, dub=dub, notes=list(d.get("notes") or []),
                      looked_up=bool(d.get("looked_up")), stopped=bool(d.get("stopped")), air=air, rating=rating)


def load_scan(path: Path) -> SavedScan | None:
    """The saved scan, or None if there isn't one or it can't be read."""
    if not path.exists():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("version") != FORMAT_VERSION:
            log.info("Ignoring %s: it was saved by a different version", path)
            return None
        tabs = data.get("tabs")
        if not isinstance(tabs, list):  # saved before tabs could be combined: one group
            tabs = [data.get("group", Category.NEEDS_DUB.name)]
        return SavedScan(results=[_result_from_dict(r) for r in data.get("results") or []],
                         source=data.get("source", "folder"), finished_at=float(data.get("finished_at") or 0),
                         stopped=bool(data.get("stopped")),
                         tabs=[str(tab) for tab in tabs],
                         expanded=[str(e) for e in data.get("expanded") or []],
                         expanded_seasons=[str(e) for e in data.get("expanded_seasons") or []])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("Couldn't read the saved results in %s: %s", path, exc)
        return None


def delete_scan(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Couldn't delete %s: %s", path, exc)


_NOTHING = object()


class ResultsSaver:
    """Saves (or deletes) the results file on a thread of its own: for a big library writing it takes a
    noticeable moment, which shouldn't pause the window. Only the newest request matters, so requests made
    while one is being written replace each other. ``flush`` waits until everything asked for is done."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cond = threading.Condition()
        self._pending: object = _NOTHING  # a SavedScan to save, None to delete the file
        self._busy = False
        self._thread: threading.Thread | None = None

    def save(self, scan: SavedScan | None) -> None:
        """Save ``scan``, or delete the file for None."""
        with self._cond:
            self._pending = scan
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="results-saver", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def flush(self, timeout: float = 30.0) -> bool:
        """Wait for the newest request to be written. False if it took longer than ``timeout``."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending is _NOTHING and not self._busy, timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._cond.wait_for(lambda: self._pending is not _NOTHING, timeout=60):
                    self._thread = None  # nothing to do for a minute: let the thread end
                    return
                job, self._pending, self._busy = self._pending, _NOTHING, True
            try:
                if job is None:
                    delete_scan(self.path)
                else:
                    save_scan(self.path, job)  # type: ignore[arg-type]
            except Exception as exc:  # e.g. the disk is full; the results stay on screen
                log.warning("Couldn't save the results: %s", exc)
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()
