"""Reading audio tracks from video files, and deciding what language they are.

MediaInfo (via pymediainfo) is used first and only reads the file headers.
If MediaInfo isn't available, ffprobe is used instead; it is looked for in
``UserData/bin``, then on PATH, and finally downloaded into ``UserData/bin``.
"""
from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable

from dubchecker.models import ORIGINAL_LANGS, AudioTrack, Cancelled, FileResult, FileStatus, Lang

log = logging.getLogger(__name__)

LANGUAGE_TAGS: dict[Lang, frozenset[str]] = {
    Lang.JAPANESE: frozenset({"jpn", "ja", "jp", "japanese"}),
    Lang.KOREAN: frozenset({"kor", "ko", "korean"}),
    # zh/zho/chi cover Chinese generally; cmn is Mandarin and yue Cantonese.
    Lang.CHINESE: frozenset({"chi", "zho", "zh", "chinese", "cmn", "yue", "mandarin", "cantonese"}),
    Lang.ENGLISH: frozenset({"eng", "en", "english"}),
}
# Hints in a track's name, used when its language tag is missing or "und".
_TITLE_HINTS: dict[Lang, re.Pattern[str]] = {
    Lang.ENGLISH: re.compile(r"(?<![a-z])(?:english|eng)(?![a-z])", re.I),
    Lang.JAPANESE: re.compile(r"(?<![a-z])(?:japanese|jpn)(?![a-z])|日本語", re.I),
    Lang.KOREAN: re.compile(r"(?<![a-z])(?:korean|kor)(?![a-z])|한국어", re.I),
    Lang.CHINESE: re.compile(r"(?<![a-z])(?:chinese|mandarin|cantonese|chi)(?![a-z])"
                             r"|中文|[国國]语|[国國]語|普通[话話]|[粤粵][语語]|[华華][语語]", re.I),
}
_COMMENTARY_RE = re.compile(r"commentary", re.I)

StatusCallback = Callable[[str], None]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash on Windows


# ---------------------------------------------------------------- classification

def language_from_tag(tag: str | None) -> Lang | None:
    """``ja``/``jpn``/``ja-JP``/``Japanese``, ``ko``/``kor``, ``zh``/``zho``/``zh-TW``/``cmn``/``yue``, ``en``/``eng``.

    Unknown tags and ``und`` give None.
    """
    base = re.split(r"[-_ ]", (tag or "").strip().lower(), maxsplit=1)[0]
    return next((lang for lang, tags in LANGUAGE_TAGS.items() if base in tags), None)


def classify_language(tag: str | None, title: str | None = "") -> Lang:
    """The language of one track: from its tag, otherwise from a hint in its name."""
    lang = language_from_tag(tag)
    if lang is not None:
        return lang
    hinted = [lang for lang, pattern in _TITLE_HINTS.items() if pattern.search(title or "")]
    return hinted[0] if len(hinted) == 1 else Lang.UNKNOWN  # no hint, or conflicting hints


def is_commentary(title: str | None) -> bool:
    return bool(title and _COMMENTARY_RE.search(title))


def make_tracks(raw_tracks: list[dict], ignore_commentary: bool = True) -> list[AudioTrack]:
    tracks = []
    for position, raw in enumerate(raw_tracks):
        title = str(raw.get("title") or "")
        tracks.append(AudioTrack(
            index=int(raw.get("index", position)),
            language=str(raw.get("language") or ""),
            title=title,
            codec=str(raw.get("codec") or ""),
            channels=str(raw.get("channels") or ""),
            lang=classify_language(raw.get("language"), title),
            ignored=ignore_commentary and is_commentary(title),
        ))
    return tracks


def classify_file(tracks: list[AudioTrack]) -> FileStatus:
    """Dual audio as soon as an original-language track (Japanese, Korean or Chinese) and English are both there."""
    langs = {t.lang for t in tracks if not t.ignored}
    has_original = any(lang in ORIGINAL_LANGS for lang in langs)
    if has_original and Lang.ENGLISH in langs:
        return FileStatus.DUAL
    if not langs or Lang.UNKNOWN in langs:
        return FileStatus.UNLABELED
    return FileStatus.ORIGINAL_ONLY if has_original else FileStatus.ENGLISH_ONLY


def build_file_result(path: str, size: int, mtime: float, raw_tracks: list[dict],
                      ignore_commentary: bool, source: str) -> FileResult:
    tracks = make_tracks(raw_tracks, ignore_commentary)
    return FileResult(path=path, size=size, mtime=mtime, tracks=tracks, status=classify_file(tracks), source=source)


# ---------------------------------------------------------------- errors

class ProbeError(Exception):
    """One file couldn't be read."""


class ProbeUnavailable(Exception):
    """Nothing on this computer can read video files. The message is shown to the user."""


