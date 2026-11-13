"""Looks (More › Looks): six film-style looks, by his press only, on a COPY of a finished photo or clip, or as a
clearly marked preview on the live picture (off at every start). Never by itself, never proposed by Studio or Claude.

Made by our own maths here (no third-party table): a smooth monotonic tone curve per look, a colour response
(saturation with skin hues protected, gentle tints only where a look is meant to be warm or cool), written out as a
33-point 3D table. Strength 0 is the untouched picture. Originals and the card are never changed.
"""
from __future__ import annotations

import io
import os
import subprocess
import threading
import time
from pathlib import Path



class _LazyNumpy:
    """numpy on first use: Studio must start even where numpy lives in a user folder the simulator hides."""

    def __getattr__(self, name):
        import numpy
        return getattr(numpy, name)


np = _LazyNumpy()

N = 33

# key, plain name, what it is for, curve points (in -> out), saturation, skin saturation, tint (shadows, highlights),
# black and white?  Tints are small RGB offsets; looks without tint keep greys neutral.
LOOKS = [
    {"key": "documentary", "name": "Quiet documentary", "for": "Muted, honest colour for interviews and real life.",
     "curve": [(0, .035), (.18, .175), (.5, .5), (.82, .79), (1, .955)], "sat": .7, "skin": .88, "tint": None, "bw": False},
    {"key": "warmprint", "name": "Warm print", "for": "Soft, warm skin and creamy highlights, like a printed photo.",
     "curve": [(0, .025), (.2, .2), (.5, .52), (.8, .82), (1, .97)], "sat": .88, "skin": .96,
     "tint": ((.012, .002, -.008), (.026, .006, -.022)), "bw": False},
    {"key": "softneg", "name": "Soft negative", "for": "Low contrast and gentle colour for calm, airy scenes.",
     "curve": [(0, .055), (.2, .225), (.5, .5), (.8, .76), (1, .93)], "sat": .6, "skin": .82, "tint": None, "bw": False},
    {"key": "slide", "name": "Bright slide", "for": "Rich colour and firm contrast for products and sunny days.",
     "curve": [(0, 0), (.18, .13), (.5, .51), (.82, .88), (1, 1)], "sat": 1.25, "skin": 1.04, "tint": None, "bw": False},
    {"key": "coolclean", "name": "Clean cool", "for": "Crisp, slightly cool light for tech, studio and modern spaces.",
     "curve": [(0, .015), (.2, .18), (.5, .5), (.8, .83), (1, .99)], "sat": .9, "skin": .97,
     "tint": ((-.010, .002, .016), (-.016, -.002, .022)), "bw": False},
    {"key": "bw", "name": "Black and white", "for": "Classic black and white with deep shadows and soft highlights.",
     "curve": [(0, .02), (.2, .15), (.5, .5), (.8, .85), (1, .98)], "sat": 0.0, "skin": 0.0, "tint": None, "bw": True},
]
BY_KEY = {lk["key"]: lk for lk in LOOKS}


def _pchip(points):
    """A monotonic cubic through the points (Fritsch-Carlson): smooth, never overshoots, never goes backwards."""
    x = np.array([p[0] for p in points], float)
    y = np.array([p[1] for p in points], float)
    h = np.diff(x)
    d = np.diff(y) / h
    m = np.zeros_like(y)
    m[0], m[-1] = d[0], d[-1]
    for k in range(1, len(y) - 1):
        if d[k - 1] * d[k] <= 0:
            m[k] = 0
        else:
            w1, w2 = 2 * h[k] + h[k - 1], h[k] + 2 * h[k - 1]
            m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k])

    def f(t):
        t = np.clip(np.asarray(t, float), 0, 1)
        k = np.clip(np.searchsorted(x, t, side="right") - 1, 0, len(h) - 1)
        s = (t - x[k]) / h[k]
        h00, h10, h01, h11 = 2 * s**3 - 3 * s**2 + 1, s**3 - 2 * s**2 + s, -2 * s**3 + 3 * s**2, s**3 - s**2
        return h00 * y[k] + h10 * h[k] * m[k] + h01 * y[k + 1] + h11 * h[k] * m[k + 1]
    return f


def _hue(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return np.degrees(np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)) % 360


