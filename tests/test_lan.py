"""LAN (iPad) mode: PIN required for every device, private network only, rate-limited."""
from starlette.testclient import TestClient

from camctl import webui


def _app(monkeypatch, tmp_path):
    monkeypatch.setattr(webui, "PIN_FILE", tmp_path / "pin.json")
    webui.set_pin("482915")
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    inner = Starlette(routes=[Route("/api/x", lambda r: JSONResponse({"ok": True})), Route("/", lambda r: JSONResponse({"page": 1}))])
    return webui.PinGate(inner)


def test_pin_required_hashed_and_rate_limited(monkeypatch, tmp_path):
    gate = _app(monkeypatch, tmp_path)
    assert "482915" not in (tmp_path / "pin.json").read_text()                 # only a salted hash is stored
    c = TestClient(gate, base_url="http://10.0.0.34", client=("10.0.0.50", 50000))
    assert c.get("/api/x").status_code == 401                                   # nothing without the PIN
    assert c.get("/", follow_redirects=False).status_code == 303
    for _ in range(5):
        assert c.post("/login", data={"pin": "000000"}).status_code == 401
    assert c.post("/login", data={"pin": "482915"}).status_code == 429         # locked after 5 wrong tries
    gate.locked_until = 0
    r = c.post("/login", data={"pin": "482915"}, follow_redirects=False)
    assert r.status_code == 303 and webui.SESSION_COOKIE in r.cookies
    assert c.get("/api/x").json() == {"ok": True}


def test_private_network_only():
    assert webui._private_address("10.0.0.50") and webui._private_address("10.0.0.3") and webui._private_address("127.0.0.1")
    assert not webui._private_address("8.8.8.8") and not webui._private_address("")


def test_the_laptop_itself_needs_no_pin_but_the_ipad_does(monkeypatch, tmp_path):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    monkeypatch.setattr(webui, "PIN_FILE", tmp_path / "pin.json")
    webui.set_pin("4321")
    gate = webui.PinGate(Starlette(routes=[Route("/api/health", lambda r: JSONResponse({"ok": True}))]))
    laptop = TestClient(gate, base_url="http://127.0.0.1:8770", client=("127.0.0.1", 50000))
    ipad = TestClient(gate, base_url="http://10.0.0.34:8770", client=("10.0.0.50", 50000))
    assert laptop.get("/api/health").status_code == 200
    assert ipad.get("/api/health").status_code == 401


def test_switching_modes_stops_the_other_studio_cleanly(monkeypatch):
    """The iPad shortcut while laptop-only Studio runs: the running one is stopped first, not a busy-port failure."""
    from camctl import launch
    state = {"mode": "local", "stopped": 0}
    monkeypatch.setattr(launch, "running_mode", lambda port=8770: state["mode"])

    def fake_stop(port=8770, wait_s=15.0):
        state["stopped"] += 1
        state["mode"] = None
        return True
    monkeypatch.setattr(launch, "stop", fake_stop)
    assert launch.prepare("lan") == 0 and state["stopped"] == 1        # laptop-only was running: stopped
    state["mode"] = "lan"
    assert launch.prepare("lan") == 3 and state["stopped"] == 1        # already home-network: left alone
    assert launch.prepare("local") == 0 and state["stopped"] == 2      # back to laptop-only: stopped first


def test_the_laptop_bypass_is_the_real_peer_only(monkeypatch, tmp_path):
    """Headers can't fake the laptop: X-Forwarded-For / Host: 127.0.0.1 from another device still need the PIN."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    monkeypatch.setattr(webui, "PIN_FILE", tmp_path / "pin.json")
    webui.set_pin("4321")
    gate = webui.PinGate(Starlette(routes=[Route("/api/health", lambda r: JSONResponse({"ok": True}))]))
    ipad = TestClient(gate, base_url="http://127.0.0.1:8770", client=("10.0.0.50", 50000))
    for h in ({"X-Forwarded-For": "127.0.0.1"}, {"Host": "127.0.0.1:8770"}, {"X-Real-IP": "::1", "Origin": "http://127.0.0.1:8770"}):
        assert ipad.get("/api/health", headers=h).status_code == 401, h


def test_key_pages_refused_for_the_ipad_even_with_the_pin(monkeypatch, tmp_path):
    from camctl import keystore
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(webui, "PIN_FILE", tmp_path / "pin.json")
    monkeypatch.setattr(keystore, "BASE", tmp_path)
    monkeypatch.setattr(keystore, "PATH", tmp_path / "claude_key.bin")
    webui.set_pin("4321")
    app = webui.PinGate(webui.build_app(_studio_with(ZVE10(ev=10)), start_auto=False))
    ipad = TestClient(app, base_url="http://10.0.0.34:8770", client=("10.0.0.50", 50000))
    r = ipad.post("/login", data={"pin": "4321"}, follow_redirects=False)
    assert r.status_code == 303                                             # PIN accepted, session cookie set
    k = ipad.post("/api/claude/key", json={"key": ("sk-" + "ant-test-") + "Q7x" * 12}, headers={"X-Forwarded-For": "127.0.0.1"})
    assert k.status_code == 403 and "laptop" in k.json()["error"]
    assert ipad.get("/api/claude/state").json()["local"] is False
