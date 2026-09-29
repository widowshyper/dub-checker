"""AniList: GraphQL client with polite rate limiting, fuzzy matching and season logic."""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable


from dubchecker.cache import MISSING, Cache
from dubchecker.models import AIRING_STATUSES, AniListMatch, Cancelled, ShowGroup

log = logging.getLogger(__name__)

API_URL = "https://graphql.anilist.co"
MEDIA_FIELDS = ("id idMal type title { romaji english native } synonyms format episodes seasonYear "
                "startDate { year month day } popularity siteUrl status")
SEARCH_QUERY = ("query ($s: String) { Page(perPage: 15) { media(search: $s, type: ANIME) { %s } } }"
                % MEDIA_FIELDS)
# voiceActors comes back null unless the character edge also selects node { id }.
DETAILS_QUERY = ("query ($id: Int) { Media(id: $id, type: ANIME) { %s nextAiringEpisode { airingAt episode } "
                 "relations { edges { relationType node { %s } } } "
                 "characters(perPage: 25, sort: [ROLE, RELEVANCE]) { edges { node { id } "
                 "voiceActors(language: ENGLISH) { id } } } } }" % (MEDIA_FIELDS, MEDIA_FIELDS))
SEQUEL_FORMATS = frozenset({"TV", "TV_SHORT", "ONA"})
AIRING_REFRESH_DAYS = 0.5  # saved details of a show that's still airing are checked again after 12 hours


class AniListError(Exception):
    """AniList couldn't be reached or answered with an error. The message is user-facing."""


def wait_or_cancel(seconds: float, cancel: threading.Event | None) -> None:
    if seconds <= 0:
        return
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise Cancelled()