def transform(rgb: np.ndarray, key: str) -> np.ndarray:
    """The look on display-referred RGB in 0..1 (any shape ending in 3)."""
    lk = BY_KEY[key]
    rgb = np.clip(rgb.astype(float), 0, 1)
    curve = _pchip(lk["curve"])
    y = rgb @ np.array([0.2126, 0.7152, 0.0722])
    if lk["bw"]:
        mono = rgb @ np.array([0.30, 0.59, 0.11])                 # a gentle red-filter mix
        out = np.repeat(curve(mono)[..., None], 3, axis=-1)
        return np.clip(out, 0, 1)
    yc = curve(y)
    chroma = rgb - y[..., None]
    hue = _hue(rgb)
    skin = np.exp(-(((hue - 28 + 180) % 360 - 180) / 22) ** 2)    # skin hues (orange around 28 degrees) kept close
    sat = lk["sat"] * (1 - skin) + lk["skin"] * skin
    # colour rides on the new tone: keep the chroma/luma ratio so dark colours do not go grey or neon
    scale = np.where(y > 1e-4, yc / np.maximum(y, 1e-4), 1.0)
    scale = np.clip(scale, 0.6, 1.6)
    out = yc[..., None] + chroma * (sat * scale)[..., None]
    if lk["tint"]:
        (ts, th) = lk["tint"]
        w_sh = np.clip(1 - yc / 0.5, 0, 1)[..., None]
        w_hi = np.clip((yc - 0.5) / 0.5, 0, 1)[..., None]
        out = out + w_sh * np.array(ts) + w_hi * np.array(th)
    # soft gamut: pull overflowing colours back toward their own grey instead of clipping a channel
    over = np.maximum(out.max(axis=-1) - 1, 0) + np.maximum(-out.min(axis=-1), 0)
    k = np.clip(1 - over / np.maximum(np.abs(out - yc[..., None]).max(axis=-1), 1e-6), 0, 1)[..., None]
    out = yc[..., None] + (out - yc[..., None]) * k
    return np.clip(out, 0, 1)


def table(key: str, n: int = N) -> np.ndarray:
    """The 3D table [b][g][r] -> rgb (the .cube order), n points per side."""
    g = np.linspace(0, 1, n)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")
    return transform(np.stack([r, gg, b], axis=-1), key)


def cube_text(key: str, n: int = N) -> str:
    t = table(key, n).reshape(-1, 3)
    head = f'TITLE "camctl {BY_KEY[key]["name"]}"\nLUT_3D_SIZE {n}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n'
    return head + "\n".join(f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in t) + "\n"


def lut_bytes(key: str) -> bytes:
    """The table as 33^3 x 3 bytes, r fastest (for the live preview in the page)."""
    return (np.round(table(key) * 255)).astype(np.uint8).tobytes()


