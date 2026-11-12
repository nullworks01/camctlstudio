"""Colour grades ("looks") for camctl Studio.

One grade is a small set of parameters. The same maths runs in Python (here,
for graded copies of downloaded photos, via a 3D LUT) and in the browser (for
the live-view preview), so what you see live is what the photo gets.

Parameters (all optional):
  temp      -1..1   cool (blue) .. warm (orange)
  tint      -1..1   green .. magenta
  sat       0..2    saturation (0 = black & white)
  contrast  0..2    S-curve strength around mid-grey (1 = unchanged)
  lift      0..0.3  raise blacks (faded / matte)
  crush     0..0.3  crush blacks
  gain      0.7..1.3 overall brightness of highlights
  shadows   [r,g,b] -1..1 colour pushed into shadows   (split toning)
  highlights[r,g,b] -1..1 colour pushed into highlights
  split     0..0.5  strength of split toning
  mono      bool    black & white
  grain     0..1    film grain (applied after the LUT)
"""
from __future__ import annotations

import io

GRADE_KEYS = ("temp", "tint", "sat", "contrast", "lift", "crush", "gain",
              "shadows", "highlights", "split", "mono", "grain")


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def grade_pixel(r: float, g: float, b: float, p: dict, strength: float = 1.0) -> tuple[float, float, float]:
    """Grade one pixel (0..1 floats). Keep in sync with gradePixel() in the Studio page."""
    r0, g0, b0 = r, g, b
    t, ti = p.get("temp", 0.0), p.get("tint", 0.0)
    r *= 1 + 0.12 * t
    b *= 1 - 0.12 * t
    g *= 1 - 0.06 * ti
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    sat = 0.0 if p.get("mono") else p.get("sat", 1.0)
    r, g, b = (lum + (c - lum) * sat for c in (r, g, b))
    k = p.get("contrast", 1.0)
    if k != 1.0:
        r, g, b = (_clamp(0.5 + (c - 0.5) * k) for c in (r, g, b))
    split = p.get("split", 0.0)
    if split:
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        ws, wh = (1 - lum) ** 2, lum ** 2
        sh, hi = p.get("shadows", (0, 0, 0)), p.get("highlights", (0, 0, 0))
        r += split * (ws * sh[0] + wh * hi[0])
        g += split * (ws * sh[1] + wh * hi[1])
        b += split * (ws * sh[2] + wh * hi[2])
    crush = p.get("crush", 0.0)
    if crush:
        r, g, b = (max(0.0, (c - crush) / (1 - crush)) for c in (r, g, b))
    lift = p.get("lift", 0.0)
    if lift:
        r, g, b = (lift + c * (1 - lift) for c in (r, g, b))
    gain = p.get("gain", 1.0)
    r, g, b = (_clamp(c * gain) for c in (r, g, b))
    if strength < 1:
        r = r0 + (r - r0) * strength
        g = g0 + (g - g0) * strength
        b = b0 + (b - b0) * strength
    return _clamp(r), _clamp(g), _clamp(b)


# ---------------------------------------------------------------- S-Log monitoring
# Sony S-Log3 (published formula) and S-Gamut3(.Cine) -> Rec.709 primaries
# (linear). Keep in sync with logToRec709() in the Studio page.
GAMUT_TO_709 = {
    "sgamut3cine": ((1.6269474097, -0.5401385389, -0.0868088708),
                    (-0.1785155271, 1.4179409275, -0.2394254004),
                    (-0.0444361150, -0.1959199662, 1.2403560812)),
    "sgamut3": ((1.8467789693, -0.5259861230, -0.3207928463),
                (-0.4441532629, 1.2594429028, 0.1847103601),
                (0.0408554212, 0.0156408361, 0.9435037427)),
}


def slog3_to_linear(x: float) -> float:
    if x >= 171.2102946929 / 1023.0:
        return (10.0 ** ((x * 1023.0 - 420.0) / 261.5)) * (0.18 + 0.01) - 0.01
    return (x * 1023.0 - 95.0) * 0.01125000 / (171.2102946929 - 95.0)


SHOULDER_START = 0.4          # scene-linear: up to here 1:1 (18 % grey = 0.18 -> 41 IRE)


def _shoulder(lin: float) -> float:
    """Soft highlight roll-off: smooth (same slope at the start), approaches 1.0, about +4.5 stops of
    highlight room above grey before white, like a camera's own 709 view LUT."""
    if lin <= SHOULDER_START:
        return lin
    x = lin - SHOULDER_START
    return SHOULDER_START + x / (1.0 + x / (1.0 - SHOULDER_START))


def _oetf709(lin: float) -> float:
    lin = min(max(lin, 0.0), 1.0)
    return 4.5 * lin if lin < 0.018 else 1.099 * lin ** 0.45 - 0.099


def _rec709_display(lin: float) -> float:
    return _oetf709(_shoulder(max(lin, 0.0)))


def log_to_rec709(r: float, g: float, b: float, gamut: str = "sgamut3cine") -> tuple[float, float, float]:
    """S-Log3 code values (0..1) -> Rec.709 display values (0..1).
    The roll-off is applied to the brightest channel and the colour kept (ratio-preserving), so warm skin
    highlights do not turn flat yellow-white when one channel would clip first."""
    lr, lg, lb = slog3_to_linear(r), slog3_to_linear(g), slog3_to_linear(b)
    m = GAMUT_TO_709[gamut]
    lin = [max(row[0] * lr + row[1] * lg + row[2] * lb, 0.0) for row in m]
    peak = max(lin)
    k = _shoulder(peak) / peak if peak > SHOULDER_START else 1.0
    o = [_clamp(_oetf709(c * k)) for c in lin]
    return o[0], o[1], o[2]


def apply_to_jpeg(jpeg: bytes, params: dict, strength: float = 1.0, quality: int = 95) -> bytes:
    """Return a graded copy of a JPEG (the original is untouched)."""
    from PIL import Image, ImageFilter
    im = Image.open(io.BytesIO(jpeg))
    exif = im.info.get("exif")
    im = im.convert("RGB")
    lut = ImageFilter.Color3DLUT.generate(33, lambda r, g, b: grade_pixel(r, g, b, params, strength))
    out = im.filter(lut)
    grain = params.get("grain", 0.0) * strength
    if grain:
        out = _add_grain(out, grain)
    buf = io.BytesIO()
    kw = {"quality": quality}
    if exif:
        kw["exif"] = exif
    out.save(buf, "JPEG", **kw)
    return buf.getvalue()


def _add_grain(im, amount: float):
    """Monochrome film grain: overlay-blend mid-grey noise onto the image."""
    from PIL import Image, ImageChops
    w, h = im.size
    noise = Image.effect_noise((max(1, w // 2), max(1, h // 2)), 64).resize((w, h)).convert("RGB")
    return Image.blend(im, ImageChops.overlay(im, noise), min(0.5, 0.5 * amount))
