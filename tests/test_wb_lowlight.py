"""White balance without a card (neutral regions, confidence, mixed light) and low-light strategies."""
import io

from PIL import Image, ImageDraw

from camctl import capture, wbauto
from camctl.drivers.sony import PROPS, PropInfo


def _jpeg(im):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=95)
    return b.getvalue()


def test_neutral_wall_gives_high_confidence_and_its_cast():
    im = Image.new("RGB", (384, 256), (200, 185, 160))        # an off-white wall under warm light
    d = ImageDraw.Draw(im)
    d.rectangle((0, 180, 384, 256), fill=(90, 50, 30))          # a brown desk (coloured: rejected)
    r = wbauto.find_neutrals(_jpeg(im))
    assert r["confidence"] == "high" and r["cast"] > 0.05 and not r["mixed"]
    assert all(box[1] < 0.75 for box in r["regions"])           # no region on the desk


def test_coloured_wall_is_not_taken_as_neutral():
    im = Image.new("RGB", (384, 256), (200, 60, 60))           # a red wall
    r = wbauto.find_neutrals(_jpeg(im))
    assert r["regions"] == [] and r["confidence"] == "low"


def test_mixed_light_is_flagged():
    im = Image.new("RGB", (384, 256), (205, 185, 150))         # tungsten-lit half
    ImageDraw.Draw(im).rectangle((192, 0, 384, 256), fill=(160, 180, 215))   # window-lit half
    r = wbauto.find_neutrals(_jpeg(im))
    assert r["mixed"] and r["confidence"] != "high"


def test_few_neutral_areas_give_low_confidence():
    im = Image.new("RGB", (384, 256), (30, 90, 160))           # mostly a blue surface
    ImageDraw.Draw(im).rectangle((0, 0, 60, 40), fill=(180, 180, 180))
    r = wbauto.find_neutrals(_jpeg(im))
    assert r["confidence"] == "low"


def _props(ap=350, iso=6400, aps=(350, 400, 560, 800), isos=(800, 1600, 3200, 6400, 12800), sh=(1 << 16) | 50):
    e = lambda code, val, ch: PropInfo(code, 4, True, True, val, choices=list(ch))
    return {PROPS["aperture"]: e(PROPS["aperture"], ap, aps), PROPS["iso"]: e(PROPS["iso"], iso, isos),
            PROPS["shutter_speed"]: e(PROPS["shutter_speed"], sh, [sh, (1 << 16) | 25, (1 << 16) | 4])}


def test_low_light_video_options_are_ranked_and_concrete():
    ll = capture.low_light("video", "interview", _props(ap=400), {"err": 1.0}, lambda n, v: str(v),
                           gear="- Amaran 100d + softbox\n- desk lamp", fps=50)
    assert ll["shortfall"] == 1.0 and "180-degree" in ll["strategy"]
    o = ll["options"]
    assert "Amaran 100d" in o[0]                                         # light first, from My gear
    assert any("f/3.5" in x for x in o) and any("f/1.8 prime gains" in x for x in o)
    assert any("25p instead of 50p" in x for x in o) and o[-1].startswith("Last resort: ISO 12800")


def test_low_light_product_photo_uses_tripod_and_long_exposure():
    ll = capture.low_light("photo", "product", _props(iso=800), {"err": 2.0}, lambda n, v: str(v))
    assert "tripod" in ll["strategy"] and "self-timer" in ll["options"][0] and "base ISO 800" in ll["options"][0]


def test_low_light_people_photo_accepts_raw_iso_compromise():
    ll = capture.low_light("photo", "portrait", _props(), {"err": 0.8}, lambda n, v: str(v))
    assert "RAW at a higher ISO" in ll["strategy"]
    assert capture.low_light("photo", "portrait", _props(), {"err": 0.1}, lambda n, v: str(v)) is None
