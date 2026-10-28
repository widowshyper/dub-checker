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
                "startDate { year month day } popularity siteUrl status averageScore")
SEARCH_QUERY = ("query ($s: String) { Page(perPage: 15) { media(search: $s, type: ANIME) { %s } } }"
                % MEDIA_FIELDS)
# voiceActors comes back null unless the character edge also selects node { id }.
DETAILS_FIELDS = (" %s nextAiringEpisode { airingAt episode } relations { edges { relationType node { %s } } } "
                  "characters(perPage: 25, sort: [ROLE, RELEVANCE]) { edges { node { id } "
                  "voiceActors(language: ENGLISH) { id } } } " % (MEDIA_FIELDS, MEDIA_FIELDS))
DETAILS_QUERY = "query ($id: Int) { Media(id: $id, type: ANIME) {%s} }" % DETAILS_FIELDS
# The latest aired episodes, newest first, for working out whether a dub may still be on its way.
AIRED_QUERY = ("query ($id: Int) { Page(perPage: 50) { airingSchedules(mediaId: $id, notYetAired: false, "
               "sort: TIME_DESC) { episode airingAt } } }")
SEQUEL_FORMATS = frozenset({"TV", "TV_SHORT", "ONA"})
AIRING_REFRESH_DAYS = 0.5  # saved details of a show that's still airing are checked again after 12 hours
# AniList counts requests, not questions, so several searches or detail lookups go in one request. Details
# are capped by AniList's query complexity limit (500): 8 fit, 12 don't.
SEARCH_BATCH, DETAILS_BATCH = 10, 8
DEFAULT_INTERVAL = 2.1  # seconds between requests until AniList says its limit (30 a minute in 2026)


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
        self.reported_limit: int | None = None  # requests a minute, from AniList's X-RateLimit-Limit

    def interval(self) -> float:
        """Seconds between requests: by the limit AniList reports, with a little margin, once it has said;
        a slower ``min_interval`` (set in config.json) is always kept."""
        if not self.reported_limit:
            return self.min_interval
        chosen = self.min_interval if self.min_interval > DEFAULT_INTERVAL else 0.0
        return max(chosen, 60 / self.reported_limit * 1.05)

    def search(self, text: str) -> list[dict]:
        data = self._query(f"search:{normalise(text)}", SEARCH_QUERY, {"s": text})
        return ((data or {}).get("Page") or {}).get("media") or []

    def details(self, media_id: int) -> dict | None:
        """Full details. Saved answers are reused for ``expiry_days``, except that a show that's still
        airing is refreshed after ``AIRING_REFRESH_DAYS``, and ones saved before air status or the score
        were asked for are asked again."""
        data = self._query(f"media:{media_id}", DETAILS_QUERY, {"id": int(media_id)}, _details_usable)
        return (data or {}).get("Media")

    def peek_search(self, text: str) -> list[dict] | None:
        """A saved search, or None if it would have to be asked for."""
        data = self._cached(f"search:{normalise(text)}")
        return None if data is MISSING else ((data or {}).get("Page") or {}).get("media") or []

    def peek_details(self, media_id: int) -> dict | None:
        """Saved details, or None if they would have to be asked for (or AniList doesn't have them)."""
        data = self._cached(f"media:{media_id}", _details_usable)
        return None if data is MISSING else (data or {}).get("Media")

    def prefetch_searches(self, texts: list[str], say: Callable[[str], None] | None = None) -> None:
        """Ask for several searches at once and save them, so ``search`` finds them saved. Ones already
        saved are skipped; if a batch fails, its searches are just asked for one by one later."""
        todo = list(dict.fromkeys(t for t in texts if t and self.peek_search(t) is None))
        for start in range(0, len(todo), SEARCH_BATCH):
            chunk = todo[start:start + SEARCH_BATCH]
            if say:
                say(f"Searching AniList for {len(todo):,} titles, {SEARCH_BATCH} at a time: "
                    f"{start + len(chunk):,} of {len(todo):,}")
            names = ", ".join(f"$s{i}: String" for i in range(len(chunk)))
            pages = " ".join(f"s{i}: Page(perPage: 15) {{ media(search: $s{i}, type: ANIME) {{ {MEDIA_FIELDS} }} }}"
                             for i in range(len(chunk)))
            data = self._batch(f"query ({names}) {{ {pages} }}", {f"s{i}": t for i, t in enumerate(chunk)})
            for i, text in enumerate(chunk):
                if (data or {}).get(f"s{i}") is not None:
                    self._save(f"search:{normalise(text)}", {"data": {"Page": data[f"s{i}"]}})

    def prefetch_details(self, media_ids: list[int], say: Callable[[str], None] | None = None) -> None:
        """Like ``prefetch_searches``, for details. A batch with an ID AniList doesn't have comes back empty,
        so those are asked for one by one later (and that answer is saved)."""
        todo = list(dict.fromkeys(int(i) for i in media_ids if i and self.peek_details(int(i)) is None))
        for start in range(0, len(todo), DETAILS_BATCH):
            chunk = todo[start:start + DETAILS_BATCH]
            if say:
                say(f"Getting the details of {len(todo):,} shows from AniList, {DETAILS_BATCH} at a time: "
                    f"{start + len(chunk):,} of {len(todo):,}")
            names = ", ".join(f"$i{k}: Int" for k in range(len(chunk)))
            media = " ".join(f"m{k}: Media(id: $i{k}, type: ANIME) {{{DETAILS_FIELDS}}}" for k in range(len(chunk)))
            data = self._batch(f"query ({names}) {{ {media} }}", {f"i{k}": i for k, i in enumerate(chunk)})
            for k, media_id in enumerate(chunk):
                if (data or {}).get(f"m{k}") is not None:
                    self._save(f"media:{media_id}", {"data": {"Media": data[f"m{k}"]}})

    def _batch(self, query: str, variables: dict) -> dict | None:
        try:
            return self._post(query, variables).get("data")
        except AniListError as exc:
            log.info("A batch of AniList lookups failed (%s); they'll be asked one by one", exc)
            return None

    def aired_episodes(self, media_id: int) -> dict[int, int]:
        """Episode number -> when it first aired (Unix time), for the latest 50 aired episodes. A saved
        answer is reused for 12 hours, since a show that's airing adds episodes."""
        data = self._query(f"aired:{media_id}", AIRED_QUERY, {"id": int(media_id)},
                           lambda _data, age_days: age_days <= AIRING_REFRESH_DAYS)
        nodes = ((data or {}).get("Page") or {}).get("airingSchedules") or []
        return {int(n["episode"]): int(n["airingAt"]) for n in nodes if n.get("episode") and n.get("airingAt")}

    def _cached(self, key: str, usable: Callable[[dict | None, float], bool] | None = None) -> Any:
        """The saved answer's data, or MISSING."""
        if self.cache is None:
            return MISSING
        cached, age_days = self.cache.get_anilist_with_age(key, self.expiry_days)
        if cached is MISSING:
            return MISSING
        data = cached.get("data") if isinstance(cached, dict) else None
        return data if usable is None or usable(data, age_days) else MISSING

    def _save(self, key: str, body: dict) -> None:
        if self.cache is not None:
            self.cache.save_anilist(key, body)

    def _query(self, key: str, query: str, variables: dict,
               usable: Callable[[dict | None, float], bool] | None = None) -> dict | None:
        data = self._cached(key, usable)
        if data is not MISSING:
            return data
        body = self._post(query, variables)
        self._save(key, body)  # includes 404 answers
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
            _GATE.wait(self.interval(), self.cancel)
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
        try:
            self.reported_limit = max(1, int(response.headers["X-RateLimit-Limit"]))
        except (KeyError, TypeError, ValueError):
            pass
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


