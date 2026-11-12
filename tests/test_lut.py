import io

import pytest

from camctl import grade, lut, presets


def test_export_roundtrip_matches_grade():
    look = presets.LOOKS["blockbuster"]
    text = lut.export_cube(look["grade"], "Blockbuster", size=9)
    assert "LUT_3D_SIZE 9" in text
    size, table = lut.parse_cube(text)
    assert size == 9 and len(table) == 9 ** 3 * 3
    # entry for r=1, g=0, b=0 (index 8, red changes fastest)
    expected = grade.grade_pixel(1.0, 0.0, 0.0, look["grade"])
    assert table[8 * 3:8 * 3 + 3] == pytest.approx(expected, abs=1e-5)


def test_slog3_reference_points_and_log_luts():
    # Sony S-Log3: 18% grey at code 420/1023, 0% at 95/1023
    assert grade.slog3_to_linear(420 / 1023) == pytest.approx(0.18, abs=1e-6)
    assert grade.slog3_to_linear(95 / 1023) == pytest.approx(0.0, abs=1e-6)
    # neutral grey stays neutral through both gamut matrices
    for gamut in ("sgamut3cine", "sgamut3"):
        r, g, b = grade.log_to_rec709(0.41, 0.41, 0.41, gamut)
        assert r == pytest.approx(g, abs=1e-4) and g == pytest.approx(b, abs=1e-4)
        assert 0.35 < g < 0.5          # mid grey lands mid-screen
    text = lut.export_cube(None, "log", size=5, source="slog3cine")
    size, table = lut.parse_cube(text)
    assert size == 5 and table[0:3] == pytest.approx([0, 0, 0], abs=1e-6)
    looked = lut.export_cube(presets.LOOKS["moonlight"]["grade"], "x", size=5, source="slog3cine")
    assert looked != text


def test_parse_rejects_bad_files():
    with pytest.raises(lut.CubeError):
        lut.parse_cube("LUT_1D_SIZE 4\n0 0 0\n")
    with pytest.raises(lut.CubeError):
        lut.parse_cube("LUT_3D_SIZE 2\n0 0 0\n")


def test_domain_and_resample_and_apply(tmp_path, monkeypatch):
    rows = [f"{r * 2} {g * 2} {b * 2}" for b in range(2) for g in range(2) for r in range(2)]
    size, table = lut.parse_cube("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 2 2 2\n" + "\n".join(rows))
    assert max(table) == 1.0 and min(table) == 0.0
    assert len(lut.resample(size, table, 5)) == 5 ** 3 * 3
    monkeypatch.setattr(lut, "USER_LUT_DIR", tmp_path)
    key = lut.save_user_lut("My Film LUT!", "LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in range(2) for g in range(2) for r in range(2)))
    assert key == "my-film-lut" and "my-film-lut" in lut.user_luts()
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (16, 9), (10, 200, 90)).save(buf, "JPEG")
    out = lut.apply_table_to_jpeg(buf.getvalue(), size, table, 0.5)
    assert out[:2] == b"\xff\xd8"
