"""Hand-off: shoot folders (copy only), LUTs, Lightroom preset, Resolve prep, Photoshop path."""
from pathlib import Path

from camctl import handoff, presets


def test_shoot_folder_copy_only_and_exports(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "Shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    sh = handoff.new_shoot("Interview: Maria/2")
    folder = Path(sh["path"])
    assert folder.name.endswith("Interview  Maria 2") or "Interview" in folder.name
    assert all((folder / s).is_dir() for s in handoff.SUBDIRS)
    src = tmp_path / "C0001.MP4"
    src.write_bytes(b"x" * 1000)
    raw = tmp_path / "20261111_1.arw"
    raw.write_bytes(b"II*\x00raw")
    handoff.add_file(src)
    handoff.add_file(raw)
    assert src.exists() and raw.exists()                                  # originals stay
    assert (folder / "VIDEO" / "C0001.MP4").exists() and (folder / "PHOTO RAW" / raw.name).exists()
    assert handoff.summary(folder)["VIDEO"] == 1
    look = presets.LOOKS["blockbuster"]
    files = handoff.export_luts(folder, "blockbuster", look)
    assert len(files) == 3 and all((folder / "LUTS" / f).read_text().count("\n") > 30000 for f in files)
    x = handoff.export_xmp(folder, look)
    text = (folder / "LIGHTROOM" / x).read_text(encoding="utf-8")
    assert 'crs:Contrast2012="20"' in text and "crs:ColorGradeShadowHue" in text and "camctl Blockbuster" in text
    handoff.write_notes(folder, [{"name": "iso", "label": "800"}], "desk", {"title": "Key", "lights": [], "steps": ["a"]}, "<svg/>")
    notes = (folder / "NOTES" / "shoot_notes.md").read_text(encoding="utf-8")
    assert "iso: 800" in notes and "S-Gamut3.Cine" in notes and (folder / "LIGHTING" / "lighting_diagram.svg").exists()
    r = handoff.send_to_resolve(folder)
    assert (folder / "NOTES" / "resolve_import.py").exists() and r["steps"]
    assert "S-Gamut3.Cine/S-Log3" in (folder / "NOTES" / "resolve_import.py").read_text()
    assert handoff.current()["path"] == str(folder)
