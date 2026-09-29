"""Sonarr API v3 client (works with Sonarr v3, v4 and v5)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from dubchecker.models import AirInfo


class SonarrError(Exception):
    """A user-facing explanation of what went wrong talking to Sonarr."""


@dataclass
class SonarrSeries:
    id: int
    title: str
    year: int | None
    path: str
    series_type: str
    status: str = ""                                   # continuing, ended, upcoming or deleted
    seasons: list = field(default_factory=list)        # Sonarr's season list, with per-season statistics
    title_slug: str = ""                               # the series' page in Sonarr: <address>/series/<slug>


def normalise_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    return url


def last_path_part(path: str) -> str:
    """The last part of a path (a series folder or a file name), for either slash style."""
    return re.split(r"[\\/]", (path or "").rstrip("\\/"))[-1]


def map_path(path: str, sonarr_prefix: str, local_prefix: str) -> str:
    r"""Translate a path as Sonarr sees it into one this PC can open, e.g. ``/tv`` -> ``\\nas\media\tv``."""
    if not path or not sonarr_prefix or not local_prefix:
        return path
    source = sonarr_prefix.replace("\\", "/").rstrip("/")
    unified = path.replace("\\", "/")
    if not (unified.casefold() == source.casefold() or unified.casefold().startswith(source.casefold() + "/")):
        return path
    rest = unified[len(source):].lstrip("/")
    windows_style = "\\" in local_prefix or bool(re.match(r"^[A-Za-z]:", local_prefix))
    sep = "\\" if windows_style else "/"
    base = local_prefix.rstrip("\\/")
    return base + (sep + rest.replace("/", sep) if rest else "")


def tracks_from_media_info(media_info: dict | None) -> list[dict] | None:
    """Tracks from Sonarr's ``mediaInfo``; None when Sonarr hasn't analysed the file.

    ``audioLanguages`` is a ``/``-joined list such as ``jpn/eng`` (full names in v3),
    padded with unknown tracks up to ``audioStreamCount``.
    """
    if not media_info:
        return None
    raw = media_info.get("audioLanguages") or ""
    languages = [part.strip() for part in re.split(r"\s*/\s*", str(raw)) if part.strip()]
    try:
        count = max(int(media_info.get("audioStreamCount") or 0), len(languages))
    except (TypeError, ValueError):
        count = len(languages)
    languages += [""] * (count - len(languages))
    codec = str(media_info.get("audioCodec") or "")
    channels = media_info.get("audioChannels")
    return [{"index": i, "language": lang, "title": "",
             "codec": codec if i == 0 else "", "channels": str(channels) if i == 0 and channels else ""}
            for i, lang in enumerate(languages)]


class SonarrClient:
    def __init__(self, url: str, api_key: str, timeout: float = 30, session: Any = None) -> None:
        import requests
        self.url = normalise_url(url)
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = session or requests.Session()

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        import requests
        if not self.url:
            raise SonarrError("Enter Sonarr's address first, for example http://localhost:8989")
        if not self.api_key:
            raise SonarrError("Enter Sonarr's API key first. You'll find it in Sonarr under Settings > General.")
        try:
            response = self.session.request(method, f"{self.url}{path}", params=params, json=body,
                                            timeout=self.timeout,
                                            headers={"X-Api-Key": self.api_key, "Accept": "application/json"})
        except requests.Timeout as exc:
            raise SonarrError(f"Sonarr at {self.url} took too long to answer. Is it busy or asleep?") from exc
        except (requests.ConnectionError, requests.exceptions.InvalidURL, requests.exceptions.MissingSchema) as exc:
            raise SonarrError(f"Couldn't connect to Sonarr at {self.url}. Check that Sonarr is running "
                              "and that the address and port are right.") from exc
        if response.status_code == 401:
            raise SonarrError("Sonarr rejected the API key. Copy it again from Sonarr > Settings > General > "
                              "API Key.")
        if response.status_code == 404:
            raise SonarrError(f"Sonarr answered, but that page wasn't found at {self.url}. If you set a URL base "
                              "in Sonarr (Settings > General), add it to the address, "
                              "e.g. http://localhost:8989/sonarr")
        if response.status_code >= 400:
            raise SonarrError(f"Sonarr answered with an error (HTTP {response.status_code}).")
        try:
            return response.json()
        except ValueError as exc:
            raise SonarrError(f"Something answered at {self.url}, but it doesn't look like Sonarr.") from exc

    def status(self) -> dict:
        data = self._get("/api/v3/system/status")
        if not isinstance(data, dict):
            raise SonarrError(f"Something answered at {self.url}, but it doesn't look like Sonarr.")
        return data

    def series(self) -> list[SonarrSeries]:
        return [SonarrSeries(id=int(s["id"]), title=str(s.get("title") or ""), year=s.get("year") or None,
                             path=str(s.get("path") or ""), series_type=str(s.get("seriesType") or "").lower(),
                             status=str(s.get("status") or "").lower(), seasons=list(s.get("seasons") or []),
                             title_slug=str(s.get("titleSlug") or ""))
                for s in self._get("/api/v3/series") or [] if "id" in s]

    def calendar(self, start: str, end: str) -> list[dict]:
        """Episodes airing between two ISO dates, including unmonitored ones."""
        return list(self._get("/api/v3/calendar", {"start": start, "end": end, "unmonitored": "true"}) or [])

    def episode_files(self, series_id: int) -> list[dict]:
        return list(self._get("/api/v3/episodefile", {"seriesId": series_id}) or [])

    def episodes(self, series_id: int) -> list[dict]:
        return list(self._get("/api/v3/episode", {"seriesId": series_id}) or [])

    def command(self, name: str, **options: Any) -> dict:
        """Start a Sonarr command such as EpisodeSearch; Sonarr runs it in the background."""
        return self._request("POST", "/api/v3/command", body={"name": name, **options}) or {}

    def find_series(self, folder_name: str, title: str = "") -> SonarrSeries | None:
        """The series whose folder has this name (as used by both scan types), else one with the same title."""
        series = self.series()
        for s in series:
            if last_path_part(s.path).casefold() == folder_name.casefold():
                return s
        wanted = _plain_title(title or folder_name)
        return next((s for s in series if _plain_title(s.title) == wanted), None)


def _plain_title(title: str) -> str:
    title = re.sub(r"[\[{(][^\]})]*[\]})]", " ", title)  # (2013), {tvdb-1}, [imdbid-...]
    return " ".join(re.sub(r"[\W_]+", " ", title.casefold()).split())


def _find_or_explain(client: SonarrClient, folder_name: str, title: str) -> SonarrSeries:
    series = client.find_series(folder_name, title)
    if series is None:
        raise SonarrError(f"Couldn't find “{title}” in Sonarr. Sonarr needs to manage this show "
                          f"(in a folder called “{folder_name}”) before it can be opened or searched for there.")
    return series


def series_page_url(client: SonarrClient, folder_name: str, title: str) -> str:
    """The address of the show's page in Sonarr's web interface, found like the replacement search:
    by folder name, then by title."""
    series = _find_or_explain(client, folder_name, title)
    if not series.title_slug:
        raise SonarrError(f"Sonarr didn't say where the page for “{series.title}” is.")
    return f"{client.url}/series/{quote(series.title_slug)}"


def search_for_replacements(client: SonarrClient, folder_name: str, title: str, seasons: list[int | None],
                            file_names: list[str]) -> str:
    """Ask Sonarr to search again for the episodes whose files are named in ``file_names``.

    Episodes are matched by file name, so this works after a Sonarr scan and after a
    local-files scan of a library Sonarr manages. If none match, whole seasons (or the
    series) are searched instead. Returns a message for the status line.
    """
    series = _find_or_explain(client, folder_name, title)
    wanted = {name.casefold() for name in file_names}
    file_ids = {ef["id"] for ef in client.episode_files(series.id)
                if "id" in ef and last_path_part(str(ef.get("path") or "")).casefold() in wanted}
    episode_ids = sorted(e["id"] for e in client.episodes(series.id)
                         if file_ids and e.get("episodeFileId") in file_ids)
    if episode_ids:
        client.command("EpisodeSearch", episodeIds=episode_ids)
        count = f"{len(episode_ids)} episode" + ("" if len(episode_ids) == 1 else "s")
        return f"Sonarr is searching for new releases of {count} of {series.title}."
    numbered = sorted({s for s in seasons if s is not None})
    if numbered:
        for season in numbered:
            client.command("SeasonSearch", seriesId=series.id, seasonNumber=season)
        names = ", ".join("Specials" if s == 0 else f"Season {s}" for s in numbered)
        return f"Sonarr is searching for new releases of {series.title} ({names})."
    client.command("SeriesSearch", seriesId=series.id)
    return f"Sonarr is searching for new releases of {series.title}."


def _timestamp(text: object) -> int | None:
    """Sonarr's '2026-10-03T15:30:00Z' as Unix time."""
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


