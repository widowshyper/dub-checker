"""Sonarr API v3 client (works with Sonarr v3, v4 and v5)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class SonarrError(Exception):
    """A user-facing explanation of what went wrong talking to Sonarr."""


@dataclass
class SonarrSeries:
    id: int
    title: str
    year: int | None
    path: str
    series_type: str


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
                             path=str(s.get("path") or ""), series_type=str(s.get("seriesType") or "").lower())
                for s in self._get("/api/v3/series") or [] if "id" in s]

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


def search_for_replacements(client: SonarrClient, folder_name: str, title: str, seasons: list[int | None],
                            file_names: list[str]) -> str:
    """Ask Sonarr to search again for the episodes whose files are named in ``file_names``.

    Episodes are matched by file name, so this works after a Sonarr scan and after a
    local-files scan of a library Sonarr manages. If none match, whole seasons (or the
    series) are searched instead. Returns a message for the status line.
    """
    series = client.find_series(folder_name, title)
    if series is None:
        raise SonarrError(f"Couldn't find “{title}” in Sonarr. Sonarr needs to manage this show "
                          f"(in a folder called “{folder_name}”) before it can search for it.")
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


def connection_summary(client: SonarrClient) -> str:
    """Used by Settings > Test connection."""
    version = client.status().get("version", "?")
    series = client.series()
    anime = sum(1 for s in series if s.series_type == "anime")
    return (f"Connected to Sonarr {version}. It has {len(series)} series, "
            f"{anime} of them set to the Anime series type.")