def apply_table(img: np.ndarray, tab: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """Trilinear lookup of uint8 RGB through the table, blended with the original by strength (0 = identical)."""
    if strength <= 0:
        return img.copy()
    n = tab.shape[0]
    out = np.empty_like(img)
    rows = max(1, 2_000_000 // max(1, img.shape[1]))
    for y0 in range(0, img.shape[0], rows):
        x = img[y0:y0 + rows].astype(np.float32) / 255 * (n - 1)
        i0 = np.clip(np.floor(x).astype(int), 0, n - 2)
        f = x - i0
        r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
        fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
        c = lambda db, dg, dr: tab[b0 + db, g0 + dg, r0 + dr]
        v = (((c(0, 0, 0) * (1 - fr) + c(0, 0, 1) * fr) * (1 - fg) + (c(0, 1, 0) * (1 - fr) + c(0, 1, 1) * fr) * fg) * (1 - fb)
             + ((c(1, 0, 0) * (1 - fr) + c(1, 0, 1) * fr) * (1 - fg) + (c(1, 1, 0) * (1 - fr) + c(1, 1, 1) * fr) * fg) * fb)
        src = img[y0:y0 + rows].astype(np.float32) / 255
        mix = src + (v - src) * float(min(max(strength, 0), 1))
        out[y0:y0 + rows] = np.clip(np.round(mix * 255), 0, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------- pictures
def sample_picture(w: int = 480, h: int = 300) -> bytes:
    """A DRAWN sample (no photo of anyone): sky, a face-coloured patch, a grey ramp and colour chips."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h))
    px = np.zeros((h, w, 3), np.float32)
    yy = np.linspace(0, 1, h)[:, None]
    px[...] = np.stack([0.45 + 0.25 * yy, 0.62 + 0.2 * yy, 0.85 - 0.1 * yy], axis=-1) * np.ones((1, w, 1))
    im = Image.fromarray((px * 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    d.rectangle((0, int(h * .62), w, h), fill=(86, 104, 62))                     # grass
    d.ellipse((int(w * .36), int(h * .14), int(w * .6), int(h * .56)), fill=(214, 160, 128))   # a face-coloured disc
    d.ellipse((int(w * .4), int(h * .5), int(w * .56), int(h * .86)), fill=(60, 72, 110))      # a jacket
    for i in range(12):                                                            # grey ramp
        v = int(i * 255 / 11)
        d.rectangle((int(w * .03) + i * int(w * .025), int(h * .05), int(w * .03) + (i + 1) * int(w * .025), int(h * .16)), fill=(v, v, v))
    for i, col in enumerate([(200, 40, 40), (230, 160, 30), (230, 220, 60), (60, 160, 70), (40, 90, 200), (150, 60, 160)]):
        d.rectangle((int(w * .7) + i * int(w * .045), int(h * .68), int(w * .7) + (i + 1) * int(w * .045) - 2, int(h * .8)), fill=col)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=92)
    return b.getvalue()


def apply_jpeg(jpeg: bytes, key: str, strength: float = 1.0, quality: int = 95) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg))
    exif = im.info.get("exif")
    arr = apply_table(np.asarray(im.convert("RGB")), table(key), strength)
    out = Image.fromarray(arr)
    b = io.BytesIO()
    kw = {"quality": quality, "subsampling": 0}
    if exif:
        kw["exif"] = exif
    out.save(b, "JPEG", **kw)
    return b.getvalue()


def copy_dir(shoot: Path, key: str) -> Path:
    return Path(shoot) / "LOOKS" / BY_KEY[key]["name"]


def copy_photo(src: Path, shoot: Path, key: str, strength: float = 1.0) -> Path:
    """A new JPEG with the look in <shoot>\\LOOKS\\<look>\\, named after the original. The original is only read."""
    src = Path(src)
    if src.suffix.lower() not in (".jpg", ".jpeg"):
        raise ValueError("Looks work on the camera's JPEG (the RAW file is left as it is).")
    out_dir = copy_dir(shoot, key)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem} - {BY_KEY[key]['name']}.jpg"
    data = apply_jpeg(src.read_bytes(), key, strength)
    tmp = out.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return out


def copy_clip(src: Path, shoot: Path, key: str, strength: float = 1.0, log: bool = False,
              stop: threading.Event | None = None) -> Path:
    """A new MP4 with the look (sound copied unchanged); a log clip is turned into a normal picture first."""
    from . import lut, video
    src = Path(src)
    out_dir = copy_dir(shoot, key)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem} - {BY_KEY[key]['name']}.mp4"
    work = out_dir / ".work"
    work.mkdir(exist_ok=True)
    look = work / f"{key}.cube"
    if strength >= 0.999:
        look.write_text(cube_text(key), encoding="utf-8")
    else:                                                  # the blend is baked into the table
        g = np.linspace(0, 1, N)
        b, gg, r = np.meshgrid(g, g, g, indexing="ij")
        ident = np.stack([r, gg, b], axis=-1)
        t = ident + (table(key) - ident) * strength
        look.write_text(f"LUT_3D_SIZE {N}\n" + "\n".join(f"{x:.6f} {y:.6f} {z:.6f}" for x, y, z in t.reshape(-1, 3)) + "\n",
                        encoding="utf-8")
    chain = []
    if log:
        conv = work / "log709.cube"
        conv.write_text(lut.export_cube(None, "camctl S-Log3 to Rec709", size=33, source="slog3cine"), encoding="utf-8")
        chain.append("lut3d=log709.cube")
    chain.append(f"lut3d={look.name}")
    tmp = out_dir / (out.stem + ".part.mp4")
    p = subprocess.Popen([video.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-i", str(src), "-vf", ",".join(chain),
                          "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
                          "-c:a", "copy", "-movflags", "+faststart", str(tmp)],
                         cwd=str(work), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        while p.poll() is None:
            if stop is not None and stop.is_set():
                p.kill()
                raise InterruptedError
            time.sleep(0.2)
        if p.returncode != 0 or not tmp.exists():
            raise RuntimeError("ffmpeg")
        os.replace(tmp, out)
        return out
    finally:
        tmp.unlink(missing_ok=True)
        for f in work.glob("*"):
            f.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass
