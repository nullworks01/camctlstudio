import json

import pytest

from camctl import assistant

SETTINGS = [
    {"name": "iso", "value": 800, "label": "800", "writable": True,
     "choices": [{"value": 800, "label": "800"}, {"value": 1600, "label": "1600"}]},
    {"name": "aperture", "value": 350, "label": "f/3.5", "writable": True,
     "choices": [{"value": 350, "label": "f/3.5"}, {"value": 560, "label": "f/5.6"}]},
    {"name": "focus_mode", "value": 2, "label": "AF-S", "writable": False,
     "choices": [{"value": 2, "label": "AF-S"}]},
]


def fake_response(payload, stop="end_turn"):
    return {"model": "claude-opus-5-5", "stop_reason": stop, "content": [
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": json.dumps(payload)}]}


def test_validates_and_labels(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    sent = {}

    def post(body, key):
        sent.update(body)
        return fake_response({"settings": [{"name": "iso", "value": 1600},
                                           {"name": "aperture", "value": 999},
                                           {"name": "aperture", "value": 350}],
                              "explanation": "Brighter.", "tips_for_user": "Add a lamp."})
    monkeypatch.setattr(assistant, "_post", post)
    r = assistant.ask(b"\xff\xd8jpg", SETTINGS, "")
    assert r["changes"] == [{"name": "iso", "value": 1600, "label": "1600", "from_label": "800"}]
    assert r["dropped"] == ["aperture=999"]
    assert r["tips"] == "Add a lamp."
    # only writable settings are offered to the model
    enum = sent["output_config"]["format"]["schema"]["properties"]["settings"]["items"]["properties"]["name"]["enum"]
    assert enum == ["iso", "aperture"]
    assert sent["fallbacks"] == "default" and "temperature" not in sent


def test_refusal_and_missing_key(monkeypatch):
    monkeypatch.setattr(assistant, "_key", lambda: "")
    with pytest.raises(assistant.AskError, match="NO_KEY"):
        assistant.ask(b"", SETTINGS, "")
    monkeypatch.setattr(assistant, "_key", lambda: "test")
    monkeypatch.setattr(assistant, "_post", lambda b, k: fake_response({}, stop="refusal"))
    with pytest.raises(assistant.AskError, match="declined"):
        assistant.ask(b"", SETTINGS, "")