def _details_usable(data: dict | None, age_days: float) -> bool:
    """Saved details are reused, except that a show that's still airing is refreshed after
    ``AIRING_REFRESH_DAYS``, and ones saved before air status or the score were asked for are asked again."""
    media = (data or {}).get("Media")
    if media is None or age_days <= AIRING_REFRESH_DAYS:
        return True
    return "status" in media and media["status"] not in AIRING_STATUSES and "averageScore" in media


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
        score=media.get("averageScore") or None,
    )


def short_title(title: str) -> str:
    """The part before a subtitle ('Title: Subtitle', 'Title - Subtitle'), tried when the whole title
    doesn't match well enough; '' if there's no shorter part."""
    short = re.split(r"\s*:\s*|\s+-\s+|\s*-\s*", title, maxsplit=1)[0].strip()
    return short if short != title else ""


def first_sequel(details: dict) -> int | None:
    """The ID of the earliest TV/ONA sequel in AniList's relations, if any."""
    sequels = [e["node"] for e in (details.get("relations") or {}).get("edges") or []
               if e.get("relationType") == "SEQUEL" and e.get("node")
               and e["node"].get("type", "ANIME") == "ANIME" and e["node"].get("format") in SEQUEL_FORMATS]
    if not sequels:
        return None
    sequels.sort(key=lambda m: tuple((m.get("startDate") or {}).get(k) or 9999 for k in ("year", "month", "day")))
    return int(sequels[0]["id"])


