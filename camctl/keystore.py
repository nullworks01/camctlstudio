"""The Claude key, saved inside Studio: encrypted for this Windows user with DPAPI (no install, no clear text).

- File: %APPDATA%\\camctl\\claude_key.bin (DPAPI blob; only this Windows user on this laptop can decrypt it).
- The key is never returned to the page, never logged, never put into an error message.
- ANTHROPIC_API_KEY (environment / Windows user setting) stays the fallback; a key saved in Studio wins.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

BASE = Path(os.environ.get("APPDATA") or Path.home()) / "camctl"
PATH = BASE / "claude_key.bin"
_ENTROPY = b"camctl-studio-claude-key-v1"
_SHAPE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{10,300}$")


def looks_like_a_key(key: str) -> bool:
    return bool(_SHAPE.match(key or ""))


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _in(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _crypt(fn, data: bytes) -> bytes:
        src, _k1 = _in(data)
        ent, _k2 = _in(_ENTROPY)
        out = _Blob()
        flags = 0x1                                       # CRYPTPROTECT_UI_FORBIDDEN
        if fn == "protect":
            ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), "camctl", ctypes.byref(ent), None, None, flags, ctypes.byref(out))
        else:
            ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, flags, ctypes.byref(out))
        if not ok:
            raise OSError("Windows could not " + ("protect" if fn == "protect" else "open") + " the saved key")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
else:                                                     # not Windows: refuse to store in clear text
    def _crypt(fn, data: bytes) -> bytes:
        raise OSError("Saving the key needs Windows (DPAPI)")


def save(key: str) -> None:
    key = (key or "").strip()
    if not looks_like_a_key(key):
        raise ValueError("That doesn't look like a Claude key (it starts with sk-ant-).")
    blob = _crypt("protect", key.encode("utf-8"))
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, PATH)


def load() -> str:
    try:
        blob = PATH.read_bytes()
    except OSError:
        return ""
    try:
        return _crypt("unprotect", blob).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def remove() -> bool:
    try:
        PATH.unlink()
        return True
    except FileNotFoundError:
        return False


def saved() -> bool:
    return PATH.exists() and bool(load())


def saved_since() -> float | None:
    """When the app key was saved (the file's time, never its contents)."""
    try:
        return PATH.stat().st_mtime if PATH.exists() else None
    except OSError:
        return None
