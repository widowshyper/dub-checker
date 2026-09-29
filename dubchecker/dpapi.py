"""Encrypting secrets (the Sonarr API key) with Windows DPAPI.

DPAPI is what Windows uses for saved passwords: the data is locked to the current
Windows account on this PC, with no password for us to store or manage. The result
can't be read on another PC or by another Windows account, so a moved portable
folder needs the key entered again. It doesn't stop programs already running as
you, which could ask Windows to decrypt it too.

Off Windows these functions report that encryption isn't available and the caller
keeps the old behaviour.
"""
from __future__ import annotations

import base64
import ctypes
import logging
import sys

log = logging.getLogger(__name__)

PREFIX = "dpapi:v1:"
# Extra input that must match to decrypt, so another program using DPAPI for the same
# Windows account can't decrypt this simply by passing the blob to Windows.
_ENTROPY = b"Dub Checker - Sonarr API key"
_UI_FORBIDDEN = 0x1  # never show a Windows dialog


def available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes.wintypes as wt

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    def _call(function: object, data: bytes) -> bytes | None:
        """CryptProtectData and CryptUnprotectData take the same arguments; None means Windows refused."""
        source, _keep = _blob(data)
        entropy, _keep_entropy = _blob(_ENTROPY)
        result = _Blob()
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, _UI_FORBIDDEN,  # type: ignore
                        ctypes.byref(result)):
            return None
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(result.pbData)

    def _protect(data: bytes) -> bytes | None:
        return _call(ctypes.windll.crypt32.CryptProtectData, data)

    def _unprotect(data: bytes) -> bytes | None:
        return _call(ctypes.windll.crypt32.CryptUnprotectData, data)


def encrypt(text: str) -> str | None:
    """``dpapi:v1:<base64>``, or None when encryption isn't available here."""
    if not available():
        return None
    try:
        sealed = _protect(text.encode("utf-8"))
    except OSError as exc:
        log.warning("Windows couldn't encrypt the API key: %s", exc)
        return None
    return PREFIX + base64.b64encode(sealed).decode("ascii") if sealed is not None else None


def decrypt(token: str) -> str | None:
    """The original text, or None if it can't be decrypted here (another PC or Windows account, or damaged)."""
    if not available() or not token.startswith(PREFIX):
        return None
    try:
        opened = _unprotect(base64.b64decode(token[len(PREFIX):], validate=True))
        return opened.decode("utf-8") if opened is not None else None
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        log.warning("The saved API key couldn't be decrypted: %s", exc)
        return None
