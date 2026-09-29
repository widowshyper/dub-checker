"""Deciding which group each season belongs in, with notes in plain English."""
from __future__ import annotations

from collections import Counter

from dubchecker.dub_sources import SOURCE_FILES, LookupResult, combine_dub_signals
from dubchecker.models import (AniListMatch, Category, DubInfo, FileResult, FileStatus, ShowGroup, ShowResult,
                               language_word)

STOPPED_NOTE = "Scan was stopped before this show was checked"


def needs_lookup(files: list[FileResult]) -> bool:
    """A season with at least one dual-audio file (original + English) has already proven a dub exists."""
    return not any(f.status is FileStatus.DUAL for f in files)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def file_notes(files: list[FileResult]) -> list[str]:
    counts = Counter(f.status for f in files)
    total = len(files)
    notes = []
    if counts[FileStatus.ORIGINAL_ONLY]:
        n = counts[FileStatus.ORIGINAL_ONLY]
        language = language_word([f for f in files if f.status is FileStatus.ORIGINAL_ONLY])
        notes.append(f"{n} of {total} episodes only have {language} audio" if total > 1
                     else f"The episode only has {language} audio")
    if counts[FileStatus.UNLABELED]:
        n = counts[FileStatus.UNLABELED]
        notes.append(f"{_plural(n, 'file')} {'has' if n == 1 else 'have'} audio without a language label "
                     "- check them in the episode list")
    if counts[FileStatus.ERROR]:
        n = counts[FileStatus.ERROR]
        notes.append(f"{_plural(n, 'file')} couldn't be read")
    return notes


def _has_problem_files(files: list[FileResult]) -> bool:
    return any(f.status in (FileStatus.UNLABELED, FileStatus.ERROR) for f in files)


def categorize_local(group: ShowGroup, files: list[FileResult], match: AniListMatch | None = None,
                     extra_notes: list[str] | None = None) -> ShowResult:
    """No AniList lookup needed: your files already show an English dub exists."""
    if _has_problem_files(files):
        category = Category.CHECK
    elif any(f.status is FileStatus.ORIGINAL_ONLY for f in files):
        category = Category.NEEDS_DUB
    else:
        category = Category.FULLY_DUBBED
    notes = file_notes(files) + list(extra_notes or [])
    return ShowResult(group, files, category, match=match, dub=DubInfo(True, [SOURCE_FILES]), notes=notes)


def categorize_lookup(group: ShowGroup, files: list[FileResult], lookup: LookupResult,
                      threshold: float) -> ShowResult:
    """Categorise a season that had no dual-audio files, using the AniList lookup."""
    local_english = any(f.status in (FileStatus.ENGLISH_ONLY, FileStatus.DUAL) for f in files)
    dub = combine_dub_signals(lookup.anilist_va, lookup.maldubs, local_english)
    match = lookup.match
    notes = list(lookup.notes)
    all_english = bool(files) and all(f.status is FileStatus.ENGLISH_ONLY for f in files)

    if all_english:
        category = Category.FULLY_DUBBED
    elif match is None:
        category = Category.CHECK
        notes.append("Use Fix match to pick the right AniList entry")
    elif match.confidence < threshold:
        category = Category.CHECK
        notes.append(f"Not sure AniList picked the right show ({match.confidence:.0f}% match) "
                     "- use Fix match if it's wrong")
    elif _has_problem_files(files):
        category = Category.CHECK
    elif dub.exists is None:
        category = Category.CHECK
        notes.append("Couldn't check whether an English dub exists")
    elif dub.exists:
        category = Category.NEEDS_DUB
        if dub.incomplete:
            notes.append("The MAL-Dubs list says the English dub isn't complete yet")
    else:
        category = Category.NO_DUB
        notes.append("No English dub found on AniList or the MAL-Dubs list")

    return ShowResult(group, files, category, match=match, dub=dub, notes=file_notes(files) + notes,
                      looked_up=True)


def stopped_result(group: ShowGroup, files: list[FileResult]) -> ShowResult:
    return ShowResult(group, files, Category.CHECK, notes=[STOPPED_NOTE], stopped=True)
