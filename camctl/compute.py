"""Computational photography on the PC: features the ZV-E10 does not have, built from several real photos.

- focus stack:   sharpest pixels of a near→far series (products, macro), one image sharp front to back
- HDR:           exposure fusion of a bracket (Mertens), detail in bright windows and dark corners
- clean burst:   align and average a handheld burst: noise drops by about sqrt(n) (ISO 12800 → about 3200 with 16)
- long exposure: average of a series from a tripod: smooth water and sky without an ND filter

Inputs are the JPEG references the camera sends with every RAW (the RAWs are never touched). Results are a
16-bit TIFF for Lightroom / Photoshop plus a JPEG preview. Alignment is ECC (homography) on a small greyscale
copy, so handheld series line up; for a tripod it is close to identity.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

try:
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None

KINDS = {
    "focusstack": {"name": "Focus stack", "shots": 8, "line": "Near to far, merged: sharp from front to back"},
    "hdr": {"name": "HDR photo", "shots": 5, "line": "Darker and brighter photos merged: detail in the windows and the shadows"},
    "burst": {"name": "Clean night photo", "shots": 12, "line": "A burst aligned and averaged: much less noise"},
    "longexp": {"name": "Long exposure", "shots": 16, "line": "Averaged from a tripod: smooth water and sky, no ND filter"},
}


def _need_cv():
    if cv2 is None:
        raise RuntimeError("OpenCV is not available")


def load(paths: list[Path]) -> list[np.ndarray]:
    _need_cv()
    out = []
    for p in paths:
        im = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if im is None:
            raise ValueError(f"cannot read {p}")
        out.append(im)
    h, w = out[0].shape[:2]
    return [im if im.shape[:2] == (h, w) else cv2.resize(im, (w, h)) for im in out]


def align(images: list[np.ndarray], scale_to: int = 1000) -> tuple[list[np.ndarray], list[float]]:
    """Align every image to the first (ECC homography on a small greyscale copy). Returns images and a 0..1
    quality per image (1 = perfectly aligned); a failed alignment keeps the image as it is (quality 0)."""
    _need_cv()
    ref = images[0]
    h, w = ref.shape[:2]
    k = min(1.0, scale_to / max(h, w))
    small = lambda im: cv2.GaussianBlur(cv2.cvtColor(cv2.resize(im, (int(w * k), int(h * k))), cv2.COLOR_BGR2GRAY), (5, 5), 0)
    ref_s = small(ref).astype(np.float32)
    out, quality = [ref], [1.0]
    S = np.array([[k, 0, 0], [0, k, 0], [0, 0, 1]], np.float64)
    for im in images[1:]:
        warp = np.eye(3, dtype=np.float32)
        try:
            cc, warp = cv2.findTransformECC(ref_s, small(im).astype(np.float32), warp, cv2.MOTION_HOMOGRAPHY,
                                            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5), None, 5)
            full = np.linalg.inv(S) @ warp.astype(np.float64) @ S
            out.append(cv2.warpPerspective(im, full, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                           borderMode=cv2.BORDER_REPLICATE))
            quality.append(float(max(0.0, min(1.0, cc))))
        except cv2.error:
            out.append(im)
            quality.append(0.0)
    return out, quality


def focus_stack(images: list[np.ndarray]) -> np.ndarray:
    """Per pixel, take the image where the local contrast (Laplacian energy) is highest; smooth the choice."""
    _need_cv()
    energies = []
    for im in images:
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32)
        lap = cv2.Laplacian(cv2.GaussianBlur(g, (3, 3), 0), cv2.CV_32F, ksize=3)
        energies.append(cv2.GaussianBlur(np.abs(lap), (0, 0), 3))
    idx = np.argmax(np.stack(energies), axis=0)
    stack = np.stack(images).astype(np.float32)
    # soft blend around the boundaries: weight each image by a blurred one-hot map
    weights = []
    for i in range(len(images)):
        weights.append(cv2.GaussianBlur((idx == i).astype(np.float32), (0, 0), 2) + 1e-6)
    wsum = np.sum(weights, axis=0)
    out = np.zeros_like(stack[0])
    for i, wgt in enumerate(weights):
        out += stack[i] * (wgt / wsum)[..., None]
    return out / 255.0


def hdr(images: list[np.ndarray]) -> np.ndarray:
    """Exposure fusion (Mertens): no tone-mapping look, natural contrast, 0..1 float."""
    _need_cv()
    fused = cv2.createMergeMertens().process([im.astype(np.float32) / 255.0 for im in images])
    lo, hi = np.percentile(fused, 0.05), np.percentile(fused, 99.95)
    return np.clip((fused - lo) / max(hi - lo, 1e-6), 0, 1)


def average(images: list[np.ndarray]) -> np.ndarray:
    """Mean of the series (clean burst / long exposure), 0..1 float with 16-bit precision."""
    acc = np.zeros(images[0].shape, np.float64)
    for im in images:
        acc += im
    return (acc / len(images) / 255.0).astype(np.float32)


def noise_estimate(im: np.ndarray) -> float:
    """Robust noise sigma (MAD of the high-pass), in 0..255 units: used to report the improvement."""
    _need_cv()
    g = cv2.cvtColor(im if im.dtype == np.uint8 else np.clip(im * 255, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
    hp = g - cv2.GaussianBlur(g, (0, 0), 1.5)
    return float(1.4826 * np.median(np.abs(hp - np.median(hp))))


def save(result: np.ndarray, out_base: Path) -> tuple[Path, Path]:
    """16-bit TIFF (for grading) + JPEG preview."""
    _need_cv()
    out_base.parent.mkdir(parents=True, exist_ok=True)
    tif, jpg = out_base.with_suffix(".tif"), out_base.with_suffix(".jpg")
    cv2.imwrite(str(tif), np.clip(result * 65535, 0, 65535).astype(np.uint16))
    cv2.imwrite(str(jpg), np.clip(result * 255, 0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 95])
    return tif, jpg


def run(kind: str, paths: list[Path], out_base: Path, tripod: bool = False) -> dict:
    """Merge the photos of one series. Returns the files and plain-word notes."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    if len(paths) < 2:
        raise ValueError("needs at least 2 photos")
    imgs = load(paths)
    notes = []
    if tripod or kind == "longexp":
        aligned, q = imgs, [1.0] * len(imgs)
    else:
        aligned, q = align(imgs)
        bad = sum(1 for x in q[1:] if x < 0.6)
        if bad:
            notes.append(f"{bad} of {len(q) - 1} photos did not line up well (movement): the result may show ghosting there.")
    if kind == "focusstack":
        res = focus_stack(aligned)
    elif kind == "hdr":
        res = hdr(aligned)
    else:
        res = average(aligned)
        if kind == "burst":
            n0, n1 = noise_estimate(imgs[0]), noise_estimate(res)
            if n0 > 0:
                notes.append(f"Noise about {n0 / max(n1, 1e-3):.1f}× lower than one photo "
                             f"(like {math.log2(max(n0 / max(n1, 1e-3), 1)):.1f} stops less ISO).")
    tif, jpg = save(res, out_base)
    return {"kind": kind, "name": KINDS[kind]["name"], "tif": str(tif), "jpg": str(jpg), "inputs": [str(p) for p in paths],
            "aligned_quality": [round(x, 2) for x in q], "notes": notes}