def _unavailable_message() -> str:
    if sys.platform.startswith("linux"):
        how = ("Install MediaInfo (for example:  sudo apt install libmediainfo0v5) "
               "or FFmpeg (sudo apt install ffmpeg)")
    elif sys.platform == "darwin":
        how = "Install MediaInfo or FFmpeg (for example:  brew install ffmpeg)"
    else:
        how = "Install MediaInfo from https://mediaarea.net or FFmpeg from https://ffmpeg.org"
    return ("Dub Checker couldn't find a way to read your video files.\n\n"
            "MediaInfo isn't available and ffprobe couldn't be downloaded (check your internet connection).\n\n"
            f"{how}, or put ffprobe into the UserData\\bin folder, then scan again.")


# ---------------------------------------------------------------- ffprobe download

FFBINARIES_API = "https://ffbinaries.com/api/v1/version/latest"
GYAN_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
EVERMEET_ZIP = "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip"
JOHNVANSICKLE_TAR = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"


def ffprobe_name() -> str:
    return "ffprobe.exe" if os.name == "nt" else "ffprobe"


def ffbinaries_platform() -> str:
    machine = platform.machine().lower()
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    if os.name == "nt":
        return f"windows-{bits}"
    if sys.platform == "darwin":
        return "osx-64"
    if machine in ("aarch64", "arm64"):
        return "linux-arm64"
    if machine.startswith("arm"):
        return "linux-armhf"
    return f"linux-{bits}"


def find_ffprobe(bin_dir: Path) -> str | None:
    local = bin_dir / ffprobe_name()
    if local.is_file():
        return str(local)
    return shutil.which("ffprobe")


def _download(url: str, status: StatusCallback | None, cancel: threading.Event | None) -> bytes:
    import requests
    with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "DubChecker"}) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        buffer = io.BytesIO()
        for chunk in response.iter_content(chunk_size=256 * 1024):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            buffer.write(chunk)
            if status:
                done = buffer.tell() // (1024 * 1024)
                status(f"Downloading ffprobe ({done} MB of {total // (1024 * 1024)} MB)..." if total
                       else f"Downloading ffprobe ({done} MB)...")
        return buffer.getvalue()


def _extract_member(archive: bytes, name: str, target: Path) -> None:
    """Pull the file called ``name`` out of a zip or tar archive into ``target``."""
    if zipfile.is_zipfile(io.BytesIO(archive)):
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for member in zf.namelist():
                if member.replace("\\", "/").rsplit("/", 1)[-1] == name:
                    data = zf.read(member)
                    break
            else:
                raise ProbeError(f"{name} wasn't in the download")
    else:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
            member = next((m for m in tf.getmembers() if m.isfile() and m.name.rsplit("/", 1)[-1] == name), None)
            if member is None:
                raise ProbeError(f"{name} wasn't in the download")
            data = tf.extractfile(member).read()  # type: ignore[union-attr]
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        tmp.write(data)
    os.replace(tmp.name, target)
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def download_ffprobe(bin_dir: Path, status: StatusCallback | None = None,
                     cancel: threading.Event | None = None) -> str:
    """Download ffprobe into ``bin_dir``, trying several sources. Returns its path."""
    import requests
    target = bin_dir / ffprobe_name()
    sources: list[Callable[[], str]] = []

    def from_ffbinaries() -> str:
        info = requests.get(FFBINARIES_API, timeout=30).json()
        return info["bin"][ffbinaries_platform()]["ffprobe"]

    sources.append(from_ffbinaries)
    if os.name == "nt":
        sources.append(lambda: GYAN_ZIP)
    elif sys.platform == "darwin":
        sources.append(lambda: EVERMEET_ZIP)
    elif platform.machine().lower() in ("x86_64", "amd64"):
        sources.append(lambda: JOHNVANSICKLE_TAR)

    for source in sources:
        try:
            url = source()
            log.info("Downloading ffprobe from %s", url)
            _extract_member(_download(url, status, cancel), ffprobe_name(), target)
            subprocess.run([str(target), "-version"], capture_output=True, timeout=30, check=True,
                           creationflags=_NO_WINDOW)
            log.info("ffprobe saved to %s", target)
            return str(target)
        except Cancelled:
            raise
        except Exception as exc:
            log.warning("ffprobe download failed: %s", exc)
            target.unlink(missing_ok=True)
    raise ProbeUnavailable(_unavailable_message())



# ---------------------------------------------------------------- readers

def _pick_language(language: str | None, others: list | None) -> str:
    """MediaInfo gives several spellings ('ja', 'Japanese', 'jpn'); prefer one we recognise."""
    candidates = [str(c) for c in [language, *(others or [])] if c]
    known = [c for c in candidates if language_from_tag(c) is not None]
    # Prefer the 3-letter code (what MKV tools show, e.g. "jpn") over MediaInfo's "ja".
    return next((c for c in known if len(c) == 3), known[0] if known else (candidates[0] if candidates else ""))


