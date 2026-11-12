"""One-button video: find/import the clip, grade it, cut silences, (subtitle), export 16:9 + 9:16.

The camera records to its SD card; in PC Remote mode the card can't be read over USB/Wi-Fi.
So a clip is picked up from wherever it becomes reachable: the card in a reader or the
camera in USB Mass Storage mode (PRIVATE\\M4ROOT\\CLIP\\*.MP4), or an HDMI capture
recording made on the PC. Files are only COPIED; nothing on the card is changed.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import string
import subprocess
import threading
import time
from pathlib import Path

from . import lut

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "captures" / "video"
SLOG3_STOP = 261.5 / 1023 * 0.30103
DEFAULT_LOOK = {"temp": 0.05, "sat": 1.06, "contrast": 1.1, "lift": 0.015, "split": 0.22,
                "shadows": [-0.2, 0.03, 0.22], "highlights": [0.22, 0.1, -0.1]}


FFMPEG_MISSING = ("The video tool ffmpeg was not found on this laptop. Cutting pauses and the quick draft of a clip "
                  "don't work until it is installed.")
_FF: dict = {}


def _candidates() -> list[str]:
    """ffmpeg on PATH (also the PATH saved in Windows, so an install after Studio started counts), then the usual
    install folders, then the imageio-ffmpeg package if it happens to be installed."""
    import glob
    out = []
    exe = shutil.which("ffmpeg")
    if exe:
        out.append(exe)
    if os.name == "nt":
        try:
            import winreg
            for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(root, key) as k:
                        for d in os.path.expandvars(str(winreg.QueryValueEx(k, "Path")[0])).split(";"):
                            if d.strip():
                                out.append(str(Path(d.strip()) / "ffmpeg.exe"))
                except OSError:
                    pass
        except ImportError:
            pass
        la, pf = os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", r"C:\Program Files")
        out += [r"C:\ffmpeg\bin\ffmpeg.exe", str(Path(pf) / "ffmpeg" / "bin" / "ffmpeg.exe"),
                r"C:\ProgramData\chocolatey\bin\ffmpeg.exe", str(Path.home() / "scoop" / "shims" / "ffmpeg.exe"),
                str(Path(la) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe")]
        out += sorted(glob.glob(str(Path(la) / "Microsoft" / "WinGet" / "Packages" / "*FFmpeg*" / "*" / "bin" / "ffmpeg.exe")))
    try:
        import imageio_ffmpeg
        out.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 - optional fallback only
        pass
    seen, uniq = set(), []
    for c in out:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)
    return uniq


def find_ffmpeg() -> str | None:
    """The first ffmpeg that exists and runs ('-version'), or None. Checked once per run of Studio."""
    if "exe" in _FF:
        return _FF["exe"]
    found = None
    for c in _candidates():
        if not Path(c).is_file():
            continue
        try:
            r = subprocess.run([c, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and "ffmpeg" in (r.stdout or "").lower():
                found = c
                break
        except (OSError, subprocess.SubprocessError):
            continue
    _FF["exe"] = found
    return found


def ffmpeg() -> str:
    exe = find_ffmpeg()
    if not exe:
        raise RuntimeError(FFMPEG_MISSING)
    return exe


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg(), "-hide_banner", "-y", *args], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kw)


# ---------------------------------------------------------------- finding clips
def card_clips(since: float = 0.0) -> list[Path]:
    clips = []
    for d in string.ascii_uppercase:
        p = Path(f"{d}:/PRIVATE/M4ROOT/CLIP")
        try:
            if p.is_dir():
                clips += [c for c in p.iterdir() if c.suffix.lower() == ".mp4" and c.stat().st_mtime >= since - 120]
        except OSError:
            continue
    return sorted(clips, key=lambda c: c.stat().st_mtime, reverse=True)


def import_clip(src: Path) -> Path:
    raw = OUT / "raw" / src.name
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.exists() or raw.stat().st_size != src.stat().st_size:
        shutil.copy2(src, raw)                       # copy only
    return raw


# ---------------------------------------------------------------- HDMI capture (optional)
def capture_devices() -> list[str]:
    """DirectShow video devices whose name looks like an HDMI capture (not the laptop webcam)."""
    try:
        r = _run(["-list_devices", "true", "-f", "dshow", "-i", "dummy"], timeout=10)
    except Exception:  # noqa: BLE001
        return []
    names = re.findall(r'"([^"]+)"\s*\(video\)', r.stderr)
    return [n for n in names if re.search(r"cam ?link|capture|hdmi|usb video|elgato|ugreen|av\.io|magewell", n, re.I)]


class HdmiRecorder:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.file: Path | None = None

    def start(self, device: str) -> Path:
        OUT.joinpath("raw").mkdir(parents=True, exist_ok=True)
        self.file = OUT / "raw" / time.strftime("HDMI_%Y%m%d-%H%M%S.mp4")
        self.proc = subprocess.Popen(
            [ffmpeg(), "-hide_banner", "-y", "-f", "dshow", "-rtbufsize", "512M", "-i", f"video={device}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(self.file)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return self.file

    def stop(self) -> Path | None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.communicate(b"q", timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None
        return self.file


# ---------------------------------------------------------------- editing
def probe(path: Path) -> dict:
    r = _run(["-i", str(path)], timeout=30)
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    size = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", r.stderr)
    return {"duration": (int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3])) if dur else 0.0,
            "width": int(size[1]) if size else 0, "height": int(size[2]) if size else 0,
            "audio": "Audio:" in r.stderr}


def speech_segments(path: Path, noise_db: int = -35, min_silence: float = 0.6, pad: float = 0.15) -> list[tuple[float, float]]:
    """Parts with sound (silences longer than min_silence removed), from ffmpeg silencedetect."""
    info = probe(path)
    if not info["audio"]:
        return [(0.0, info["duration"])]
    r = _run(["-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"], timeout=600)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    segs, t = [], 0.0
    for s, e in zip(starts, ends + [info["duration"]] * (len(starts) - len(ends))):
        if s - t > 0.05:
            segs.append((max(0.0, t - pad), min(info["duration"], s + pad)))
        t = e
    if info["duration"] - t > 0.05:
        segs.append((max(0.0, t - pad), info["duration"]))
    return segs or [(0.0, info["duration"])]


def _fpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:")


def make_luts(look: dict | None, log: bool) -> tuple[Path | None, Path]:
    d = OUT / "luts"
    d.mkdir(parents=True, exist_ok=True)
    tech = d / "slog3cine_to_rec709.cube"
    if log and not tech.exists():
        tech.write_text(lut.export_cube(None, "camctl S-Log3.Cine to Rec709", size=65, source="slog3cine"))
    name = "look_" + re.sub(r"[^a-z0-9]+", "", json.dumps(look or DEFAULT_LOOK, sort_keys=True))[:40] + ".cube"
    lk = d / name
    if not lk.exists():
        lk.write_text(lut.export_cube(look or DEFAULT_LOOK, "camctl look", size=65))
    return (tech if log else None), lk


def finish(raw: Path, look: dict | None = None, log: bool = True, ev: float = 0.0, cut: bool = True,
           subtitles: Path | None = None, face_x: float = 0.5, progress=lambda s: None) -> dict:
    """Grade + optional silence cut + optional burned subtitles; export 16:9 1080p and 9:16 1080x1920."""
    OUT.mkdir(parents=True, exist_ok=True)
    stem = raw.stem
    tech, lk = make_luts(look, log)
    segs = speech_segments(raw) if cut else [(0.0, probe(raw)["duration"])]
    progress(f"{len(segs)} speaking part(s)")
    sel = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in segs)
    pre = "scale=1920:1080:flags=lanczos:in_range=full:out_range=full:in_color_matrix=bt709,format=gbrp16le" if log \
        else "scale=1920:1080:flags=lanczos,format=gbrp16le"
    lift = int(round(ev * SLOG3_STOP * 65535)) if log else 0
    grade = (f"lutrgb=r=val+{lift}:g=val+{lift}:b=val+{lift}," if lift else "") + "hqdn3d=2:1.5:4:3," \
        + (f"lut3d=file='{_fpath(tech)}':interp=tetrahedral," if tech else "") \
        + f"lut3d=file='{_fpath(lk)}':interp=tetrahedral,vignette=angle=0.42:mode=forward"
    post = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
    vcut = f"select='{sel}',setpts=N/FRAME_RATE/TB," if cut else ""
    acut = f"aselect='{sel}',asetpts=N/SR/TB" if cut else "anull"
    subs = f",subtitles='{_fpath(subtitles)}':force_style='FontName=Segoe UI,FontSize=13,Outline=1,Shadow=0,MarginV=28'" if subtitles else ""
    enc = ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-colorspace", "bt709", "-color_primaries", "bt709",
           "-color_trc", "bt709", "-color_range", "tv", "-movflags", "+faststart", "-c:a", "aac", "-b:a", "192k"]
    wide = OUT / f"{stem}_16x9.mp4"
    has_audio = probe(raw)["audio"]
    progress("Grading 16:9…")
    fc = f"[0:v]{vcut}{pre},{grade},{post}{subs}[v]" + (f";[0:a]{acut}[a]" if has_audio else "")
    r = _run(["-i", str(raw), "-filter_complex", fc, "-map", "[v]", *(["-map", "[a]"] if has_audio else []), *enc, str(wide)],
             timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed (16:9): " + r.stderr.strip().splitlines()[-1] if r.stderr else "ffmpeg failed")
    progress("Making the 9:16 version…")
    tall = OUT / f"{stem}_9x16.mp4"
    cx = min(max(face_x, 0.28), 0.72)                       # keep the face in the vertical crop
    crop = f"crop=608:1080:{int(cx * 1920 - 304)}:0,scale=1080:1920:flags=lanczos"
    r = _run(["-i", str(wide), "-vf", crop, *enc, str(tall)], timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed (9:16)")
    thumb = OUT / f"{stem}_thumb.jpg"
    _run(["-ss", "1", "-i", str(wide), "-frames:v", "1", "-vf", "scale=640:-2", str(thumb)], timeout=60)
    return {"wide": wide.name, "tall": tall.name, "thumb": thumb.name, "parts": len(segs),
            "kept_s": round(sum(b - a for a, b in segs), 1), "duration_s": round(probe(raw)["duration"], 1)}


# ---------------------------------------------------------------- job runner
class VideoJobs:
    def __init__(self):
        self.status = {"state": "idle", "message": "", "result": None, "error": "", "clip": None, "since": 0.0}
        self.lock = threading.Lock()
        self.hdmi = HdmiRecorder()
        self._watch: threading.Thread | None = None

    def recording_started(self, device: str | None = None) -> None:
        self.status.update(state="recording", message="Recording…", result=None, error="", clip=None, since=time.time())
        if device:
            try:
                self.hdmi.start(device)
                self.status["message"] = "Recording (also on the PC via HDMI)…"
            except Exception as exc:  # noqa: BLE001
                self.status["message"] = f"Recording (HDMI capture failed: {exc})"

    def recording_stopped(self, opts: dict) -> None:
        f = self.hdmi.stop() if self.hdmi.proc else None
        if f and f.exists() and f.stat().st_size > 100_000:
            self._imported(f, opts)
            return
        self.status.update(state="waiting_clip",
                           message="The clip is on the camera's SD card. Put the card in the laptop "
                                   "(or set USB Connection to Mass Storage); I'll pick it up automatically.")
        if not getattr(self, "external_import", False):     # Studio's card importer does it otherwise
            self._watch = threading.Thread(target=self._wait_for_card, args=(opts,), daemon=True)
            self._watch.start()

    def _wait_for_card(self, opts: dict) -> None:
        since = self.status["since"]
        while self.status["state"] == "waiting_clip":
            clips = card_clips(since)
            if clips:
                self.status["message"] = f"Found {clips[0].name}, copying…"
                try:
                    from . import handoff
                    raw = handoff.add_file(clips[0], "VIDEO")        # copy only, straight into the shoot
                except OSError as exc:
                    self.status.update(state="error", error=f"Copy failed: {exc}")
                    return
                self._imported(raw, opts)
                return
            time.sleep(3)

    def _imported(self, raw: Path, opts: dict) -> None:
        """The clip is on the PC (in the shoot folder). Hand-off first; a graded draft only if asked."""
        self.status.update(state="imported", clip=raw.name, clip_path=str(raw),
                           message=f"{raw.name} is in your shoot folder (untouched S-Log3). Ready for Resolve.")
        if opts.get("quick_draft"):
            self._process(raw, opts)

    def cancel(self) -> None:
        self.status.update(state="idle", message="")

    def process_file(self, path: Path, opts: dict) -> None:
        threading.Thread(target=self._process, args=(path, opts), daemon=True).start()

    def _process(self, raw: Path, opts: dict) -> None:
        self.status.update(state="processing", clip=raw.name, message="Finishing your video…")
        try:
            res = finish(raw, look=opts.get("look"), log=bool(opts.get("log", True)), ev=float(opts.get("ev", 0)),
                         cut=bool(opts.get("cut", True)), face_x=float(opts.get("face_x", 0.5)),
                         progress=lambda m: self.status.update(message=m))
            self.status.update(state="done", message="Ready", result=res)
        except Exception as exc:  # noqa: BLE001
            self.status.update(state="error", error=str(exc))
