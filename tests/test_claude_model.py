"""Studio's Claude calls on Claude Fable 5.2: the request shape, refusals, the one-time safety net to Opus 5.5 and
timeouts, against a local STUB server (no real API call, no real key: a fake one)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

FAKE_KEY = "sk-" + "ant-test-FAKE-not-a-real-key"     # built from parts: the publish guard flags key-shaped text


class Stub:
    def __init__(self):
        self.mode = "ok"
        self.seen = []                      # (headers, body) of every request

    def reply(self, h, body):
        """What the real API does with this request shape (the rules the coordinator listed), then the scenario."""
        self.seen.append(({k.lower(): v for k, v in h.items()}, body))
        m = body.get("model")
        th = body.get("thinking")
        if th is not None and th.get("type") != "adaptive":
            return 400, {"error": {"message": "thinking.type disabled/enabled is not supported for this model"}}
        if any(k in body for k in ("temperature", "top_p", "top_k")):
            return 400, {"error": {"message": "temperature is not supported for this model"}}
        if (body.get("tool_choice") or {}).get("type") in ("any", "tool"):
            return 400, {"error": {"message": 'tool_choice: type "tool" and "any" are not supported for this model.'}}
        prompt = json.dumps(body.get("messages"))
        if m == "claude-fable-5-2" and body.get("max_tokens", 0) < 16000 and "Reply with OK." not in prompt:
            return 400, {"error": {"message": "max_tokens too small for thinking"}}
        if m == "claude-fable-5-2" and self.mode == "404":
            return 404, {"error": {"type": "not_found_error", "message": "model: claude-fable-5-2"}}
        if self.mode == "timeout":
            time.sleep(2.5)
        if self.mode == "refusal":
            return 200, {"model": m, "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": None},
                         "content": [], "usage": {"input_tokens": 10, "output_tokens": 0}}
        text = json.dumps({"score": 77, "verdict": "Close.", "fixes": []})
        return 200, {"model": m, "stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": ""},
                                                                        {"type": "text", "text": text}],
                     "usage": {"input_tokens": 1000, "output_tokens": 200}}


@pytest.fixture
def stub(monkeypatch, tmp_path):
    from camctl import assistant
    s = Stub()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            code, out = s.reply(self.headers, json.loads(self.rfile.read(n)))
            data = json.dumps(out).encode()
            try:
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(assistant, "API_URL", f"http://127.0.0.1:{srv.server_address[1]}/v1/messages")
    monkeypatch.setattr(assistant, "_key", lambda: FAKE_KEY)
    monkeypatch.setattr(assistant.usage, "file", tmp_path / "usage.json")
    monkeypatch.setattr(assistant, "_NOT_FOR_THIS_KEY", {"until": 0.0})
    monkeypatch.setattr(assistant, "FALLBACKS", [])
    yield s
    srv.shutdown()


def _compare():
    from camctl import assistant
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (32, 18), (90, 90, 90)).save(b, "JPEG")
    return assistant.compare_to_reference(["AAAA"], b.getvalue(), "test")


def test_models_named_in_one_place_opus_is_studios_choice():
    from pathlib import Path
    from camctl import assistant
    assert assistant.MODEL == "claude-opus-5-5" and assistant.MODEL_NAME == "Claude Opus 5.5"
    assert assistant.chosen_model() == "claude-opus-5-5"                         # the default
    assert assistant.FABLE == "claude-fable-5-2" and assistant.FALLBACK_MODEL == "claude-opus-5-5"
    pkg = Path(assistant.__file__).parent
    for f in pkg.glob("*.py"):
        if f.name != "assistant.py":
            t = f.read_text(encoding="utf-8")
            assert "claude-fable" not in t and "claude-opus" not in t, f.name
    page = (pkg / "studio_pro.html").read_text(encoding="utf-8")
    assert "claude-fable" not in page and 'id="asClaudeModelT"' in page        # the page shows the server's name


def test_shape_removes_what_fable_rejects():
    from camctl import assistant
    b = assistant.shape({"model": "x", "max_tokens": 300, "thinking": {"type": "disabled"}, "temperature": 0.2,
                         "top_p": 0.9, "tool_choice": {"type": "tool", "name": "t"},
                         "tools": [{"name": "t", "input_schema": {"type": "object", "properties": {},
                                                                    "additionalProperties": False}}],
                         "messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "{"}]},
                        assistant.FABLE)
    assert b["model"] == "claude-fable-5-2" and "thinking" not in b and "temperature" not in b and "top_p" not in b
    assert b["tool_choice"] == {"type": "auto"} and b["tools"][0]["strict"] is True
    assert b["max_tokens"] >= 16000 and b["fallbacks"] == "default"
    assert b["messages"][-1]["role"] == "user"                                   # no prefill


def test_normal_answer_with_effort_per_button(stub):
    from camctl import assistant
    assistant.choose_model(assistant.FABLE)
    with assistant.feature("lighting lab"):
        out = _compare()
    assert out["score"] == 77
    h, body = stub.seen[-1]
    assert body["model"] == "claude-fable-5-2" and body["output_config"]["effort"] == "high"
    assert h.get("anthropic-beta") == "server-side-fallback-2026-07-01" and body["fallbacks"] == "default"
    with assistant.feature("operator decision"):
        _compare()
    assert stub.seen[-1][1]["output_config"]["effort"] == "low"
    u = assistant.usage.today()
    assert u["model"] == "Claude Fable 5.2" and u["total"]["cost"] == pytest.approx(2 * (1000 * 8 + 200 * 40) / 1e6, rel=1e-3)


def test_refusal_is_a_calm_sentence(stub):
    from camctl import assistant
    stub.mode = "refusal"
    with pytest.raises(assistant.AskError) as e:
        _compare()
    assert str(e.value) == "Claude declined this request. Try wording it differently."
    assert len(stub.seen) == 1                                                   # no retry on a refusal


def test_404_falls_back_once_to_opus_and_says_so(stub, tmp_path):
    from camctl import assistant, autofix
    assistant.choose_model(assistant.FABLE)
    stub.mode = "404"
    out = _compare()
    assert out["score"] == 77
    assert [b["model"] for _, b in stub.seen] == ["claude-fable-5-2", "claude-opus-5-5"]
    assert assistant.FALLBACKS and assistant.FALLBACK_NAME == "Claude Opus 5.5"
    log = (autofix._stamp_file().parent / "logs" / "operator.log").read_text(encoding="utf-8")
    assert "answered 404" in log and "Claude Opus 5.5" in log and FAKE_KEY not in log
    stub.seen.clear()
    _compare()                                                                    # not offered to this key: no 404 again
    assert [b["model"] for _, b in stub.seen] == ["claude-opus-5-5"]


def test_timeout_is_one_calm_answer(stub, monkeypatch):
    from camctl import assistant
    monkeypatch.setattr(assistant, "TIMEOUT_S", 1)
    stub.mode = "timeout"
    with pytest.raises(assistant.AskError) as e:
        _compare()
    assert "took longer" in str(e.value) and "Traceback" not in str(e.value)
    assert len(stub.seen) == 1                                                   # one press = one request


def test_key_test_stays_tiny(stub):
    from camctl import assistant
    assistant.choose_model(assistant.FABLE)
    st, say = assistant.test_key(FAKE_KEY)
    assert st == "ok" and "Claude Fable 5.2" in say
    body = stub.seen[-1][1]
    assert body["max_tokens"] <= 64 and body["output_config"]["effort"] == "low"
    stub.mode = "404"
    st, say = assistant.test_key(FAKE_KEY)
    assert st == "ok" and "Claude Opus 5.5" in say


def test_page_shows_the_safety_net_and_can_stop_waiting():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    assert 'r.headers.get("X-Claude-Answered-By")' in page and '"Answered with "+by' in page
    assert 'data-cl="stopwait"' in page and "Stop waiting" in page


@pytest.mark.parametrize("mode,status,words", [
    ("cut", "ok", "Claude Fable 5.2"),                    # 200 with stop_reason max_tokens and no text: still works
    ("401", "wrong", "didn't accept"),
    ("403model", "ok", "Studio will use Claude Opus 5.5"),
    ("404", "ok", "Studio will use Claude Opus 5.5"),
    ("429", "busy", "couldn't check the key"),
    ("500", "busy", "couldn't check the key"),
])
def test_key_test_cases(stub, monkeypatch, mode, status, words):
    from camctl import assistant
    assistant.choose_model(assistant.FABLE)
    real = stub.reply

    def reply(h, body):
        if mode == "cut":
            stub.seen.append((h, body))
            return 200, {"model": body["model"], "stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": ""}],
                         "usage": {"input_tokens": 5, "output_tokens": 64}}
        if mode in ("401", "429", "500"):
            stub.seen.append((h, body))
            return int(mode), {"error": {"message": "x"}}
        if mode == "403model" and body["model"] == "claude-fable-5-2":
            stub.seen.append((h, body))
            return 403, {"error": {"message": "model not allowed"}}
        stub.mode = "404" if mode == "404" else "ok"
        return real(h, body)
    monkeypatch.setattr(stub, "reply", reply)
    st, say = assistant.test_key(FAKE_KEY)
    assert st == status and words in say and FAKE_KEY not in say


def test_key_test_offline(monkeypatch):
    from camctl import assistant
    monkeypatch.setattr(assistant, "API_URL", "http://127.0.0.1:9/v1/messages")      # nothing listens there
    st, say = assistant.test_key(FAKE_KEY)
    assert st == "offline" and "internet" in say


def test_opus_is_the_default_shape_cost_and_no_retry(stub):
    from camctl import assistant
    out = _compare()
    assert out["score"] == 77
    h, body = stub.seen[-1]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default" and "thinking" not in body
    assert body["output_config"]["effort"] == "low" and body["max_tokens"] == 8000     # the shape proven before
    u = assistant.usage.today()
    assert u["model"] == "Claude Opus 5.5" and u["total"]["cost"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6, rel=1e-3)
    stub.seen.clear()
    real = stub.reply
    stub.reply = lambda h, b: (stub.seen.append((h, b)), (404, {"error": {"message": "x"}}))[1]
    with pytest.raises(assistant.AskError):
        _compare()
    assert [b["model"] for _, b in stub.seen] == ["claude-opus-5-5"]           # Opus chosen: no other model
    stub.reply = real
    st, say = assistant.test_key(FAKE_KEY)
    assert st == "ok" and "Claude Opus 5.5" in say


def test_the_choice_is_kept_in_the_prefs_file(stub):
    from camctl import assistant
    assistant.choose_model(assistant.FABLE)
    f = assistant._prefs_file()
    assert json.loads(f.read_text(encoding="utf-8"))["claude_model"] == "claude-fable-5-2"
    assert assistant.chosen_model() == "claude-fable-5-2"                        # read from the file: kept after a restart
    _compare()
    assert stub.seen[-1][1]["model"] == "claude-fable-5-2"
    assistant.choose_model(None)                                                 # Studio's choice again
    assert "claude_model" not in json.loads(f.read_text(encoding="utf-8")) and assistant.chosen_model() == "claude-opus-5-5"


def test_page_has_the_model_row_in_one_place():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    assert page.count('id="asClaudeModel"') == 1 and page.count('id="asModelPick"') == 1
    from camctl import assistant
    assert "Fast and precise. Studio's choice." in assistant.MODEL_LINES.values()
    assert "m.line" in page