class _RateGate:
    """Spaces out requests across every client in the process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, interval: float, cancel: threading.Event | None) -> None:
        with self._lock:
            wait_or_cancel(self._next - time.monotonic(), cancel)
            self._next = time.monotonic() + interval

    def hold(self, seconds: float) -> None:
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


_GATE = _RateGate()


class AniListClient:
    def __init__(self, cache: Cache | None, min_interval: float = 2.1, expiry_days: float = 30,
                 cancel: threading.Event | None = None, status: Callable[[str], None] | None = None,
                 session: Any = None) -> None:
        import requests
        self.cache = cache
        self.min_interval = min_interval
        self.expiry_days = expiry_days
        self.cancel = cancel
        self.status = status
        self.session = session or requests.Session()
        self.offline = False
        self.requests_made = 0

    def search(self, text: str) -> list[dict]:
        data = self._query(f"search:{normalise(text)}", SEARCH_QUERY, {"s": text})
        return ((data or {}).get("Page") or {}).get("media") or []

    def details(self, media_id: int) -> dict | None:
        """Full details. Saved answers are reused for ``expiry_days``, except that a show that's still
        airing (or was saved before air status was asked for) is refreshed after ``AIRING_REFRESH_DAYS``."""
        def usable(data: dict | None, age_days: float) -> bool:
            media = (data or {}).get("Media")
            if media is None or age_days <= AIRING_REFRESH_DAYS:
                return True
            return "status" in media and media["status"] not in AIRING_STATUSES

        data = self._query(f"media:{media_id}", DETAILS_QUERY, {"id": int(media_id)}, usable)
        return (data or {}).get("Media")

    def _query(self, key: str, query: str, variables: dict,
               usable: Callable[[dict | None, float], bool] | None = None) -> dict | None:
        if self.cache is not None:
            cached, age_days = self.cache.get_anilist_with_age(key, self.expiry_days)
            if cached is not MISSING:
                data = cached.get("data") if isinstance(cached, dict) else None
                if usable is None or usable(data, age_days):
                    return data
        body = self._post(query, variables)
        if self.cache is not None:
            self.cache.save_anilist(key, body)  # includes 404 answers
        return body.get("data")

    def _say(self, text: str) -> None:
        if self.status:
            self.status(text)

    def _post(self, query: str, variables: dict) -> dict:
        import requests
        if self.offline:
            raise AniListError("Couldn't reach AniList - check your internet connection.")
        connection_failures = 0
        for attempt in range(6):
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled()
            _GATE.wait(self.min_interval, self.cancel)
            try:
                response = self.session.post(API_URL, json={"query": query, "variables": variables}, timeout=30,
                                             headers={"Accept": "application/json"})
                self.requests_made += 1
            except (requests.ConnectionError, requests.Timeout) as exc:
                connection_failures += 1
                log.warning("AniList request failed: %s", exc)
                if connection_failures >= 3:
                    self.offline = True
                    raise AniListError("Couldn't reach AniList - check your internet connection.") from exc
                wait_or_cancel(2 ** attempt, self.cancel)
                continue

            self._respect_rate_headers(response)
            if response.status_code == 429:
                self._slow_down(response.headers.get("Retry-After"))
                continue
            if response.status_code >= 500:
                log.warning("AniList server error %s", response.status_code)
                wait_or_cancel(min(60, 2 ** (attempt + 1)), self.cancel)
                continue
            if response.status_code == 404:
                try:
                    return response.json()
                except ValueError:
                    return {"data": None}
            if response.status_code != 200:
                raise AniListError(f"AniList answered with an error (HTTP {response.status_code}).")
            try:
                return response.json()
            except ValueError as exc:
                raise AniListError("AniList sent an answer Dub Checker couldn't understand.") from exc
        raise AniListError("AniList isn't answering right now - try again later.")

    def _respect_rate_headers(self, response: Any) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        try:
            if remaining is not None and int(remaining) <= 1 and reset:
                _GATE.hold(min(65.0, max(0.0, float(reset) - time.time())))
        except ValueError:
            pass

    def _slow_down(self, retry_after: str | None) -> None:
        try:
            seconds = max(1, int(float(retry_after or 60)))
        except ValueError:
            seconds = 60
        log.info("AniList rate limit hit; waiting %s s", seconds)
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            self._say(f"AniList is busy and asked Dub Checker to wait - carrying on in {int(left + 0.99)} s...")
            wait_or_cancel(min(1.0, left), self.cancel)


# ---------------------------------------------------------------- matching helpers

def _fuzz() -> Any:
    """rapidfuzz, imported the first time it's needed so it doesn't slow down starting the app."""
    from rapidfuzz import fuzz
    return fuzz


def normalise(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("&", " and ").replace("×", " x ")
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def media_titles(media: dict) -> list[str]:
    title = media.get("title") or {}
    names = [title.get("romaji"), title.get("english"), title.get("native")] + list(media.get("synonyms") or [])
    return [n for n in names if n]


def media_year(media: dict) -> int | None:
    return media.get("seasonYear") or (media.get("startDate") or {}).get("year")


def title_score(query: str, media: dict) -> float:
    q = normalise(query)
    best = 0.0
    for name in media_titles(media):
        n = normalise(name)
        if n:
            best = max(best, _fuzz().ratio(q, n), _fuzz().token_sort_ratio(q, n))
    return best


def year_adjust(score: float, want_year: int | None, media: dict) -> float:
    have = media_year(media)
    if want_year is None or have is None:
        return score
    if have == want_year:
        return min(100.0, score + 8)
    if abs(have - want_year) == 1:
        return min(100.0, score + 3)
    return score - 15


def rank(query: str, year: int | None, results: list[dict],
         scorer: Callable[[str, dict], float] = title_score) -> list[tuple[float, dict]]:
    scored = [(year_adjust(scorer(query, m), year, m), m) for m in results]
    scored.sort(key=lambda item: (item[0], item[1].get("popularity") or 0), reverse=True)
    return scored


_ROMAN = {1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v", 6: "vi", 7: "vii", 8: "viii", 9: "ix", 10: "x"}


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _season_marker_patterns(n: int) -> list[str]:
    patterns = [rf"\bseason {n}\b", rf"\b{ordinal(n)} season\b", rf"(?<= ){n}\b"]
    if n in _ROMAN:
        patterns.append(rf"\b{_ROMAN[n]}\b")
    return patterns


def has_season_marker(title: str, n: int) -> bool:
    """'Season 2', '2nd Season', 'Title 2' or 'Title II' for n=2."""
    text = normalise(title)
    return any(re.search(p, text) for p in _season_marker_patterns(n))


def strip_season_marker(title: str, n: int) -> str:
    text = normalise(title)
    for pattern in _season_marker_patterns(n):
        text = re.sub(pattern, " ", text)
    return " ".join(text.split())


def parse_anilist_id(text: str) -> int | None:
    """Accepts ``16498`` or ``https://anilist.co/anime/16498/Shingeki-no-Kyojin/``."""
    text = (text or "").strip()
    if re.fullmatch(r"\d{1,9}", text):
        return int(text) or None
    match = re.search(r"anilist\.co/anime/(\d{1,9})", text, re.I)
    return int(match.group(1)) if match else None


def to_match(media: dict, confidence: float, manual: bool = False, note: str = "") -> AniListMatch:
    """``media`` is best the full details, which also say when the next episode airs."""
    title = media.get("title") or {}
    upcoming = media.get("nextAiringEpisode") or {}
    return AniListMatch(
        anilist_id=int(media["id"]),
        title=title.get("english") or title.get("romaji") or title.get("native") or f"AniList #{media['id']}",
        romaji=title.get("romaji") or "",
        english=title.get("english") or "",
        mal_id=media.get("idMal"),
        year=media_year(media),
        format=media.get("format") or "",
        episodes=media.get("episodes"),
        confidence=round(confidence, 1),
        manual=manual,
        note=note,
        air_status=media.get("status") or "",
        next_episode=upcoming.get("episode"),
        next_airing_at=upcoming.get("airingAt"),
    )


