"""Sound report for imported clips: loudness, clipping, background noise, long pauses and "no sound", in plain words.

- Reads the COPY in the shoot folder only (never the card, never the original); nothing is written into the media.
- Uses the ffmpeg that comes with Studio (imageio-ffmpeg): ebur128 for loudness, silencedetect for pauses, and the
  decoded samples for clipping (with the times) and the room's noise floor.
- Results are kept beside the take data (shoot NOTES\\sound.json), once per clip. It never calls Claude.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import video

LONG_PAUSE_S = 2.0
CLIP_LEVEL = 32700          # of 32767 in 16-bit: a sample at the top is clipped
_lock = threading.Lock()


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _creation():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _clipping(path: Path, stop: threading.Event | None = None) -> tuple[list[float], float, float]:
    """(clip times in seconds, noise floor dBFS, loudest 100 ms block dBFS) from the first channel's samples."""
    import numpy as np
    p = subprocess.Popen([video.ffmpeg(), "-hide_banner", "-v", "error", "-i", str(path), "-vn", "-af", "pan=mono|c0=c0",
                          "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_creation())
    rate = 48000
    m = re.search(r"(\d{4,6}) Hz", video._run(["-i", str(path)], timeout=30).stderr or "")
    if m:
        rate = int(m.group(1))
    block = rate // 10
    times, levels, n0, last = [], [], 0, -9.0
    try:
        while True:
            if stop is not None and stop.is_set():
                raise InterruptedError
            raw = p.stdout.read(block * 2 * 50)               # five seconds at a time
            if not raw:
                break
            a = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.int32)
            hit = np.flatnonzero(np.abs(a) >= CLIP_LEVEL)
            for i in hit:
                t = (n0 + int(i)) / rate
                if t - last > 0.25 or t - times[-1] >= 1.0:   # one place per burst (and per second of a long one)
                    times.append(round(t, 2))
                last = t
            nb = len(a) // block
            if nb:
                rms = np.sqrt((a[: nb * block].reshape(nb, block).astype(np.float64) ** 2).mean(axis=1)) / 32768
                levels += [float(x) for x in 20 * np.log10(np.maximum(rms, 1e-9))]
            n0 += len(a)
    finally:
        if p.poll() is None:
            p.kill()
        p.stdout.close()
    live = [v for v in levels if v > -90]                      # digital silence is not "the room"
    noise = float(np.percentile(live, 10)) if live else -120.0
    if live and noise > float(np.percentile(live, 50)) - 6:
        noise = float("nan")                                   # no quiet moment at all: the room cannot be heard
    top = max(levels) if levels else -120.0
    return times, noise, top


def analyse(path: Path, stop: threading.Event | None = None) -> dict:
    """The sound report of one clip (plain words + small numbers)."""
    path = Path(path)
    info = video.probe(path)
    if not info["audio"]:
        return {"file": path.name, "sound": False, "lines": [{"key": "none", "word": "no sound track", "num": ""}],
                "summary": "No sound track in this clip."}
    r = video._run(["-i", str(path), "-vn", "-af",
                    f"ebur128=peak=sample,silencedetect=noise=-40dB:d={LONG_PAUSE_S}", "-f", "null", "-"], timeout=3600)
    err = r.stderr or ""
    i_lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    lufs = float(i_lufs[-1]) if i_lufs else None
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    dur = info["duration"] or 0.0
    pauses = []
    for k, s in enumerate(starts):
        e = ends[k] if k < len(ends) else dur
        if s > 0.5 and e < dur - 0.5:                          # not the quiet start and end of the clip
            pauses.append((round(max(s, 0), 1), round(e, 1)))
    clips, noise, top = _clipping(path, stop)
    silent = lufs is None or lufs <= -60 or top < -55
    lines = []
    if silent:
        lines.append({"key": "none", "word": "no sound", "num": "", "advice": "The microphone may be off or unplugged."})
        return {"file": path.name, "sound": False, "lines": lines, "lufs": lufs, "summary": "No sound in this clip.",
                "t": time.time()}
    loud = "quiet" if lufs < -23 else "loud" if lufs > -11 else "good for online video"
    lines.append({"key": "loudness", "word": loud, "num": f"{lufs:.0f} LUFS",
                  "advice": "Raise it in the edit, or move the microphone closer next time." if loud == "quiet" else
                  "Turn the microphone gain down a little next time." if loud == "loud" else ""})
    n = len(clips)
    cw = "none" if n == 0 else "a few peaks" if n <= 3 else f"clipped in {n} places"
    lines.append({"key": "clipping", "word": cw, "num": ", ".join(_mmss(t) for t in clips[:5]) + (" …" if n > 5 else ""),
                  "times": clips[:50], "advice": "" if n == 0 else "Lower the microphone gain or move it a little further away."})
    nw = ("can't tell (no quiet moment)" if noise != noise else
          "low" if noise < -60 else "noticeable" if noise < -48 else "high")
    lines.append({"key": "noise", "word": nw, "num": "" if noise != noise else f"{noise:.0f} dBFS",
                  "advice": "" if nw in ("low", "can't tell (no quiet moment)") else "A quieter room (no fan, window closed) or the microphone closer to you."})
    lines.append({"key": "pauses", "word": f"{len(pauses)} long pause{'s' if len(pauses) != 1 else ''}" if pauses else "none",
                  "num": f"over {LONG_PAUSE_S:g} s" if pauses else "", "pauses": pauses[:50],
                  "advice": "Cut them out in your editor if you like." if pauses else ""})
    bad = [ln for ln in lines if ln["advice"]]
    return {"file": path.name, "sound": True, "lines": lines, "lufs": lufs, "clips": n, "noise_dbfs": None if noise != noise else round(noise, 1),
            "pauses": len(pauses), "summary": "Sound: " + ", ".join(f"{ln['key']} {ln['word']}" for ln in lines),
            "ok": not bad, "t": time.time()}


# ---------------------------------------------------------------- once per clip, in the background
def _store(folder: Path) -> Path:
    return Path(folder) / "NOTES" / "sound.json"


def load(folder: Path) -> dict:
    try:
        return json.loads(_store(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(folder: Path, name: str, rep: dict) -> None:
    with _lock:
        d = load(folder)
        d[name] = rep
        f = _store(folder)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        tmp.replace(f)


class SoundJobs:
    """One clip after the other, in one quiet background thread; can be cancelled; never blocks the page."""

    def __init__(self):
        self.queue: list[tuple[Path, Path]] = []
        self.status = {"busy": False, "current": None, "done": 0, "left": 0, "message": ""}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def add(self, clip: Path, shoot_folder: Path, again: bool = False) -> None:
        clip, shoot_folder = Path(clip), Path(shoot_folder)
        if clip.name in load(shoot_folder) and not again:
            return                                            # once per clip (again: his press on the take)
        with self._lock:
            if any(c.name == clip.name for c, _ in self.queue) or self.status["current"] == clip.name:
                return
            self.queue.append((clip, shoot_folder))
            self.status["left"] = len(self.queue)
        if not (self.thread and self.thread.is_alive()):
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, daemon=True, name="sound-report")
            self.thread.start()

    def cancel(self) -> None:
        self._stop.set()
        with self._lock:
            self.queue.clear()
        self.status.update(left=0, message="Sound report stopped. You can run it again from the take.")

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if not self.queue:
                    break
                clip, folder = self.queue.pop(0)
                self.status.update(busy=True, current=clip.name, left=len(self.queue),
                                   message=f"Listening to {clip.name}…")
            try:
                rep = analyse(clip, self._stop)
                save(folder, clip.name, rep)
                self.status["done"] += 1
                self.status["message"] = f"{clip.name}: {rep['summary']}"
            except InterruptedError:
                break
            except Exception:  # noqa: BLE001 - a report that fails says so calmly, the clip is fine
                save(folder, clip.name, {"file": clip.name, "sound": None, "lines": [], "t": time.time(),
                                         "summary": "Studio couldn't read the sound of this clip. The clip itself is fine."})
        self.status.update(busy=False, current=None, left=len(self.queue))
