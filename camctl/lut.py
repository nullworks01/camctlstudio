"""3D LUTs (.cube) for camctl Studio: export looks, import your own.

.cube is the format DaVinci Resolve, Premiere Pro and Final Cut Pro read.
Table order: red changes fastest, then green, then blue (also what Pillow's
Color3DLUT expects).
"""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

from .grade import grade_pixel

USER_LUT_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "camctl" / "luts"
MAX_SIZE = 65


INPUTS = {
    "rec709": "Rec.709 footage / photos",
    "slog3cine": "S-Log3 / S-Gamut3.Cine (PP8)",
    "slog3": "S-Log3 / S-Gamut3 (PP9)",
}


def export_cube(params: dict | None, title: str, size: int = 33, source: str = "rec709") -> str:
    """A .cube that applies the look. With source=slog3*/ it first converts log
    footage to Rec.709 (so one LUT takes camera log straight to the look).
    params=None gives a plain technical log -> Rec.709 LUT."""
    from .grade import log_to_rec709
    gamut = {"slog3cine": "sgamut3cine", "slog3": "sgamut3"}.get(source)
    out = io.StringIO()
    out.write(f'TITLE "{title}"\n# Made by camctl Studio. Input: {INPUTS.get(source, source)}\n'
              f"LUT_3D_SIZE {size}\nDOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
    n = size - 1
    for b in range(size):
        for g in range(size):
            for r in range(size):
                c = (r / n, g / n, b / n)
                if gamut:
                    c = log_to_rec709(*c, gamut=gamut)
                if params:
                    c = grade_pixel(*c, params)
                out.write(f"{c[0]:.6f} {c[1]:.6f} {c[2]:.6f}\n")
    return out.getvalue()


class CubeError(ValueError):
    pass


def parse_cube(text: str) -> tuple[int, list[float]]:
    """Return (size, flat [r,g,b,...] table in 0..1). Raises CubeError."""
    size, lo, hi, values = None, [0.0] * 3, [1.0] * 3, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key = line.split()[0].upper()
        if key == "LUT_3D_SIZE":
            size = int(line.split()[1])
        elif key == "LUT_1D_SIZE":
            raise CubeError("1D LUTs aren't supported; please use a 3D .cube LUT.")
        elif key == "DOMAIN_MIN":
            lo = [float(x) for x in line.split()[1:4]]
        elif key == "DOMAIN_MAX":
            hi = [float(x) for x in line.split()[1:4]]
        elif key in ("TITLE", "LUT_3D_INPUT_RANGE"):
            continue
        elif re.match(r"^[-+0-9.eE]", line):
            parts = line.split()
            if len(parts) != 3:
                raise CubeError(f"Bad data line: {line[:40]}")
            values.extend(float(p) for p in parts)
    if not size or not 2 <= size <= MAX_SIZE:
        raise CubeError("Missing or unsupported LUT_3D_SIZE (2-65).")
    if len(values) != size ** 3 * 3:
        raise CubeError(f"Expected {size ** 3} colour rows, found {len(values) // 3}.")
    span = [h - l or 1.0 for l, h in zip(lo, hi)]
    table = [min(max((v - lo[i % 3]) / span[i % 3], 0.0), 1.0) for i, v in enumerate(values)]
    return size, table


def resample(size: int, table: list[float], to: int = 33) -> list[float]:
    """Nearest-neighbour resample to `to` points per axis (for the browser preview)."""
    if size == to:
        return table
    out = []
    for b in range(to):
        for g in range(to):
            for r in range(to):
                ri, gi, bi = (round(x * (size - 1) / (to - 1)) for x in (r, g, b))
                j = ((bi * size + gi) * size + ri) * 3
                out.extend(table[j:j + 3])
    return out


def save_user_lut(name: str, text: str) -> str:
    """Validate and store an imported .cube. Returns its key."""
    size, table = parse_cube(text)
    key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "lut"
    USER_LUT_DIR.mkdir(parents=True, exist_ok=True)
    (USER_LUT_DIR / f"{key}.json").write_text(json.dumps({"name": name[:60], "size": size, "table": table}))
    return key


def user_luts() -> dict[str, dict]:
    out = {}
    if USER_LUT_DIR.exists():
        for f in sorted(USER_LUT_DIR.glob("*.json")):
            try:
                d = json.loads(f.read_text())
                out[f.stem] = {"name": d["name"], "size": d["size"], "table": d["table"]}
            except (OSError, ValueError, KeyError):
                continue
    return out


def apply_table_to_jpeg(jpeg: bytes, size: int, table: list[float], strength: float = 1.0) -> bytes:
    from PIL import Image, ImageFilter
    im = Image.open(io.BytesIO(jpeg))
    exif = im.info.get("exif")
    src = im.convert("RGB")
    out = src.filter(ImageFilter.Color3DLUT(size, table))
    if strength < 1:
        out = Image.blend(src, out, strength)
    buf = io.BytesIO()
    kw = {"quality": 95}
    if exif:
        kw["exif"] = exif
    out.save(buf, "JPEG", **kw)
    return buf.getvalue()
