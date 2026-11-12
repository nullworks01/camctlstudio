"""Usage meter: token counts per feature/day/shoot from the API's usage field; nothing else stored."""
import json

from camctl import assistant


def test_records_tokens_per_feature(monkeypatch, tmp_path):
    monkeypatch.setattr(assistant.usage, "file", tmp_path / "usage.json")

    class R:
        def __init__(self, d):
            self.d = d

        def read(self):
            return json.dumps(self.d).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    resp = {"content": [], "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 50}}
    monkeypatch.setattr(assistant.urllib.request, "urlopen", lambda req, timeout=0: R(resp))
    with assistant.feature("shot list"):
        assistant._post({"x": 1}, "k")
    assistant._post({"x": 1}, "k")
    t = assistant.usage.today()
    assert t["total"] == {"calls": 2, "input": 2500, "output": 600}
    assert t["features"]["shot list"]["calls"] == 1 and t["features"]["other"]["input"] == 1250
    text = (tmp_path / "usage.json").read_text()
    assert '"k"' not in text and "x-api-key" not in text                     # only counts are stored
