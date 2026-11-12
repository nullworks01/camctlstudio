"""Line up judged by camera position and size, not picture similarity, on a recorded frame of his camera (12 Nov 2026):
same camera with the person moved slightly is 'Lined up'; a moved or closer camera gives the right single instruction."""
import io
from pathlib import Path

import pytest
from PIL import Image

from camctl import match, resume

REF = (Path(__file__).parent / "data" / "lineup_ref.jpg").read_bytes()


def _jpg(im):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=88)
    return b.getvalue()


def _say(live: Image.Image):
    return resume.line_up_say(match.compare(match.prepare(REF), _jpg(live)))


def _shift(dx, dy):
    im = Image.open(io.BytesIO(REF)).convert("RGB")
    out = Image.new("RGB", im.size)
    out.paste(im, (dx, dy))
    if dx > 0:
        out.paste(im.crop((0, 0, dx, im.height)).transpose(Image.FLIP_LEFT_RIGHT), (0, dy))
    return out


def _zoom(k):
    im = Image.open(io.BytesIO(REF)).convert("RGB")
    w, h = im.size
    if k > 1:                                   # camera closer: everything bigger
        cw, ch = int(w / k), int(h / k)
        return im.crop(((w - cw) // 2, (h - ch) // 2, (w + cw) // 2, (h + ch) // 2)).resize((w, h), Image.LANCZOS)
    small = im.resize((int(w * k), int(h * k)), Image.LANCZOS)   # camera further away: everything smaller
    out = im.resize((w, h)).copy()
    out.paste(small, ((w - small.width) // 2, (h - small.height) // 2))
    return out


def test_same_camera_person_moved_slightly_is_lined_up():
    im = Image.open(io.BytesIO(REF)).convert("RGB")
    person = im.crop((380, 100, 740, 576))
    im.paste(person, (400, 96))                  # he leans 20 px to the side and a little up
    say, lined = _say(im)
    assert lined and say == "Lined up"


def test_unchanged_is_lined_up():
    assert _say(Image.open(io.BytesIO(REF)).convert("RGB")) == ("Lined up", True)


@pytest.mark.parametrize("dx,dy,want", [(70, 0, "A little right"), (0, 45, "Camera lower")])
def test_moved_camera_gives_one_instruction(dx, dy, want):
    say, lined = _say(_shift(dx, dy))
    assert not lined and say == want


@pytest.mark.parametrize("k,want", [(1.18, "Further back"), (0.85, "Move closer")])
def test_closer_or_further_camera(k, want):
    say, lined = _say(_zoom(k))
    assert not lined and say == want
