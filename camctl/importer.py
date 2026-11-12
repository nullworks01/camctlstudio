"""Takes log + automatic import from the camera card.

Every photo and every recording is logged as a *take* in the shoot (NOTES\\takes.json):
time, kind, shot name, key settings and the live-view measurements. When the card
becomes reachable (card reader, or the camera in USB Mass Storage mode) new clips and
photos are COPIED into the shoot, verified (size + SHA-256) and matched to their takes
by time. Nothing on the card is ever deleted, renamed or changed.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import string
import threading
import time
from pathlib import Path

from . import handoff

VIDEO_EXT = {".mp4", ".mov", ".mxf"}
PHOTO_EXT = {".arw", ".jpg", ".jpeg", ".heif", ".hif"}
MATCH_TOLERANCE_S = 15 * 60          # camera clock vs PC clock


# ---------------------------------------------------------------- takes log
_lock = threading.Lock()


def _takes_file(sh: dict | None = None) -> Path:
    sh = sh or handoff.ensure_shoot()
    return Path(sh["path"]) / "NOTES" / "takes.json"


def takes(sh: dict | None = None) -> list[dict]:
    try:
        return json.loads(_takes_file(sh).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items: list[dict], sh: dict | None = None) -> None:
    f = _takes_file(sh)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(items, indent=1), encoding="utf-8")


def log_take(kind: str, start: float, end: float, shot: str = "", settings: dict | None = None,
             measure: dict | None = None, files: list[str] | None = None) -> dict:
    with _lock:
        items = takes()
        t = {"id": (max((x["id"] for x in items), default=0) + 1), "kind": kind, "start": start, "end": end,
             "shot": shot, "settings": settings or {}, "measure": measure or {}, "files": files or [],
             "imported": bool(files)}
        items.append(t)
        _save(items)
        return t


def update_take(tid: int, **kw) -> dict | None:
    with _lock:
        items = takes()
        for t in items:
            if t["id"] == tid:
                t.update(kw)
                _save(items)
                return t
    return None


# ---------------------------------------------------------------- the card
def find_card() -> Path | None:
    """Root of a reachable Sony card (PRIVATE\\M4ROOT or DCIM on a removable drive)."""
    for d in string.ascii_uppercase:
        root = Path(f"{d}:/")
        try:
            if (root / "PRIVATE" / "M4ROOT").is_dir() or ((root / "DCIM").is_dir() and d != "C"):
                return root
        except OSError:
            continue
    return None


def card_files(root: Path) -> list[Path]:
    out = []
    clipdir = root / "PRIVATE" / "M4ROOT" / "CLIP"
    if clipdir.is_dir():
        out += [f for f in clipdir.iterdir() if f.suffix.lower() in VIDEO_EXT]
    dcim = root / "DCIM"
    if dcim.is_dir():
        for sub in dcim.iterdir():
            if sub.is_dir():
                out += [f for f in sub.iterdir() if f.suffix.lower() in PHOTO_EXT | VIDEO_EXT]
    return sorted(out, key=lambda f: f.stat().st_mtime)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_verified(src: Path, dst: Path) -> Path:
    """Copy src to dst and verify size + SHA-256; the source is never touched."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size and _sha(dst) == _sha(src):
        return dst
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copy2(src, tmp)
    if tmp.stat().st_size != src.stat().st_size or _sha(tmp) != _sha(src):
        tmp.unlink(missing_ok=True)
        raise OSError(f"Copy of {src.name} didn't verify; the card file is untouched, try again.")
    tmp.replace(dst)
    return dst


def clock_offset(files: list[Path], open_takes: list[dict]) -> float:
    """Camera clock minus PC clock, from the newest clip vs the newest video take (0 if unknown).
    The camera clock can be off by hours (time zone)."""
    vids = [f for f in files if f.suffix.lower() in VIDEO_EXT]
    vt = [t for t in open_takes if t["kind"] == "video"]
    if not vids or not vt:
        return 0.0
    off = max(f.stat().st_mtime for f in vids) - max(t["end"] for t in vt)
    return off if abs(off) <= 24 * 3600 else 0.0


def match(files: list[Path], open_takes: list[dict]) -> dict[str, int]:
    """file name -> take id, by end time (after correcting the camera's clock offset)."""
    vids = [f for f in files if f.suffix.lower() in VIDEO_EXT]
    vt = [t for t in open_takes if t["kind"] == "video"]
    if not vids or not vt:
        return {}
    offset = clock_offset(files, open_takes)
    out, used = {}, set()
    for t in sorted(vt, key=lambda t: t["end"]):
        best = min((f for f in vids if f.name not in used), default=None,
                   key=lambda f: abs(f.stat().st_mtime - offset - t["end"]))
        if best is not None and abs(best.stat().st_mtime - offset - t["end"]) < 120:
            out[best.name] = t["id"]
            used.add(best.name)
    return out


class Importer:
    """Watches for the card and imports new files of the current shoot (copy + verify only)."""

    def __init__(self, on_imported=lambda path, take: None):
        self.on_imported = on_imported
        self.status = {"card": None, "waiting": [], "imported": [], "busy": False, "error": "",
                       "step": "Put the SD card in the laptop, or set the camera's USB Connection to Mass Storage."}
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self._loop, daemon=True, name="card-import")
            self.thread.start()

    def pending_takes(self) -> list[dict]:
        sh = handoff.current()
        return [t for t in takes(sh) if not t.get("imported")] if sh else []

    def scan_once(self) -> None:
        sh = handoff.current()
        root = find_card()
        self.status["card"] = str(root) if root else None
        pend = [t for t in takes(sh) if not t.get("imported")] if sh else []
        self.status["waiting"] = [{"take": t["id"], "shot": t.get("shot", ""), "kind": t["kind"],
                                   "at": t["end"]} for t in pend]
        if not root or not sh:
            return
        since = sh["created"] - 600
        recent = [f for f in card_files(root) if f.stat().st_mtime >= since - 24 * 3600]
        offset = clock_offset(recent, pend)
        files = [f for f in recent if f.stat().st_mtime - offset >= since]   # only this shoot's files
        folder = Path(sh["path"])
        already = {p.name for sub in ("VIDEO", "PHOTO RAW", "PHOTO JPEG") if (folder / sub).is_dir()
                   for p in (folder / sub).iterdir()}
        new = [f for f in files if f.name not in already]
        if not new:
            return
        m = match(new, pend)
        self.status["busy"] = True
        try:
            for f in new:
                ext = f.suffix.lower()
                sub = "VIDEO" if ext in VIDEO_EXT else "PHOTO RAW" if ext == ".arw" else "PHOTO JPEG"
                try:
                    dst = copy_verified(f, folder / sub / f.name)
                except OSError as exc:
                    self.status["error"] = str(exc)
                    continue
                tid = m.get(f.name)
                take = update_take(tid, imported=True, card_file=f.name, files=[str(dst)]) if tid else None
                self.status["imported"] = (self.status["imported"] + [f.name])[-30:]
                self.on_imported(dst, take)
        finally:
            self.status["busy"] = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001
                self.status["error"] = str(exc)
            self._stop.wait(4.0)
