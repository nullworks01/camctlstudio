"""Studio's window and the page checks use Google Chrome first, then Microsoft Edge, then Chromium."""
import sys

from camctl import browser


def test_chrome_comes_before_edge(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "CAMCTL_BROWSER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"")
    assert browser.find() == str(edge) and browser.name(browser.find()) == "Edge"     # only Edge: Edge
    chrome = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"")
    assert browser.find() == str(chrome) and browser.name(browser.find()) == "Chrome"  # both: Chrome


def test_a_browser_chosen_by_hand_wins(monkeypatch, tmp_path):
    own = tmp_path / "mybrowser"
    own.write_bytes(b"")
    monkeypatch.setenv("CAMCTL_BROWSER", str(own))
    assert browser.find() == str(own)


def test_none_found_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "candidates", lambda: [tmp_path / "missing"])
    assert browser.find() is None and browser.name(None) == "none"
