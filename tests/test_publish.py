"""Nothing personal is ever published: the guard passes on what would go out, and catches invented personal data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import publish  # noqa: E402


def test_what_would_be_published_is_clean():
    assert publish.check() == []


def test_never_selected():
    rel = {p.relative_to(publish.ROOT).as_posix() for p in publish.files()}
    assert not any(r.startswith(("logs/", "captures/", "screenshots/")) or r.endswith(".bak") or r == "AUDIT.md" for r in rel)
    pics = {r for r in rel if r.lower().endswith((".jpg", ".png", ".arw", ".ico"))}
    assert pics <= publish.IMAGES_OK


def test_the_guard_catches_invented_personal_data():
    j = "".join                                          # built at runtime so this file passes the guard itself
    samples = {
        "user path": j(["C:", "\\", "Us", "ers", "\\", "somebody", "\\", "Pictures"]),
        "home network address": "camera at " + j(["192", ".168", ".7.41"]),
        "MAC address": j(["aa:bb", ":cc:dd", ":ee:ff"]),
        "Claude key": j(["sk-", "ant-", "api03-ABCDEFGHIJKLmnop"]),
        "tunnel / connector URL": j(["https://quiet-river-1234.", "trycloudflare", ".com"]),
    }
    for name, text in samples.items():
        assert publish.PATTERNS[name].search(text), name
    assert not publish.PATTERNS["home network address"].search("Sony access point 192.168.122.1")
    assert publish.PATTERNS["Claude key"].search(("sk-" + "ant-test-") + "x" * 20)        # invented test keys count too
    assert not publish.PATTERNS["Claude key"].search("your-key-here")


def test_copy_writes_gitignore_and_issue_template_without_bom(tmp_path):
    import subprocess
    r = subprocess.run([sys.executable, str(publish.ROOT / "tools" / "publish.py"), "--copy", str(tmp_path / "out")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    for rel in (".gitignore", ".github/ISSUE_TEMPLATE/camera-report.md"):
        data = (tmp_path / "out" / rel).read_bytes()
        assert data and not data.startswith(b"\xef\xbb\xbf"), rel
    tpl = (tmp_path / "out" / ".github/ISSUE_TEMPLATE/camera-report.md").read_text(encoding="utf-8")
    assert tpl.startswith("---")                                   # GitHub reads the header only like this
