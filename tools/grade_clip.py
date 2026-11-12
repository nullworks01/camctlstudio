"""Grade a ZV-E10 S-Log3.Cine clip to a finished 1080p Rec.709 film look.

    python tools/grade_clip.py [clip.mp4] [--ev 0.7] [--look 0.5]

Without a path it finds the newest clip on a connected camera card
(PRIVATE\\M4ROOT\\CLIP\\*.MP4 on any drive) and COPIES it (the card is never changed).
Writes into captures\\video\\:  <name>_graded.mp4, <name>_before_after.mp4 and QA stills.

Pipeline (16-bit RGB): exposure correction in log -> light denoise -> camctl technical
LUT S-Log3.Cine->Rec.709 (soft highlight roll-off) -> moderate teal/orange look LUT
-> subtle vignette -> H.264 1080p, tagged BT.709.
"""
from __future__ import annotations

import argparse
import shutil
import string
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from camctl import lut  # noqa: E402

OUT = ROOT / "captures" / "video"
LOOK = {"temp": 0.05, "sat": 1.06, "contrast": 1.1, "lift": 0.015, "split": 0.22,
        "shadows": [-0.2, 0.03, 0.22], "highlights": [0.22, 0.1, -0.1]}
SLOG3_STOP = 261.5 / 1023 * 0.30103        # S-Log3 code change per stop (0..1)


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def find_clip() -> Path | None:
    clips = []
    for d in string.ascii_uppercase:
        p = Path(f"{d}:/PRIVATE/M4ROOT/CLIP")
        if p.is_dir():
            clips += [c for c in p.glob("*.MP4")] + [c for c in p.glob("*.mp4")]
    return max(clips, key=lambda c: c.stat().st_mtime) if clips else None


def blend(params: dict, k: float) -> dict:
    """The look at strength k (0..1) relative to neutral."""
    neutral = {"temp": 0, "tint": 0, "sat": 1, "contrast": 1, "lift": 0, "split": 0, "gain": 1, "crush": 0}
    out = {}
    for key, v in params.items():
        if isinstance(v, list):
            out[key] = v
        else:
            n = neutral.get(key, 0)
            out[key] = n + (v - n) * k
    return out


def fpath(p: Path) -> str:
    """Path for an ffmpeg filter argument (escape the drive colon)."""
    return str(p).replace("\\", "/").replace(":", "\\:")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("clip", nargs="?")
    ap.add_argument("--ev", type=float, default=0.7, help="exposure correction in stops")
    ap.add_argument("--look", type=float, default=1.0, help="look strength 0..1")
    ap.add_argument("--seconds", type=float, default=0, help="only the first N seconds (test)")
    a = ap.parse_args()

    src = Path(a.clip) if a.clip else find_clip()
    if src is None or not src.exists():
        sys.exit("No clip found. Put the camera card in the laptop or pass the file path.")
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / "raw" / src.name
    raw.parent.mkdir(exist_ok=True)
    if not raw.exists() or raw.stat().st_size != src.stat().st_size:
        shutil.copy2(src, raw)                # copy only; never touch the card
    luts = OUT / "luts"
    luts.mkdir(exist_ok=True)
    tech, look = luts / "slog3cine_to_rec709.cube", luts / "camctl_teal_orange.cube"
    tech.write_text(lut.export_cube(None, "camctl S-Log3.Cine to Rec709", size=65, source="slog3cine"))
    look.write_text(lut.export_cube(blend(LOOK, a.look), "camctl teal orange", size=65))

    stem = raw.stem
    graded, split = OUT / f"{stem}_graded.mp4", OUT / f"{stem}_before_after.mp4"
    lift = int(round(a.ev * SLOG3_STOP * 65535))
    # in_range=full: keep the camera's S-Log3 code values as they are (no legal->full stretch)
    pre = ("scale=1920:1080:flags=lanczos:in_range=full:out_range=full:in_color_matrix=bt709,format=gbrp16le")
    grade = (f"lutrgb=r=val+{lift}:g=val+{lift}:b=val+{lift},hqdn3d=2.5:2:5:4,"
             f"lut3d=file='{fpath(tech)}':interp=tetrahedral,lut3d=file='{fpath(look)}':interp=tetrahedral,"
             "vignette=angle=0.42:mode=forward")
    post = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
    enc = ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-colorspace", "bt709", "-color_primaries", "bt709",
           "-color_trc", "bt709", "-color_range", "tv", "-movflags", "+faststart"]
    t = ["-t", str(a.seconds)] if a.seconds else []
    ff = ffmpeg()
    run = lambda args: subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)

    run(["-i", str(raw), *t, "-vf", f"{pre},{grade},{post}", *enc, "-c:a", "aac", "-b:a", "192k", str(graded)])
    font = "C\\:/Windows/Fonts/segoeuib.ttf"
    label = lambda txt, x: (f"drawtext=fontfile='{font}':text='{txt}':x={x}:y=40:fontsize=34:fontcolor=white:"
                            "box=1:boxcolor=black@0.45:boxborderw=12")
    fc = (f"[0:v]{pre},split[a][b];[a]crop=960:1080:0:0,{label('S-LOG3 (CAMERA)', 40)}[l];"
          f"[b]{grade},crop=960:1080:960:0,{label('GRADED', 'w-tw-40')}[r];"
          f"[l][r]hstack,drawbox=x=958:y=0:w=4:h=ih:color=white@0.9:t=fill,{post}[v]")
    run(["-i", str(raw), *t, "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", *enc, "-c:a", "aac", str(split)])
    for name, f in (("graded", graded), ("before_after", split)):
        run(["-ss", "3", "-i", str(f), "-frames:v", "1", str(OUT / f"{stem}_{name}_still.jpg")])
    print(f"graded: {graded}\nbefore/after: {split}\nstills: {OUT / (stem + '_*_still.jpg')}")


if __name__ == "__main__":
    main()