def season_candidate(title: str, season: int, results: list[dict], base_id: int,
                     threshold: float) -> tuple[float, dict] | None:
    """The best "Title Season N" search result that names that season and isn't season 1 itself."""
    def season_scorer(query: str, media: dict) -> float:
        if not any(has_season_marker(name, season) for name in media_titles(media)):
            return 0.0
        plain = max((_fuzz().ratio(normalise(title), strip_season_marker(n, season)) for n in media_titles(media)),
                    default=0.0)
        return max(plain, title_score(query, media))

    for score, media in rank(f"{title} Season {season}", None, results, scorer=season_scorer):
        if score < threshold:
            return None
        if media["id"] != base_id:
            return score, media
    return None


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
                return MatchOutcome(None, None,
                                    [f"Your manual match (AniList ID {override_id}) wasn't found on AniList"])
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

    def _best(self, title: str, year: int | None,
              search: Callable[[str], list[dict] | None] | None = None) -> tuple[float, dict] | None:
        """The best search result for a title (or the part before its subtitle). ``search`` is
        ``client.search`` unless given; one that returns None (not saved yet) gives None."""
        search = search or self.client.search
        results = search(title)
        if results is None:
            return None
        ranked = rank(title, year, results)
        best = ranked[0] if ranked else None
        if (best is None or best[0] < self.threshold) and (short := short_title(title)):
            retry_results = search(short)
            retry = rank(short, year, retry_results) if retry_results is not None else []
            if retry and (best is None or retry[0][0] > best[0]):
                best = retry[0]
        return best

    def _follow_sequels(self, media_id: int, steps: int) -> dict | None:
        current = media_id
        for _ in range(steps):
            details = self.client.details(current)
            sequel = first_sequel(details) if details else None
            if sequel is None:
                return None
            current = sequel
        return self.client.details(current)

    def prefetch(self, seasons: list[tuple[ShowGroup, int | None]], say: Callable[[str], None] | None = None) -> None:
        """Ask AniList about many seasons in a few batched requests, following the same steps as ``find``,
        so that ``find`` then mostly finds saved answers. Anything missed here is just asked for then.
        ``seasons`` are (season, manual match ID or None)."""
        client = self.client
        client.prefetch_details([override for _, override in seasons if override], say)
        auto = [group for group, override in seasons if not override]
        later = [g for g in auto if g.season is not None and g.season > 1]
        client.prefetch_searches([g.search_title for g in auto]
                                 + [f"{g.search_title} Season {g.season}" for g in later], say)
        # The part before a subtitle, where the whole title didn't match well enough.
        retries = []
        for g in auto:
            results = client.peek_search(g.search_title)
            if results is not None and (short := short_title(g.search_title)):
                ranked = rank(g.search_title, g.year, results)
                if not ranked or ranked[0][0] < self.threshold:
                    retries.append(short)
        client.prefetch_searches(retries, say)

        wanted: list[int] = []
        chains: list[tuple[int, int]] = []  # (AniList ID, sequel steps still to follow)
        for g in auto:
            best = self._best(g.search_title, g.year, client.peek_search)
            if best is None:
                continue
            base_id = int(best[1]["id"])
            wanted.append(base_id)
            if g.season is not None and g.season > 1:
                chains.append((base_id, g.season - 1))
                found = season_candidate(g.search_title, g.season, client.peek_search(
                    f"{g.search_title} Season {g.season}") or [], base_id, self.threshold)
                if found:
                    wanted.append(int(found[1]["id"]))
        client.prefetch_details(wanted, say)
        while chains:  # later seasons follow AniList's sequel list from season 1: a step at a time, together
            steps = []
            for media_id, left in chains:
                details = client.peek_details(media_id)
                sequel = first_sequel(details) if details else None
                if sequel is not None and left > 0:
                    steps.append((sequel, left - 1))
            chains = steps
            client.prefetch_details([media_id for media_id, _ in chains], say)

    def _find_later_season(self, group: ShowGroup, season: int, base_score: float, base_media: dict) -> MatchOutcome:
        title = group.search_title
        chain = self._follow_sequels(base_media["id"], season - 1)
        found = season_candidate(title, season, self.client.search(f"{title} Season {season}"), base_media["id"],
                                 self.threshold)

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
