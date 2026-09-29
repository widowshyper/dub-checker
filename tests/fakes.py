"""Test doubles shared by the pipeline, Sonarr and GUI tests."""
from __future__ import annotations

import re
import threading
from pathlib import Path

from dubchecker.cache import Cache
from dubchecker.config import Config, Overrides
from dubchecker.dub_sources import LookupResult
from dubchecker.models import AniListMatch, ShowGroup
from dubchecker.pipeline import ScanPipeline

LANGUAGES = {"dual": ["jpn", "eng"], "jpn": ["jpn"], "eng": ["eng"], "und": ["und"]}


class FakeProber:
    """Tracks come from a marker in the file name: 'ep1 [dual].mkv', '[jpn]', '[eng]', '[und]', '[broken]'."""

    def __init__(self, log: list, default: str = "jpn", on_read=None) -> None:
        self.log = log
        self.default = default
        self.on_read = on_read
        self.lock = threading.Lock()
        self.reads = 0

    def probe(self, path: str) -> list[dict]:
        with self.lock:
            self.reads += 1
            self.log.append(("read", Path(path).name))
            count = self.reads
        if self.on_read:
            self.on_read(count)
        match = re.search(r"\[(dual|jpn|eng|und|broken)\]", path)
        marker = match.group(1) if match else self.default
        if marker == "broken":
            raise ValueError("this file is corrupt")
        return [{"index": i, "language": lang, "title": "", "codec": "PCM", "channels": "2"}
                for i, lang in enumerate(LANGUAGES[marker])]


class FakeLookup:
    """Every season matches with 100 % confidence; ``dub`` sets what the AniList cast list says."""

    def __init__(self, log: list, dub: bool | None = True, on_lookup=None, air_status: str = "") -> None:
        self.log = log
        self.dub = dub
        self.on_lookup = on_lookup
        self.air_status = air_status

    def lookup(self, group: ShowGroup, override_id: int | None = None) -> LookupResult:
        self.log.append(("lookup", group.key))
        if self.on_lookup:
            self.on_lookup(group)
        match = AniListMatch(override_id or 1, f"{group.show_title} (AniList)", confidence=100,
                             manual=bool(override_id), air_status=self.air_status)
        return LookupResult(match, self.dub, None)


def make_pipeline(data: Path, cache: Cache, prober, lookup, config: Config | None = None,
                  cancel: threading.Event | None = None, overrides: Overrides | None = None):
    events: list[tuple] = []

    def emit(kind: str, *payload) -> None:
        events.append((kind, *payload))

    pipeline = ScanPipeline(config or Config(probe_workers=2, air_status_source="off"), cache, overrides or Overrides(data / "overrides.json"),
                            prober, lambda: lookup, emit, cancel or threading.Event())
    return pipeline, events


def touch(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path