class SonarrAirStatus:
    """Air status from Sonarr (Settings > General > Air status > Sonarr): each series' status plus the
    calendar's upcoming episodes. Two requests in total, whatever the size of the library."""

    CALENDAR_DAYS = 120

    def __init__(self, series: list[SonarrSeries], upcoming: list[dict]) -> None:
        self.by_folder = {last_path_part(s.path).casefold(): s for s in series}
        self.by_title = {_plain_title(s.title): s for s in series}
        self.next: dict[tuple[int, int], tuple[int | None, int]] = {}  # (series, season) -> (episode, airs at)
        for episode in upcoming:
            when = _timestamp(episode.get("airDateUtc"))
            if when is None or episode.get("seriesId") is None:
                continue
            key = (int(episode["seriesId"]), int(episode.get("seasonNumber") or 0))
            if key not in self.next or when < self.next[key][1]:
                self.next[key] = (episode.get("episodeNumber"), when)

    @classmethod
    def fetch(cls, client: SonarrClient, now: float | None = None) -> SonarrAirStatus:
        start = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
        end = start + timedelta(days=cls.CALENDAR_DAYS)
        iso = "%Y-%m-%dT%H:%M:%SZ"
        return cls(client.series(), client.calendar(start.strftime(iso), end.strftime(iso)))

    def find(self, folder_name: str, title: str) -> SonarrSeries | None:
        """Matched the same way as the replacement search: by folder name, then by title."""
        return self.by_folder.get(folder_name.casefold()) or self.by_title.get(_plain_title(title))

    def air(self, folder_name: str, title: str, season: int | None) -> AirInfo | None:
        series = self.find(folder_name, title)
        if series is None:
            return None
        if season is None:  # no season information: go by the whole series
            upcoming = [value for (sid, _), value in self.next.items() if sid == series.id]
            coming = min(upcoming, key=lambda value: value[1]) if upcoming else None
            stats = None
        else:
            coming = self.next.get((series.id, season))
            stats = next((s.get("statistics") or {} for s in series.seasons if s.get("seasonNumber") == season), None)
            if coming is None and stats and _timestamp(stats.get("nextAiring")):
                coming = (None, _timestamp(stats.get("nextAiring")))  # further ahead than the calendar looks
        started = bool(stats.get("previousAiring")) if stats is not None else True
        if series.status == "upcoming" or (coming and not started):
            status = "NOT_YET_RELEASED"
        elif coming:
            status = "RELEASING"
        elif series.status == "continuing" and season is None:
            status = "HIATUS"  # the show goes on, but nothing is scheduled yet
        else:
            status = "FINISHED"  # this season is done (or the whole series has ended)
        return AirInfo(status, coming[0] if coming else None, coming[1] if coming else None, "Sonarr")


def connection_summary(client: SonarrClient) -> str:
    """Used by Settings > Test connection."""
    version = client.status().get("version", "?")
    series = client.series()
    anime = sum(1 for s in series if s.series_type == "anime")
    return (f"Connected to Sonarr {version}. It has {len(series)} series, "
            f"{anime} of them set to the Anime series type.")
