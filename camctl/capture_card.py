"""Optional HDMI capture card (e.g. a Cam Link) as the live picture and as a recorder on the laptop.
NOT YET TESTED ON A REAL CAPTURE CARD.

- One ffmpeg process owns the device (one consumer per device): it sends MJPEG frames for the live picture (Studio's
  frame pump takes them as they are, at the card's own size), RMS levels of the card's sound every 50 ms, and, while
  HE records, the same picture and sound into a Matroska file (playable after a crash), turned into MP4 on stop.
- Listing devices opens nothing. A device is opened only after he picks it by name. Device names are never logged.
- Camera control stays on USB / Wi-Fi; recording here never touches the camera's own recording.
FAKE (tests only): ffmpeg's own test pattern and tone instead of a device; never a real webcam.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import video

FAKE = False                          # tests: use ffmpeg's test pattern ("Test pattern") instead of DirectShow
FAKE_SIZE = "1280x720"
STALL_S = 3.0                         # no frame this long: the card stopped sending
MIN_FREE_START = 2_000_000_000        # bytes free to start a recording on the laptop
MIN_FREE_KEEP = 1_000_000_000         # below this while recording: a calm stop


def _flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def list_devices() -> dict:
    """{"video": [names], "audio": [names], "ffmpeg": bool}. Asks the system for names only; opens nothing."""
    if FAKE:
        return {"video": ["Test pattern"], "audio": ["Test tone"], "ffmpeg": True}
    ff = video.find_ffmpeg()
    if not ff:
        return {"video": [], "audio": [], "ffmpeg": False}
    try:
        r = subprocess.run([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, timeout=15, creationflags=_flags())
    except Exception:  # noqa: BLE001
        return {"video": [], "audio": [], "ffmpeg": True}
    return {**parse_devices(r.stderr or ""), "ffmpeg": True}


def parse_devices(text: str) -> dict:
    vids = re.findall(r'"([^"]+)"\s*\(video\)', text)
    auds = re.findall(r'"([^"]+)"\s*\(audio\)', text)
    if not vids and not auds:                                   # older ffmpeg: sections instead of tags
        sec = None
        vids, auds = [], []
        for line in text.splitlines():
            if "DirectShow video devices" in line:
                sec = vids
            elif "DirectShow audio devices" in line:
                sec = auds
            m = re.search(r'\]\s+"([^"]+)"\s*$', line)
            if m and sec is not None and "Alternative name" not in line:
                sec.append(m.group(1))
    return {"video": list(dict.fromkeys(vids)), "audio": list(dict.fromkeys(auds))}


class CardSource:
    """The capture card's picture and sound through ONE ffmpeg process; restarted (about a second) to record."""

    def __init__(self, video_name: str, audio_name: str | None = None):
        self.video_name, self.audio_name = video_name, audio_name
        self.proc: subprocess.Popen | None = None
        self.frame: bytes | None = None
        self.seq = 0
        self.frame_t = 0.0
        self.size: tuple[int, int] | None = None
        self.levels: list[tuple[float, float]] = []      # (time, RMS dBFS) of the card's sound, the last 20 s
        self.record_path: Path | None = None
        self.started = 0.0
        self.cond = threading.Condition()
        self.error = ""

    # -- the process ------------------------------------------------------------------------------------------
    def _inputs(self) -> list[str]:
        if FAKE:
            return ["-re", "-f", "lavfi", "-i", f"testsrc2=size={FAKE_SIZE}:rate=25",
                    "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        name = f"video={self.video_name}" + (f":audio={self.audio_name}" if self.audio_name else "")
        return ["-f", "dshow", "-rtbufsize", "256M", "-i", name]

    def _maps(self) -> tuple[str, str | None]:
        if FAKE:
            return "0:v", "1:a"
        return "0:v", ("0:a" if self.audio_name else None)

    def start(self, record_path: Path | None = None) -> None:
        self.stop()
        v, a = self._maps()
        args = [video.ffmpeg(), "-hide_banner", "-loglevel", "info", "-nostats", *self._inputs(),
                "-map", v, "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
        if a:
            args += ["-map", a, "-af", "asetnsamples=n=2400,astats=metadata=1:reset=1,"
                                       "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level", "-f", "null", "-"]
        if record_path is not None:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            args += ["-map", v] + (["-map", a] if a else []) + \
                    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"] + \
                    (["-c:a", "aac", "-b:a", "192k"] if a else []) + ["-f", "matroska", str(record_path)]
        self.record_path = record_path
        self.error = ""
        self.started = time.time()
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=_flags())
        threading.Thread(target=self._read_frames, args=(self.proc,), daemon=True, name="card-frames").start()
        threading.Thread(target=self._read_log, args=(self.proc,), daemon=True, name="card-log").start()

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.stdin.write(b"q")                           # ffmpeg finishes the file cleanly
                p.stdin.flush()
                p.wait(timeout=8)
            except Exception:  # noqa: BLE001
                p.kill()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stalled(self) -> bool:
        ref = self.frame_t or self.started
        return (not self.alive()) or (time.time() - ref > STALL_S)

    # -- readers ----------------------------------------------------------------------------------------------
    def _read_frames(self, p) -> None:
        buf = b""
        while True:
            chunk = p.stdout.read(65536) if p.stdout else b""
            if not chunk:
                break
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8")
                e = buf.find(b"\xff\xd9", s + 2)
                if s < 0 or e < 0:
                    if s > 0:
                        buf = buf[s:]
                    break
                jpg, buf = buf[s:e + 2], buf[e + 2:]
                with self.cond:
                    self.frame, self.frame_t = jpg, time.time()
                    self.seq += 1
                    self.cond.notify_all()
                if self.size is None:
                    try:
                        import io
                        from PIL import Image
                        self.size = Image.open(io.BytesIO(jpg)).size
                    except Exception:  # noqa: BLE001
                        pass

    def _read_log(self, p) -> None:
        for raw in iter(p.stderr.readline, b""):
            line = raw.decode("utf-8", "replace")
            m = re.search(r"RMS_level=(-?[\d.]+|-inf)", line)
            if m:
                v = -120.0 if m.group(1) == "-inf" else float(m.group(1))
                now = time.time()
                self.levels.append((now, v))
                if len(self.levels) > 500:
                    del self.levels[:-400]
            elif "Error" in line or "Could not" in line:
                self.error = "The capture card could not be opened."   # never the device name

    def next_frame(self, after: int, timeout: float = 1.0) -> tuple[bytes | None, int]:
        end = time.time() + timeout
        with self.cond:
            while self.seq <= after and time.time() < end:
                self.cond.wait(max(0.01, end - time.time()))
            return self.frame, self.seq

    def level(self) -> float | None:
        recent = [v for t, v in self.levels if time.time() - t < 0.5]
        return max(recent) if recent else None

    def levels_since(self, t0: float) -> list[float]:
        return [v for t, v in self.levels if t >= t0]

    def writing(self) -> bool:
        """The recording file is really being written (it has grown past its header)."""
        p = self.record_path
        try:
            return bool(p and p.exists() and p.stat().st_size > 64_000)
        except OSError:
            return False


def disk_free(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def finish_file(mkv: Path) -> tuple[Path | None, str]:
    """MKV -> MP4 (copied, not re-encoded), verified (it opens and has a duration); the MKV goes only then."""
    mp4 = mkv.with_suffix(".mp4")
    try:
        r = subprocess.run([video.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-i", str(mkv), "-c", "copy",
                            "-movflags", "+faststart", str(mp4)], capture_output=True, timeout=1800, creationflags=_flags())
        ok = r.returncode == 0 and mp4.exists() and video.probe(mp4)["duration"] > 0
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        mp4.unlink(missing_ok=True)
        return None, f"The recording is saved as {mkv.name} in the VIDEO folder of your shoot (it could not be turned into MP4)."
    mkv.unlink(missing_ok=True)
    return mp4, f"Saved {mp4.name} in the VIDEO folder of your shoot."
