"""Planning a focus stack from a focus sweep (no photos): where is each part of the picture sharpest?

Measured 12 Nov 2026 on his kit lens: a focus tap is not a reliable way to find the near and far point (it gave the
same position for both), but stepping the focus and measuring the live picture's local sharpness is. The plan gives
the start and end focus positions for the stack and one plain verdict before he presses.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

GRID = (8, 6)              # tiles across, down


def tile_sharpness(jpeg: bytes) -> np.ndarray:
    """Local sharpness (gradient energy) per tile of the live picture, shape (rows, cols)."""
    im = Image.open(io.BytesIO(jpeg)).convert("L").resize((320, 180))
    a = np.asarray(im).astype(np.float32)
    gy, gx = np.gradient(a)
    e = gx * gx + gy * gy
    cols, rows = GRID
    h, w = e.shape
    return np.array([[e[r * h // rows:(r + 1) * h // rows, c * w // cols:(c + 1) * w // cols].mean()
                      for c in range(cols)] for r in range(rows)], dtype=np.float32)


def plan(samples: list[tuple[int, np.ndarray]], has_face: bool = False) -> dict:
    """samples: (focus position, tile sharpness) from near to far. Returns start/end positions and the verdict."""
    samples = [(p, t) for p, t in samples if p is not None]
    if len(samples) < 3:
        return {"ok": False, "verdict": "Couldn't measure the focus range here.", "kind": "unknown"}
    pos = np.array([p for p, _ in samples])
    stack = np.stack([t for _, t in samples])                 # (n, rows, cols)
    peak = stack.max(axis=0)
    strong = peak >= 0.5 * float(peak.max())                  # the tiles with real detail: the object
    best = pos[stack.argmax(axis=0)][strong]
    near, far = int(np.percentile(best, 10)), int(np.percentile(best, 90))
    out = {"near": near, "far": far, "positions_checked": [int(p) for p in pos]}
    warn = " A face is in the picture: a focus stack is for still objects; a person moving ruins it." if has_face else ""
    if np.mean(best <= pos.min()) > 0.5:
        return {**out, "ok": False, "kind": "too_close",
                "verdict": "Too close: the lens can't focus this near. Move the camera back a little." + warn}
    if far - near < 6:
        return {**out, "ok": False, "kind": "all_sharp",
                "verdict": "Everything is already sharp at this zoom: one photo is enough. Zoom in or come closer for a stack." + warn}
    return {**out, "ok": True, "kind": "good", "start": max(0, near - 2), "end": min(100, far + 2),
            "verdict": f"Good: near and far differ (focus {near} to {far}). The stack covers that range." + warn}
