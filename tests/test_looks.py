"""Looks: six of our own, made by our maths, production quality (smooth, monotonic, no clipping, greys neutral where
meant, skin hue close), strength 0 = identical; copies never touch the original."""
import hashlib
import io
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from camctl import looks

KEYS = [lk["key"] for lk in looks.LOOKS]


def test_six_looks_with_our_own_plain_names():
    assert len(looks.LOOKS) == 6
    parts = [("fu", "ji"), ("pro", "via"), ("vel", "via"), ("as", "tia"), ("classic ch", "rome"), ("ete", "rna"),
             ("ac", "ros"), ("ko", "dak"), ("por", "tra"), ("ek", "tar"), ("cine", "still"), ("film simu", "lation"),
             ("ag", "fa"), ("il", "ford"), ("pola", "roid")]
    banned = re.compile("|".join(a + b for a, b in parts), re.I)        # built from parts: no brand name in the code
    for lk in looks.LOOKS:
        assert not banned.search(lk["name"] + " " + lk["for"]), lk["name"]
    src = Path(looks.__file__).read_text(encoding="utf-8")
    assert not banned.search(src)


@pytest.mark.parametrize("key", KEYS)
def test_curves_are_smooth_and_monotonic_without_clipping(key):
    ramp = np.linspace(0, 1, 1024)
    grey = np.stack([ramp] * 3, axis=-1)
    out = looks.transform(grey, key)
    y = out @ np.array([0.2126, 0.7152, 0.0722])
    assert np.all(np.diff(y) >= -1e-6)                                  # never goes backwards (no banding/solarising)
    assert np.max(np.abs(np.diff(y, 2))) < 2e-3                          # smooth: no kinks
    assert y[0] >= 0 and y[-1] <= 1 and y[-1] > 0.9 and y[0] < 0.08      # no crushed blacks or clipped whites


@pytest.mark.parametrize("key", ["documentary", "softneg", "slide", "bw"])
def test_neutral_greys_stay_neutral(key):
    grey = np.stack([np.linspace(0.05, 0.95, 50)] * 3, axis=-1)
    out = looks.transform(grey, key)
    assert np.max(out.max(axis=-1) - out.min(axis=-1)) < 2e-3


@pytest.mark.parametrize("key", [k for k in KEYS if k != "bw"])
def test_skin_hue_shift_is_small(key):
    skin = np.array([[0.84, 0.63, 0.5], [0.62, 0.43, 0.33], [0.93, 0.76, 0.66]])
    out = looks.transform(skin, key)
    d = (looks._hue(out) - looks._hue(skin) + 180) % 360 - 180
    assert np.max(np.abs(d)) < 6                                         # degrees


def test_strength_zero_is_identical_and_full_strength_matches_the_table():
    img = np.asarray(Image.open(io.BytesIO(looks.sample_picture())).convert("RGB"))
    tab = looks.table("warmprint")
    assert np.array_equal(looks.apply_table(img, tab, 0.0), img)
    half = looks.apply_table(img, tab, 0.5).astype(int)
    full = looks.apply_table(img, tab, 1.0).astype(int)
    assert np.all(np.abs(half - (img.astype(int) + full) / 2) <= 1.01)
    direct = np.round(looks.transform(img / 255.0, "warmprint") * 255)
    assert np.mean(np.abs(full - direct)) < 1.5                         # the 33-point table is faithful


def test_cube_file_is_well_formed():
    t = looks.cube_text("slide")
    lines = [ln for ln in t.splitlines() if re.match(r"^[\d.]+ [\d.]+ [\d.]+$", ln)]
    assert "LUT_3D_SIZE 33" in t and len(lines) == 33 ** 3
    assert lines[0] == "0.000000 0.000000 0.000000" or all(float(x) < 0.05 for x in lines[0].split())


def test_photo_copy_keeps_the_original_and_exif(tmp_path):
    shoot = tmp_path / "shoot"
    src = shoot / "PHOTO JPEG" / "C0001.jpg"
    src.parent.mkdir(parents=True)
    im = Image.open(io.BytesIO(looks.sample_picture()))
    exif = Image.Exif()
    exif[0x010F] = "Sony"
    im.save(src, "JPEG", quality=95, exif=exif.tobytes())
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    out = looks.copy_photo(src, shoot, "documentary", 1.0)
    assert out == shoot / "LOOKS" / "Quiet documentary" / "C0001 - Quiet documentary.jpg"
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert Image.open(out).getexif().get(0x010F) == "Sony"
    with pytest.raises(ValueError):
        looks.copy_photo(src.with_suffix(".arw"), shoot, "bw")


def test_clip_copy_keeps_the_original(tmp_path):
    import subprocess
    from camctl import video
    ff = video.find_ffmpeg()
    if not ff:
        pytest.skip("ffmpeg not available")
    src = tmp_path / "VIDEO" / "C0001.MP4"
    src.parent.mkdir()
    subprocess.run([ff, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:d=1",
                    "-f", "lavfi", "-i", "sine=f=440:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(src)], check=True, timeout=60)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    out = looks.copy_clip(src, tmp_path, "coolclean", 1.0, log=True)
    info = video.probe(out)
    assert out.exists() and info["audio"] and info["width"] == 320
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert not (out.parent / ".work").exists()


def test_the_sample_is_drawn_never_a_photo():
    im = Image.open(io.BytesIO(looks.sample_picture()))
    assert im.size == (480, 300) and not im.getexif()
