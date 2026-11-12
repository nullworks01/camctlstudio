"""S-Log3 -> Rec.709 monitor view and the S-Log3 exposure target."""
import re
from pathlib import Path

from camctl import capture, grade, presets

PAGE = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")


def _code(stops_over_grey):
    return (presets.SLOG3_GREY + presets.SLOG3_STOP * stops_over_grey) / 255


def test_known_slog3_values_map_to_expected_709():
    g = grade.log_to_rec709(0.41, 0.41, 0.41)                  # 18 % grey (S-Log3 code 420/1023)
    assert all(abs(c - 0.409) < 0.015 for c in g)              # -> about 41 IRE, neutral
    b = grade.log_to_rec709(95 / 1023, 95 / 1023, 95 / 1023)   # S-Log3 black
    assert all(c < 0.01 for c in b)
    w = grade.log_to_rec709(0.61, 0.61, 0.61)                  # 90 % white (code ~ 0.61)
    assert all(0.8 < c < 0.95 for c in w)                      # bright, not clipped


def test_ramp_is_smooth_and_monotonic():
    ys = [grade.log_to_rec709(i / 255, i / 255, i / 255)[0] for i in range(40, 240)]
    assert all(b > a for a, b in zip(ys, ys[1:]))              # no flat steps, no posterisation
    assert max(b - a for a, b in zip(ys, ys[1:])) < 0.02       # no jumps


def test_skin_highlights_in_range_are_not_clipped():
    # the owner's measured warm skin highlight (forehead, S-Log3 under 2700 K): R 60.6, G 56.8, B 52.6 IRE
    r, g, b = grade.log_to_rec709(0.606, 0.568, 0.526)
    assert max(r, g, b) < 0.95 and r > g > b                   # bright skin, still graded colour, not flat white
    skin = grade.log_to_rec709(_code(1.5) + 0.02, _code(1.5), _code(1.5) - 0.02)   # skin at Studio's target
    assert max(skin) < 0.85


def test_view_is_applied_once_and_float_path():
    assert PAGE.count("applyLog709(d,LUTPP") == 1                   # exactly one place draws the 709 view
    assert "VIEW_COMP" in PAGE and "709 VIEW · FILE IS S-LOG3" in PAGE
    # page and grade.py share the roll-off start
    assert re.search(r"const SH=0\.4;", PAGE) and grade.SHOULDER_START == 0.4


def test_slog3_exposure_target_for_skin_is_in_the_safe_band():
    face = capture.target_level(True, True, False) / 2.55
    scene = capture.target_level(True, False, False) / 2.55
    assert 48 <= face <= 56 and 45 <= scene <= 52