# Ask MediaInfo for just the fields we use, as plain text, instead of its full XML report (which
# pymediainfo then has to parse): about twice as fast per file. Control characters separate the
# sections, tracks and fields, so track names containing "|" or tabs can't break it.
_END_GENERAL, _END_TRACK, _FIELD = "\x1d", "\x1e", "\x1f"
_MEDIAINFO_TEMPLATE = (f"General;%Format%{_END_GENERAL}\n"
                       f"Audio;%Language%{_FIELD}%Language/String3%{_FIELD}%Title%{_FIELD}%Format%{_FIELD}"
                       f"%Channel(s)%{_END_TRACK}")


def read_with_mediainfo(path: str) -> list[dict]:
    from pymediainfo import MediaInfo
    output = MediaInfo.parse(path, parse_speed=0, output=_MEDIAINFO_TEMPLATE)
    container, _, audio = str(output).partition(_END_GENERAL)
    if not container.strip() and not audio.strip():
        raise ProbeError("This doesn't look like a video file Dub Checker can read")
    tracks = []
    for position, record in enumerate(r for r in audio.split(_END_TRACK) if r.strip(_FIELD + "\r\n ")):
        code, code3, title, codec, channels = (record.strip("\r\n").split(_FIELD) + [""] * 5)[:5]
        tracks.append({
            "index": position,
            "language": _pick_language(code, [code3]),
            "title": title,
            "codec": codec,
            "channels": channels,
        })
    return tracks


def read_with_ffprobe(ffprobe: str, path: str) -> list[dict]:
    command = [ffprobe, "-v", "error", "-probesize", "2M", "-analyzeduration", "0", "-select_streams", "a",
               "-show_entries", "stream=index,codec_name,channels:stream_tags=language,title",
               "-of", "json", path]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("ffprobe took too long to read this file") from exc
    try:
        streams = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}").get("streams")
    except ValueError:
        streams = None
    if proc.returncode != 0 and not streams:  # a non-zero exit with streams is still usable
        message = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ProbeError(message[-1] if message else f"ffprobe failed (exit code {proc.returncode})")
    tracks = []
    for position, stream in enumerate(streams or []):
        tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
        tracks.append({
            "index": position,
            "language": tags.get("language", ""),
            "title": tags.get("title", ""),
            "codec": stream.get("codec_name", ""),
            "channels": str(stream.get("channels") or ""),
        })
    return tracks


class MediaProber:
    """Reads the audio tracks of one file. Safe to use from several threads."""

    def __init__(self, bin_dir: Path, status: StatusCallback | None = None,
                 cancel: threading.Event | None = None) -> None:
        self.bin_dir = bin_dir
        self.status = status
        self.cancel = cancel
        self._lock = threading.Lock()
        self._mediainfo_ok: bool | None = None
        self._ffprobe: str | None = None
        self._ffprobe_checked = False
        self._download_failed: ProbeUnavailable | None = None

    def mediainfo_available(self) -> bool:
        if self._mediainfo_ok is not None:  # decided already: no need for every reader thread to take the lock
            return self._mediainfo_ok
        with self._lock:
            if self._mediainfo_ok is None:
                try:
                    from pymediainfo import MediaInfo
                    self._mediainfo_ok = bool(MediaInfo.can_parse())
                except Exception as exc:
                    log.warning("MediaInfo isn't available: %s", exc)
                    self._mediainfo_ok = False
                if not self._mediainfo_ok:
                    log.info("Using ffprobe because MediaInfo can't be loaded")
            return self._mediainfo_ok

    def _get_ffprobe(self, download: bool) -> str | None:
        with self._lock:
            if self._ffprobe is None and not self._ffprobe_checked:
                self._ffprobe = find_ffprobe(self.bin_dir)
                self._ffprobe_checked = True
            if self._ffprobe is None and download:
                if self._download_failed is not None:
                    raise self._download_failed
                try:
                    self._ffprobe = download_ffprobe(self.bin_dir, self.status, self.cancel)
                except ProbeUnavailable as exc:
                    self._download_failed = exc
                    raise
            return self._ffprobe

    def probe(self, path: str) -> list[dict]:
        """Raw track dicts (index, language, title, codec, channels) for one file."""
        if self.mediainfo_available():
            try:
                return read_with_mediainfo(path)
            except (Cancelled, ProbeUnavailable):
                raise
            except Exception as exc:
                ffprobe = self._get_ffprobe(download=False)
                if ffprobe is None:
                    raise ProbeError(str(exc) or type(exc).__name__) from exc
                log.info("MediaInfo failed on %s (%s); trying ffprobe", path, exc)
                return read_with_ffprobe(ffprobe, path)
        ffprobe = self._get_ffprobe(download=True)
        assert ffprobe is not None
        return read_with_ffprobe(ffprobe, path)
