"""The two-phase scan.

Phase 1 reads the audio tracks of every file (cache first, throttled, in parallel).
Phase 2 groups files into seasons and asks AniList only about seasons that have
no dual-audio file (original language + English); every other season is decided from local evidence.
The original language is Japanese, Korean or Chinese.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Protocol

from dubchecker.anilist import AniListError
from dubchecker.cache import Cache, FileCacheWriter
from dubchecker.categorize import categorize_local, categorize_lookup, needs_lookup, stopped_result
from dubchecker.config import Config, Overrides, to_portable
from dubchecker.dub_sources import LookupResult
from dubchecker.media_probe import ProbeUnavailable, build_file_result
from dubchecker.models import (Cancelled, Category, FileResult, FileStatus, ShowGroup, ShowResult, VideoFile, season_key,
                               air_from_match, season_sort_key, strip_id_tags)
from dubchecker.scanner import FolderLister, group_show, is_network_path, list_library, natural_key
from dubchecker.sonarr import (SonarrAirStatus, SonarrClient, SonarrError, last_path_part, map_path,
                              tracks_from_media_info)

log = logging.getLogger(__name__)

Emit = Callable[..., None]  # emit(kind, *payload); kinds: progress, results, result, done


class Prober(Protocol):
    def probe(self, path: str) -> list[dict]: ...


class LookupService(Protocol):
    def lookup(self, group: ShowGroup, override_id: int | None = None) -> LookupResult: ...


# ---------------------------------------------------------------- statistics

@dataclass
class ScanStats:
    files: int = 0
    cached: int = 0
    read: int = 0
    failed: int = 0
    from_sonarr: int = 0
    folders_listed: int = 0
    list_time: float = 0.0
    read_time: float = 0.0
    lookups: int = 0
    lookup_time: float = 0.0
    workers: int = 0
    network: bool = False

    def short_text(self) -> str:
        parts = []
        if self.from_sonarr:
            parts.append(f"{self.from_sonarr:,} from Sonarr")
        parts.append(f"{self.read:,} read from disk")
        if self.cached:
            parts.append(f"{self.cached:,} already known")
        if self.failed:
            parts.append(f"{self.failed:,} couldn't be read")
        return "(" + ", ".join(parts) + ")"

    def log_line(self, elapsed: float) -> str:
        rate = self.read / self.read_time if self.read_time > 0 else 0.0
        return (f"Scan summary: {self.files} files ({self.cached} cached, {self.read} read, {self.failed} failed, "
                f"{self.from_sonarr} from Sonarr); {self.folders_listed} folders listed in {self.list_time:.2f} s; "
                f"reading took {self.read_time:.2f} s ({rate:.1f} files/s, {self.workers} workers, "
                f"network share: {'yes' if self.network else 'no'}); {self.lookups} AniList lookups in "
                f"{self.lookup_time:.1f} s; total {elapsed:.1f} s")


# ---------------------------------------------------------------- hard drive care

class ReadThrottle:
    """"Read N files, then pause S seconds", shared by every reader thread for the whole scan.

    N reads may start; once all of them have finished, the next reader waits out
    the break before another N may start. Cached files never come through here.
    """

    def __init__(self, enabled: bool, every: int, seconds: float, cancel: threading.Event,
                 on_wait: Callable[[float], None] | None = None) -> None:
        self.enabled = enabled and every > 0 and seconds > 0
        self.every = max(1, every)
        self.seconds = seconds
        self.cancel = cancel
        self.on_wait = on_wait
        self.pauses = 0
        self._cond = threading.Condition()
        self._started = 0
        self._active = 0
        self._pausing = False

    def acquire(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        if not self.enabled:
            return
        while True:
            with self._cond:
                while True:
                    if self.cancel.is_set():
                        raise Cancelled()
                    if not self._pausing and self._started < self.every:
                        self._started += 1
                        self._active += 1
                        return
                    if not self._pausing and self._active == 0:
                        self._pausing = True  # this thread takes the break for everyone
                        break
                    self._cond.wait(0.25)
            try:
                self._take_break()
            finally:
                with self._cond:
                    self._pausing = False
                    self._started = 0
                    self.pauses += 1
                    self._cond.notify_all()

    def release(self) -> None:
        if not self.enabled:
            return
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def _take_break(self) -> None:
        deadline = time.monotonic() + self.seconds
        while (left := deadline - time.monotonic()) > 0:
            if self.on_wait:
                self.on_wait(left)
            if self.cancel.wait(min(1.0, left)):
                raise Cancelled()


# ---------------------------------------------------------------- scan

@dataclass
class ReadJob:
    file: VideoFile
    show_index: int
    show_title: str


@dataclass
class SeasonFiles:
    group: ShowGroup
    files: list[FileResult]
    complete: bool = True


@dataclass
class ScanOutcome:
    results: list[ShowResult] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)
    stopped: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def show_count(self) -> int:
        return len({r.group.folder_name for r in self.results})


def evaluate_season(group: ShowGroup, files: list[FileResult], service: LookupService,
                    override_id: int | None, threshold: float, air_source: str = "anilist") -> ShowResult:
    """Look one season up and categorise it. Used by the scan and by Fix match.

    With air status from AniList, the season also gets the air status AniList gave with the match.
    """
    result = _evaluate_season(group, files, service, override_id, threshold)
    if air_source == "anilist":
        result.air = air_from_match(result.match)
    return result


def _evaluate_season(group: ShowGroup, files: list[FileResult], service: LookupService,
                     override_id: int | None, threshold: float) -> ShowResult:
    try:
        lookup = service.lookup(group, override_id)
    except Cancelled:
        raise
    except Exception as exc:
        log.warning("Lookup failed for %s: %s", group.display_title, exc)
        message = str(exc) if isinstance(exc, AniListError) else f"Couldn't check AniList: {exc}"
        lookup = LookupResult(None, notes=[message])
    if needs_lookup(files):
        return categorize_lookup(group, files, lookup, threshold)
    # Has dual audio but a manual match: keep the local verdict, show the chosen entry.
    return categorize_local(group, files, match=lookup.match, extra_notes=[] if lookup.match else lookup.notes)


class ScanPipeline:
    def __init__(self, config: Config, cache: Cache, overrides: Overrides, prober: Prober,
                 lookup_factory: Callable[[], LookupService], emit: Emit, cancel: threading.Event) -> None:
        self.config = config
        self.cache = cache
        self.overrides = overrides
        self.prober = prober
        self.lookup_factory = lookup_factory
        self.emit = emit
        self.cancel = cancel
        self.stats = ScanStats()
        self.stopped = False
        self.warnings: list[str] = []
        self._stats_lock = threading.Lock()
        self.air_client: SonarrClient | None = None

    # ------------------------------------------------------------ helpers

    def progress(self, text: str, fraction: float | None = None, busy: bool = False) -> None:
        self.emit("progress", text, fraction, busy)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    @staticmethod
    def _key(path: str) -> str:
        return to_portable(path)

    def _workers(self) -> int:
        if self.stats.network and self.config.network_workers_enabled:
            return self.config.network_workers
        return self.config.probe_workers

    # ------------------------------------------------------------ entry point

    def run(self, root: str | None = None, sonarr: SonarrClient | None = None,
            air_client: SonarrClient | None = None) -> ScanOutcome:
        """Scan a folder (`root`) or Sonarr (`sonarr`). `air_client` is the Sonarr to ask for air status
        when that's set to come from Sonarr; a Sonarr scan uses its own client."""
        self.air_client = air_client or sonarr
        started = time.monotonic()
        outcome = ScanOutcome(stats=self.stats)
        try:
            seasons = self._collect_sonarr(sonarr) if sonarr is not None else self._collect_folder(root or "")
            outcome.results = self._evaluate(seasons)
        except Cancelled:
            self.stopped = True
        except (ProbeUnavailable, SonarrError) as exc:
            outcome.error = str(exc)
        except FileNotFoundError:
            outcome.error = f"The folder {root} wasn't found. Is the drive connected?"
        except PermissionError:
            outcome.error = f"Dub Checker isn't allowed to open {root}."
        except Exception as exc:
            log.exception("Scan failed")
            outcome.error = f"The scan failed unexpectedly: {exc}"
        outcome.stopped = self.stopped
        outcome.warnings = self.warnings
        outcome.elapsed = time.monotonic() - started
        log.info(self.stats.log_line(outcome.elapsed))
        self.emit("done", outcome)
        return outcome

    # ------------------------------------------------------------ phase 1: folder

    def _collect_folder(self, root: str) -> list[SeasonFiles]:
        self.stats.network = is_network_path(root)
        lister = FolderLister()
        self.progress("Looking through your library...", busy=True)
        show_groups: list[tuple[str, list[ShowGroup]]] = []
        by_key: dict[str, ShowGroup] = {}
        try:
            shows = list_library(root, lister)
            for number, show in enumerate(shows, 1):
                self._check_cancel()
                self.progress(f"Listing folders: show {number} of {len(shows)} ({show.name})", busy=True)
                try:
                    groups = group_show(show, lister)
                except OSError as exc:
                    log.warning("Couldn't list %s: %s", show.path, exc)
                    groups = []
                unique = []
                for g in groups:
                    if g.key in by_key:  # loose files that belong to a show folder
                        by_key[g.key].files.extend(g.files)
                    else:
                        by_key[g.key] = g
                        unique.append(g)
                label = show.name if show.loose_files is not None or not unique else unique[0].show_title
                show_groups.append((label, unique))
        finally:
            self.stats.folders_listed = lister.folders_listed
            self.stats.list_time = lister.seconds

        jobs = [ReadJob(f, number, label)
                for number, (label, groups) in enumerate(show_groups, 1) for g in groups for f in g.files]
        self.stats.files = len(jobs)
        read = self._read_all(jobs, len(show_groups))
        seasons = []
        for _, groups in show_groups:
            for g in groups:
                files = [read[f.path] for f in g.files if f.path in read]
                if files:  # seasons with nothing read yet (Stop) are left out
                    seasons.append(SeasonFiles(g, files, complete=len(files) == len(g.files)))
        return seasons

    # ------------------------------------------------------------ phase 1: Sonarr

    def _collect_sonarr(self, client: SonarrClient) -> list[SeasonFiles]:
        cfg = self.config
        self.progress("Connecting to Sonarr...", busy=True)
        series = client.series()
        if cfg.sonarr_anime_only:
            series = [s for s in series if s.series_type == "anime"]
        series.sort(key=lambda s: natural_key(s.title))

        groups: dict[str, ShowGroup] = {}
        files_by_key: dict[str, list[FileResult]] = {}
        jobs: list[ReadJob] = []
        unreachable, example = 0, ""
        for number, s in enumerate(series, 1):
            self._check_cancel()
            self.progress(f"Asking Sonarr about your shows: {number} of {len(series)} ({s.title})",
                          (number - 1) / max(1, len(series)))
            folder = last_path_part(s.path) or s.title
            local_series_path = map_path(s.path, cfg.sonarr_path_from, cfg.sonarr_path_to)
            reachable: bool | None = None
            episode_files = sorted(client.episode_files(s.id), key=lambda e: (
                season_sort_key(e.get("seasonNumber")), natural_key(str(e.get("path") or ""))))
            for ef in episode_files:
                season = ef.get("seasonNumber")
                key = season_key(folder, season)
                if key not in groups:
                    groups[key] = ShowGroup(key, folder, strip_id_tags(s.title), s.year, season)
                    files_by_key[key] = []
                path = map_path(str(ef.get("path") or ""), cfg.sonarr_path_from, cfg.sonarr_path_to)
                size = int(ef.get("size") or 0)
                raw = tracks_from_media_info(ef.get("mediaInfo"))
                if raw is None:
                    result = FileResult(path, size, source="sonarr", note="Sonarr hasn't analysed this file yet")
                else:
                    result = build_file_result(path, size, 0.0, raw, cfg.ignore_commentary_tracks, "sonarr")
                    self.stats.from_sonarr += 1
                groups[key].files.append(VideoFile(path, size, 0.0))
                files_by_key[key].append(result)

                if cfg.sonarr_read_unknown and result.status is FileStatus.UNLABELED:
                    if reachable is None:  # checked once per series
                        reachable = bool(local_series_path) and os.path.isdir(local_series_path)
                    try:
                        if not reachable:
                            raise OSError
                        st = os.stat(path)
                        jobs.append(ReadJob(VideoFile(path, st.st_size, st.st_mtime), number, s.title))
                    except OSError:
                        unreachable += 1
                        example = example or path

        self.stats.files = sum(len(v) for v in files_by_key.values())
        if unreachable:
            self.warnings.append(
                f"{unreachable:,} files that Sonarr couldn't label couldn't be opened from this PC, for example:\n"
                f"{example}\n\nIf Sonarr runs on another computer or in Docker, set up path mapping in "
                "Settings > Sonarr so Dub Checker can find the files.")
        if jobs:
            self.stats.network = is_network_path(jobs[0].file.path)
            read = self._read_all(jobs, len(series))
            for results in files_by_key.values():
                results[:] = [read.get(f.path, f) for f in results]
        return [SeasonFiles(groups[k], files_by_key[k]) for k in groups]

    # ------------------------------------------------------------ reading

    def _read_all(self, jobs: list[ReadJob], show_count: int) -> dict[str, FileResult]:
        """Read every job's tracks: one batched cache check, then parallel reads of the misses."""
        results: dict[str, FileResult] = {}
        if not jobs:
            return results
        self.progress("Checking which files are already known...", busy=True)
        keys = [self._key(j.file.path) for j in jobs]
        cached = self.cache.get_files({key: (j.file.size, j.file.mtime) for key, j in zip(keys, jobs)})
        misses = []
        for key, job in zip(keys, jobs):
            f = job.file
            raw = cached.get(key)
            if raw is None:
                misses.append(job)
            else:
                results[f.path] = build_file_result(f.path, f.size, f.mtime, raw,
                                                    self.config.ignore_commentary_tracks, "cache")
        self.stats.cached += len(results)
        if not misses:
            return results

        total, done = len(jobs), len(results)
        self.stats.workers = self._workers()
        self.progress(f"Reading audio tracks: file {done:,} of {total:,}", done / total)
        throttle = ReadThrottle(self.config.pause_enabled, self.config.pause_every_files, self.config.pause_seconds,
                                self.cancel, self._on_break)
        writer = FileCacheWriter(self.cache)
        started = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=self.stats.workers, thread_name_prefix="reader")
        try:
            futures = {pool.submit(self._read_one, job, throttle, writer): job for job in misses}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    results[job.file.path] = future.result()
                except Cancelled:
                    self.stopped = True
                    continue
                done += 1
                self.progress(f"Reading audio tracks: file {done:,} of {total:,} "
                              f"(Show {job.show_index} of {show_count}: {job.show_title})", done / total)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            writer.flush()  # also when stopped
            self.stats.read_time += time.monotonic() - started
        return results

    def _read_one(self, job: ReadJob, throttle: ReadThrottle, writer: FileCacheWriter) -> FileResult:
        f = job.file
        throttle.acquire()  # raises Cancelled on Stop, even during a break
        try:
            raw = self.prober.probe(f.path)
        except (Cancelled, ProbeUnavailable):
            raise
        except Exception as exc:  # one bad file never stops the scan
            log.warning("Couldn't read %s: %s", f.path, exc)
            with self._stats_lock:
                self.stats.failed += 1
            return FileResult(f.path, f.size, f.mtime, status=FileStatus.ERROR,
                              error=str(exc) or type(exc).__name__, source="disk")
        finally:
            throttle.release()
        with self._stats_lock:
            self.stats.read += 1
        writer.add(self._key(f.path), f.size, f.mtime, raw)
        return build_file_result(f.path, f.size, f.mtime, raw, self.config.ignore_commentary_tracks, "disk")

    def _on_break(self, seconds_left: float) -> None:
        self.progress(f"Giving the drive a break - reading continues in {math.ceil(seconds_left)} s")

    # ------------------------------------------------------------ phase 2

    def _stop_rest(self, pending: list[SeasonFiles], results: list[ShowResult]) -> None:
        self.stopped = True
        rest = [stopped_result(s.group, s.files) for s in pending]
        results.extend(rest)
        self.emit("results", rest)

    def _evaluate(self, seasons: list[SeasonFiles]) -> list[ShowResult]:
        immediate: list[ShowResult] = []
        pending: list[SeasonFiles] = []
        for s in seasons:
            if not s.complete:
                immediate.append(stopped_result(s.group, s.files))
            elif needs_lookup(s.files) or self.overrides.get(s.group.key) is not None:
                pending.append(s)
            else:
                immediate.append(categorize_local(s.group, s.files))
        results = list(immediate)
        self.emit("results", immediate)  # everything decided locally appears at once
        if self.stopped:
            self._stop_rest(pending, results)
            return results

        service: LookupService | None = None
        started = time.monotonic()
        try:
            for number, s in enumerate(pending):
                if self.cancel.is_set():
                    self._stop_rest(pending[number:], results)
                    break
                try:
                    if service is None:
                        self.progress("Getting the list of dubbed shows...", busy=True)
                        service = self.lookup_factory()
                    self.progress(f"Checking AniList: show {number + 1} of {len(pending)} ({s.group.display_title})",
                                  number / len(pending))
                    result = evaluate_season(s.group, s.files, service, self.overrides.get(s.group.key),
                                             self.config.confidence_threshold, self.config.air_status_source)
                except Cancelled:
                    self._stop_rest(pending[number:], results)
                    break
                self.stats.lookups += 1
                results.append(result)
                self.emit("result", result)
            if not self.stopped and self.config.air_status_source == "anilist":
                self._add_air_status(results, service)
            elif not self.stopped and self.config.air_status_source == "sonarr":
                self._add_sonarr_air_status(results)
        finally:
            self.stats.lookup_time = time.monotonic() - started
        return results

    def _add_sonarr_air_status(self, results: list[ShowResult]) -> None:
        """Air status from Sonarr: its series list and calendar, two requests for the whole library.
        Shows are matched by folder name (then title), so this works after a local files scan too."""
        if self.air_client is None:
            self.warnings.append("Air status is set to come from Sonarr, but Sonarr isn't set up. Enter its "
                                 "address and API key in Settings > Sonarr, or choose AniList in Settings > General.")
            return
        self.progress("Getting air status from Sonarr...", busy=True)
        try:
            sonarr = SonarrAirStatus.fetch(self.air_client)
        except SonarrError as exc:
            self.warnings.append(f"Couldn't get air status from Sonarr: {exc}")
            return
        updated = []
        for index, season in enumerate(results):
            air = sonarr.air(season.group.folder_name, season.group.show_title, season.group.season)
            if air is not None:
                results[index] = dataclasses.replace(season, air=air)
                updated.append(results[index])
        if updated:
            self.emit("results", updated)

    def _add_air_status(self, results: list[ShowResult], service: LookupService | None) -> None:
        """Last step: seasons decided from your files skipped AniList, so look them up now for their air
        status. Their group doesn't change. Seasons still missing English audio go first."""
        todo = [i for i, r in enumerate(results) if r.match is None and not r.looked_up and not r.stopped]
        todo.sort(key=lambda i: results[i].category is not Category.NEEDS_DUB)
        for number, index in enumerate(todo):
            if self.cancel.is_set():
                self.stopped = True
                return
            season = results[index]
            try:
                if service is None:
                    self.progress("Getting ready to check air status...", busy=True)
                    service = self.lookup_factory()
                self.progress(f"Checking air status: show {number + 1} of {len(todo)} ({season.group.display_title})",
                              number / len(todo))
                lookup = service.lookup(season.group)
            except Cancelled:
                self.stopped = True
                return
            except Exception as exc:  # e.g. AniList unreachable: the season keeps its result, just no air status
                log.warning("Air status lookup failed for %s: %s", season.group.display_title, exc)
                continue
            self.stats.lookups += 1
            if lookup.match is not None:
                results[index] = dataclasses.replace(season, match=lookup.match, air=air_from_match(lookup.match))
                self.emit("result", results[index])
