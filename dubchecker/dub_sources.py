"""Where dub information comes from: AniList's cast list, the MAL-Dubs list and your own files."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dubchecker.anilist import AniListClient, AniListMatcher, english_cast_listed
from dubchecker.cache import Cache
from dubchecker.config import Config
from dubchecker.models import AniListMatch, DubInfo, ShowGroup

log = logging.getLogger(__name__)

MAL_DUBS_URL = "https://raw.githubusercontent.com/MAL-Dubs/MAL-Dubs/main/data/dubInfo.json"
REFRESH_SECONDS = 7 * 86400

SOURCE_ANILIST = "AniList cast list"
SOURCE_MAL_DUBS = "MAL-Dubs list"
SOURCE_FILES = "Your files"


class MalDubs:
    """The community MAL-Dubs list: ``{"dubbed": [malIds], "incomplete": [malIds]}``.

    Saved in UserData and refreshed weekly; if the download fails the saved copy is used.
    """

    def __init__(self, path: Path, session: Any = None) -> None:
        self.path = path
        self.session = session
        self.dubbed: set[int] = set()
        self.incomplete: set[int] = set()
        self.available = False

    def _parse(self, data: Any) -> bool:
        if not isinstance(data, dict) or not isinstance(data.get("dubbed"), list):
            return False
        self.dubbed = {int(i) for i in data.get("dubbed") or [] if str(i).isdigit()}
        self.incomplete = {int(i) for i in data.get("incomplete") or [] if str(i).isdigit()}
        self.available = True
        return True

    def load(self, allow_download: bool = True) -> None:
        try:
            fresh = time.time() - self.path.stat().st_mtime < REFRESH_SECONDS
        except OSError:
            fresh = False
        if not fresh and allow_download:
            try:
                import requests
                session = self.session or requests
                response = session.get(MAL_DUBS_URL, timeout=30)
                response.raise_for_status()
                data = response.json()
                if self._parse(data):
                    tmp = self.path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(data), encoding="utf-8")
                    os.replace(tmp, self.path)
                    log.info("MAL-Dubs list updated (%d dubbed)", len(self.dubbed))
                    return
            except Exception as exc:
                log.warning("Couldn't download the MAL-Dubs list: %s", exc)
        try:
            self._parse(json.loads(self.path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as exc:
            log.warning("No usable saved MAL-Dubs list: %s", exc)

    def status(self, mal_id: int | None) -> str | None:
        """"dubbed", "incomplete", "not_listed", or None when it can't be checked."""
        if not self.available or not mal_id:
            return None
        if mal_id in self.dubbed:
            return "dubbed"
        if mal_id in self.incomplete:
            return "incomplete"
        return "not_listed"


def combine_dub_signals(anilist_va: bool | None, maldubs: str | None, local_english: bool) -> DubInfo:
    """A dub exists if any signal says so. If nothing could be checked, the answer is unknown."""
    sources = []
    checked = False
    if anilist_va is not None:
        checked = True
        if anilist_va:
            sources.append(SOURCE_ANILIST)
    if maldubs is not None:
        checked = True
        if maldubs in ("dubbed", "incomplete"):
            sources.append(SOURCE_MAL_DUBS)
    if local_english:
        checked = True
        sources.append(SOURCE_FILES)
    exists: bool | None = True if sources else (False if checked else None)
    return DubInfo(exists=exists, sources=sources, incomplete=maldubs == "incomplete")


@dataclass
class LookupResult:
    match: AniListMatch | None
    anilist_va: bool | None = None
    maldubs: str | None = None
    notes: list[str] = field(default_factory=list)


class DubLookup:
    """Finds a season on AniList and gathers the online dub signals for it."""

    def __init__(self, matcher: AniListMatcher, maldubs: MalDubs) -> None:
        self.matcher = matcher
        self.maldubs = maldubs

    def lookup(self, group: ShowGroup, override_id: int | None = None) -> LookupResult:
        outcome = self.matcher.find(group, override_id)
        if outcome.match is None:
            return LookupResult(None, notes=outcome.notes)
        notes = list(outcome.notes)
        if outcome.match.note:
            notes.insert(0, outcome.match.note)
        return LookupResult(outcome.match, english_cast_listed(outcome.details),
                            self.maldubs.status(outcome.match.mal_id), notes)


def make_online_lookup(cache: Cache, config: Config, data_dir: Path, cancel: threading.Event | None,
                       status: Callable[[str], None] | None) -> DubLookup:
    client = AniListClient(cache, config.anilist_min_interval, config.cache_expiry_days, cancel, status)
    maldubs = MalDubs(data_dir / "dubInfo.json")
    maldubs.load()
    return DubLookup(AniListMatcher(client, config.confidence_threshold), maldubs)
