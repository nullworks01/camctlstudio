"""Playground › Day reel: "the moment before" each photo, from the live picture Studio already receives.

- While his switch is on, the last 3 seconds of live-view frames are kept IN MEMORY ONLY, exactly as received.
- When he takes a photo with Studio (his press, or a smart / special shot he armed), those frames become a small silent
  MP4 in <shoot>\\REEL\\clips (written in the background, after the shutter command went out).
- "Make today's reel" joins today's clips in time order, each followed by its photo (the camera's JPEG, fitted
  inside 1280x720 with dark bars, never cropped or stretched): hard cuts, no music, no titles, no colour change.
Photos and clips are only read; only "Delete today's clips" removes anything, and only clips. Never Claude.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import video

SECONDS = 3.0
FPS = 25
MAX_FRAMES = int(SECONDS * 40)             # 40 fps would be more than any live view delivers
MAX_BYTES = 40_000_000                      # and never more than this much memory
PHOTO_S = 1.5
OUT_W, OUT_H = 1280, 720


class Buffer:
    """The rolling memory of the last few seconds of live picture (nothing on disk)."""

    def __init__(self):
        self.on = False
        self.paused = False
        self.frames: deque = deque()
        self.bytes = 0
        self.lock = threading.Lock()

    def feed(self, jpeg: bytes, t: float | None = None) -> None:
        if not self.on or self.paused or not jpeg:
            return
        t = time.time() if t is None else t
        with self.lock:
            self.frames.append((t, jpeg))
            self.bytes += len(jpeg)
            while self.frames and (t - self.frames[0][0] > SECONDS or len(self.frames) > MAX_FRAMES
                                   or self.bytes > MAX_BYTES):
                self.bytes -= len(self.frames.popleft()[1])

    def clear(self) -> None:
        with self.lock:
            self.frames.clear()
            self.bytes = 0

    def set_on(self, on: bool) -> None:
        self.on = bool(on)
        if not self.on:
            self.clear()                      # switched off: emptied at once

    def pause(self, paused: bool) -> None:
        if paused and not self.paused:
            self.clear()
        self.paused = bool(paused)

    def snapshot(self, now: float | None = None) -> list[tuple[float, bytes]]:
        """The frames of the last SECONDS (a stale buffer after a disconnect gives nothing)."""
        now = time.time() if now is None else now
        with self.lock:
            return [(t, j) for t, j in self.frames if now - t <= SECONDS + 0.3]


# ---------------------------------------------------------------- files
def folder(shoot: Path) -> Path:
    return Path(shoot) / "REEL"


def _index_file(shoot: Path) -> Path:
    return folder(shoot) / "clips" / "index.json"


def index(shoot: Path) -> dict:
    try:
        return json.loads(_index_file(shoot).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(shoot: Path, d: dict) -> None:
    f = _index_file(shoot)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, f)


def pref(shoot: Path | None) -> bool:
    """The switch, remembered per shoot."""
    if not shoot:
        return False
    try:
        return bool(json.loads((Path(shoot) / "NOTES" / "reel.json").read_text(encoding="utf-8")).get("on"))
    except (OSError, ValueError):
        return False


def save_pref(shoot: Path, on: bool) -> None:
    f = Path(shoot) / "NOTES" / "reel.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"on": bool(on)}), encoding="utf-8")


def _creation():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def constant_rate(frames: list[tuple[float, bytes]], fps: int = FPS) -> list[bytes]:
    """The frames at a constant rate from their real arrival times (a frame repeats until the next one arrived)."""
    if not frames:
        return []
    frames = sorted(frames, key=lambda f: f[0])
    t0, t1 = frames[0][0], frames[-1][0]
    n = max(1, int(round((t1 - t0) * fps)) + 1)
    out, k = [], 0
    for i in range(n):
        t = t0 + i / fps
        while k + 1 < len(frames) and frames[k + 1][0] <= t + 1e-6:
            k += 1
        out.append(frames[k][1])
    return out


def write_clip(frames: list[tuple[float, bytes]], shoot: Path, photos: list[str], when: float | None = None) -> Path | None:
    """One small silent MP4 of the moment before a photo, linked to that photo in the clip index."""
    seq = constant_rate(frames)
    if len(seq) < 2:
        return None
    when = when or time.time()
    clips = folder(shoot) / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    out = clips / (time.strftime("%Y%m%d-%H%M%S", time.localtime(when)) + ".mp4")
    n = 2
    while out.exists():
        out = clips / (time.strftime("%Y%m%d-%H%M%S", time.localtime(when)) + f"-{n}.mp4")
        n += 1
    p = subprocess.Popen([video.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "image2pipe", "-c:v", "mjpeg",
                          "-framerate", str(FPS), "-i", "-", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-an",
                          "-movflags", "+faststart", str(out)],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=_creation())
    try:
        for j in seq:
            p.stdin.write(j)
        p.stdin.close()
        p.wait(timeout=60)
    except Exception:  # noqa: BLE001
        p.kill()
        out.unlink(missing_ok=True)
        raise
    if p.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        return None
    d = index(shoot)
    d[out.name] = {"t": when, "photos": [str(x) for x in photos], "frames": len(seq), "seconds": round(len(seq) / FPS, 2)}
    _save_index(shoot, d)
    return out


def today(shoot: Path, day: str | None = None) -> list[dict]:
    """Today's moments in time order: [{clip, t, photo}] (the photo: the camera's JPEG in the shoot)."""
    day = day or time.strftime("%Y%m%d")
    out = []
    for name, v in index(shoot).items():
        p = folder(shoot) / "clips" / name
        if name.startswith(day) and p.exists():
            jpg = next((x for x in v.get("photos", []) if x.lower().endswith((".jpg", ".jpeg")) and Path(x).exists()), None)
            out.append({"clip": p, "t": v.get("t", 0), "photo": Path(jpg) if jpg else None})
    return sorted(out, key=lambda m: m["t"])


def size_today(shoot: Path) -> int:
    return sum(m["clip"].stat().st_size for m in today(shoot))


FIT = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
       f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS},format=yuv420p")


def make(shoot: Path, stop: threading.Event | None = None, progress=lambda i, n: None, day: str | None = None) -> Path:
    """Today's reel: clip, photo, clip, photo ... hard cuts, 1280x720, 25 fps, no sound. Reads only."""
    ms = today(shoot, day)
    if not ms:
        raise ValueError("No moments yet today. Switch the day reel on, then take photos with Studio.")
    ff = video.ffmpeg()
    work = folder(shoot) / "work"
    work.mkdir(parents=True, exist_ok=True)
    parts = []
    try:
        seq = []
        for m in ms:
            seq.append(("clip", m["clip"]))
            if m["photo"] is not None:
                seq.append(("photo", m["photo"]))
        for i, (kind, src) in enumerate(seq):
            if stop is not None and stop.is_set():
                raise InterruptedError
            progress(i, len(seq))
            part = work / f"part{i:04d}.mp4"
            src_args = ["-i", str(src)] if kind == "clip" else ["-loop", "1", "-t", str(PHOTO_S), "-i", str(src)]
            r = subprocess.run([ff, "-hide_banner", "-v", "error", "-y", *src_args, "-vf", FIT, "-an",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-r", str(FPS), str(part)],
                               capture_output=True, timeout=600, creationflags=_creation())
            if r.returncode != 0 or not part.exists():
                raise RuntimeError("ffmpeg")
            parts.append(part)
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        name = f"Day reel {time.strftime('%Y-%m-%d') if not day else day[:4] + '-' + day[4:6] + '-' + day[6:8]}.mp4"
        out = folder(shoot) / name
        tmp = folder(shoot) / (name + ".part.mp4")
        r = subprocess.run([ff, "-hide_banner", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", "-movflags", "+faststart", str(tmp)],
                           capture_output=True, timeout=600, creationflags=_creation(), cwd=str(work))
        if r.returncode != 0 or not tmp.exists():
            raise RuntimeError("ffmpeg")
        os.replace(tmp, out)
        progress(len(seq), len(seq))
        return out
    finally:
        for p in parts:
            p.unlink(missing_ok=True)
        for leftover in ("list.txt",):
            (work / leftover).unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass


def delete_today(shoot: Path, day: str | None = None) -> int:
    """Removes today's CLIPS only (never photos, never the reel), and their index entries."""
    ms = today(shoot, day)
    d = index(shoot)
    n = 0
    for m in ms:
        m["clip"].unlink(missing_ok=True)
        d.pop(m["clip"].name, None)
        n += 1
    _save_index(shoot, d)
    return n