def english_cast_listed(details: dict | None) -> bool | None:
    """True if any character has an English voice actor; None if the cast list is empty."""
    edges = ((details or {}).get("characters") or {}).get("edges") or []
    if not edges:
        return None
    return any(edge.get("voiceActors") for edge in edges)


# ---------------------------------------------------------------- matcher

@dataclass
class MatchOutcome:
    match: AniListMatch | None
    details: dict | None = None
    notes: list[str] = field(default_factory=list)


class AniListMatcher:
    def __init__(self, client: AniListClient, threshold: float = 80) -> None:
        self.client = client
        self.threshold = threshold

    def find(self, group: ShowGroup, override_id: int | None = None) -> MatchOutcome:
        if override_id:
            details = self.client.details(override_id)
            if not details:
                return MatchOutcome(None, None, [f"Your manual match (AniList ID {override_id}) wasn't found on AniList"])
            return MatchOutcome(to_match(details, 100, manual=True, note="You chose this match"), details)

        base = self._best(group.search_title, group.year)
        season = group.season
        if base is None:
            return MatchOutcome(None, None, ["Couldn't find this show on AniList"])
        base_score, base_media = base

        if season == 0:
            capped = min(base_score, self.threshold - 1)
            note = "Specials and OVAs are hard to match automatically - please check this one"
            details = self.client.details(base_media["id"])
            return MatchOutcome(to_match(details or base_media, capped, note=note), details)
        if season is None or season == 1:
            details = self.client.details(base_media["id"])
            return MatchOutcome(to_match(details or base_media, base_score), details)
        return self._find_later_season(group, season, base_score, base_media)

    def _best(self, title: str, year: int | None) -> tuple[float, dict] | None:
        ranked = rank(title, year, self.client.search(title))
        best = ranked[0] if ranked else None
        if best is None or best[0] < self.threshold:
            short = re.split(r"\s*:\s*|\s+-\s+|\s*-\s*", title, maxsplit=1)[0].strip()
            if short and short != title:
                retry = rank(short, year, self.client.search(short))
                if retry and (best is None or retry[0][0] > best[0]):
                    best = retry[0]
        return best

    def _follow_sequels(self, media_id: int, steps: int) -> dict | None:
        current = media_id
        details = None
        for _ in range(steps):
            details = self.client.details(current)
            if not details:
                return None
            sequels = [e["node"] for e in (details.get("relations") or {}).get("edges") or []
                       if e.get("relationType") == "SEQUEL" and e.get("node")
                       and e["node"].get("type", "ANIME") == "ANIME" and e["node"].get("format") in SEQUEL_FORMATS]
            if not sequels:
                return None
            sequels.sort(key=lambda m: tuple((m.get("startDate") or {}).get(k) or 9999 for k in ("year", "month", "day")))
            current = sequels[0]["id"]
        return self.client.details(current)

    def _find_later_season(self, group: ShowGroup, season: int, base_score: float, base_media: dict) -> MatchOutcome:
        title = group.search_title
        chain = self._follow_sequels(base_media["id"], season - 1)

        def season_scorer(query: str, media: dict) -> float:
            if not any(has_season_marker(name, season) for name in media_titles(media)):
                return 0.0
            plain = max((_fuzz().ratio(normalise(title), strip_season_marker(n, season)) for n in media_titles(media)),
                        default=0.0)
            return max(plain, title_score(query, media))

        found = None
        query = f"{title} Season {season}"
        for score, media in rank(query, None, self.client.search(query), scorer=season_scorer):
            if score < self.threshold:
                break
            if media["id"] != base_media["id"]:
                found = (score, media)
                break

        label = group.season_label
        if chain and found and chain["id"] == found[1]["id"]:
            return MatchOutcome(to_match(chain, max(base_score, found[0])), chain)
        if found:
            details = self.client.details(found[1]["id"])
            notes = []
            if chain:
                notes.append(f"AniList's sequel list pointed to a different entry for {label} - worth a quick check")
            return MatchOutcome(to_match(details or found[1], found[0]), details, notes)
        if chain:
            note = f"Found {label} by following AniList's sequel list from season 1"
            return MatchOutcome(to_match(chain, base_score * 0.95, note=note), chain)
        return MatchOutcome(None, None, [f"Couldn't find {label} on AniList"])
