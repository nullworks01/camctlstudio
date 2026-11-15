"""docs/models.md, the public list of camera models that should work, is written from Studio's own data files."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import models_doc  # noqa: E402


def test_the_page_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "models_doc.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_levels_and_what_is_tested():
    lv = models_doc.levels()
    assert "ZV-E10" in lv["Sony"]["full"]
    assert not lv["Fujifilm"]["full"] and lv["Fujifilm"]["ctrl"]          # no live picture over USB: never "Full"
    assert not any("PowerShot" in n for n in lv["Canon"]["full"] + lv["Canon"]["ctrl"])
    text = (ROOT / "docs" / "models.md").read_text(encoding="utf-8")
    assert "only the Sony ZV-E10" in text and text.count("**Wi-Fi** (untested)") == 6
    assert "(models.md)" in (ROOT / "docs" / "cameras.md").read_text(encoding="utf-8")
    assert "docs/models.md" in (ROOT / "README.md").read_text(encoding="utf-8")
