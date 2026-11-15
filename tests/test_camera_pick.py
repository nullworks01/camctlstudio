"""The start screen "Your camera": maker, model and how it connects (USB, Wi-Fi, webcam mode, HDMI capture card).
Shown first at start until he chose once; the choice is remembered and changes nothing on the camera."""
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from camctl import webui, wifi
from camctl.drivers import registry

from test_autopilot import _studio_with
from zve10_sim import ZVE10

PAGE = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(wifi, "BASE", tmp_path)
    monkeypatch.setattr(wifi, "CONFIG", tmp_path / "wifi.json")
    st = _studio_with(ZVE10(ev=10))
    with TestClient(webui.build_app(st, start_auto=False)) as c:
        yield c, st


def test_every_maker_says_how_it_can_connect():
    by = {c["brand"]: c for c in registry.camera_choices()}
    assert set(by) == {"Sony", "Canon", "Nikon", "Fujifilm", "Panasonic", "OM System / Olympus", "Ricoh / Pentax", "Leica"}
    assert by["Sony"]["tested"] and not any(c["tested"] for b, c in by.items() if b != "Sony")
    assert by["Sony"]["models"][0] == "ZV-E10"                          # the tested model first
    assert by["Canon"]["usb_live"] and not by["Fujifilm"]["usb_live"]
    assert by["Panasonic"]["wifi_ip"] == "192.168.54.1" and not by["Leica"]["wifi"]
    assert by["Fujifilm"]["webcam"] and not by["Ricoh / Pentax"]["webcam"]


def test_start_screen_until_he_picked_once(app):
    c, st = app
    assert c.get("/api/ready").json()["picked"] is False
    d = c.get("/api/camera/choices").json()
    assert d["connected"]["ok"] and d["connected"]["brand"] == "Sony" and d["pick"] is None
    r = c.post("/api/camera/choices", json={"brand": "Sony", "model": "ZV-E10", "route": "usb"})
    assert r.status_code == 200 and r.json()["pick"] == {"brand": "Sony", "model": "ZV-E10", "route": "usb"}
    assert st.cam is not None and st.choice == "usb"                    # already on the cable: no reconnect
    assert c.get("/api/ready").json()["picked"] is True
    assert c.get("/api/connection").json()["pick"]["model"] == "ZV-E10"


def test_wrong_choices_are_said_in_words(app):
    c, _ = app
    assert "maker" in c.post("/api/camera/choices", json={"route": "usb"}).json()["error"]
    assert "Wi-Fi" in c.post("/api/camera/choices", json={"brand": "Leica", "route": "wifi"}).json()["error"]
    assert c.post("/api/camera/choices", json={"brand": "Sony", "route": "teleport"}).status_code == 400
    assert wifi.load().get("pick") is None


def test_wifi_pick_saves_the_maker_and_its_address(app):
    c, st = app
    st.released = True
    r = c.post("/api/camera/choices", json={"brand": "Panasonic", "route": "wifi"})      # its own network address
    assert r.status_code == 200
    assert st.host == "192.168.54.1:80" and wifi.brand_for(st.host) == "Panasonic" and st.choice == "wifi"
    st.shutdown()


def test_webcam_and_card_only_bring_the_picture(app):
    c, st = app
    for route in ("webcam", "card"):
        r = c.post("/api/camera/choices", json={"brand": "Fujifilm", "model": "X-T4", "route": route})
        assert r.status_code == 200 and st.choice == "auto" and st.cam is not None


def test_page_has_the_start_screen_and_the_ways_back_to_it():
    for el in ("camPick", "cpMakers", "cpModel", "cpRoutes", "cpHow", "cpGo", "cpLater", "offPick", "rdPick", "connPick"):
        assert f'id="{el}"' in PAGE, el
    assert re.search(r"if\(!r\.picked && !CP\.shown\)\{ cpOpen\(true\)", PAGE)    # first start: the chooser first
    for k in ("auto", "usb", "wifi", "webcam", "card"):
        assert f'["{k}",' in PAGE
