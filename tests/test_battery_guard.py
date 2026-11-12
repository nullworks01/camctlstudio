"""Every camera test tool checks the battery before each step and stops at the limit (12 Nov 2026: one run went to 12%)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import battery_guard  # noqa: E402


def test_stops_at_the_limit(monkeypatch, capsys):
    monkeypatch.setattr(battery_guard, "battery", lambda base=None: 15)
    with pytest.raises(SystemExit) as e:
        battery_guard.guard(15, "zoom run 3")
    assert e.value.code == 3 and "Stopped before zoom run 3: the battery is at 15% (limit 15%)." in capsys.readouterr().out


def test_unreadable_battery_stops_too(monkeypatch):
    monkeypatch.setattr(battery_guard, "battery", lambda base=None: None)
    with pytest.raises(SystemExit):
        battery_guard.guard(15, "recall")


def test_goes_on_above_the_limit(monkeypatch):
    monkeypatch.setattr(battery_guard, "battery", lambda base=None: 40)
    assert battery_guard.guard(15, "save") == 40
