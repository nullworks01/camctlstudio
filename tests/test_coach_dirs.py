"""Coach directions from the picture: side light, backlight, framing (no Claude)."""
from PIL import Image, ImageDraw

from camctl import coach


def _m(face_mean, zones_top=40, clip=0.0):
    z = {k: {"mean": zones_top if k.startswith("top") else 40, "clip_pct": 0} for k in
         ("top-left", "top", "top-right", "left", "centre", "right", "bottom-left", "bottom", "bottom-right")}
    return {"overall": {"mean": 40, "clip_pct": clip}, "face": {"mean": face_mean}, "zones": z}


def test_side_light_and_backlight_and_framing():
    im = Image.new("RGB", (256, 144), (90, 90, 90))
    d = ImageDraw.Draw(im)
    d.rectangle((100, 30, 127, 90), fill=(230, 200, 180))     # bright left half of the face
    d.rectangle((128, 30, 156, 90), fill=(110, 90, 80))       # dark right half
    box = [100 / 256, 30 / 144, 56 / 256, 60 / 144]
    out = coach._directions(im, _m(60), box, True, False)
    side = next(x for x in out if x["key"] == "side")
    assert side["light"] == "left" and "right side" in side["text"]
    out = coach._directions(im, _m(45, zones_top=95, clip=5), box, True, False)
    assert any(x["key"] == "backlight" and x["light"] == "behind" for x in out)
    low = coach._directions(im, _m(60), [0.4, 0.5, 0.2, 0.3], True, False)
    assert any(x["key"] == "eyes_low" for x in low)
    none = coach._directions(im, _m(60), None, False, False)
    assert none[0]["key"] == "face"
