"""Pick the best take: local rating first (sharpness, eyes open), then one Claude call for the top few.

The local part is free and runs only when he asks. Nothing is deleted or moved: this only names takes.
"""
from __future__ import annotations

import base64

import numpy as np

try:
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None

_EYES = None


def _eyes():
    global _EYES
    if _EYES is None and cv2 is not None:
        _EYES = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml")
    return _EYES


def rate(jpeg: bytes, face=None) -> dict:
    """{'sharp': Laplacian variance on the subject, 'eyes': 0/1/2 eyes found open, 'face': bool, 'score': 0..1-ish}.
    face = (x, y, w, h) as 0..1, or None to find it."""
    if cv2 is None:
        raise RuntimeError("OpenCV is not available")
    im = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if im is None:
        raise ValueError("cannot read the picture")
    H, W = im.shape[:2]
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    if face is None:
        try:
            from PIL import Image
            import io
            from . import measure
            pim = Image.open(io.BytesIO(jpeg)).convert("RGB")
            small = pim.resize((512, max(1, round(pim.height * 512 / pim.width))))
            found = measure._faces_cv(small) or []
            if found:
                x, y, w, h = max(found, key=lambda f: f[2] * f[3])
                face = (x / small.width, y / small.height, w / small.width, h / small.height)
        except Exception:  # noqa: BLE001
            face = None
    if face:
        x0, y0 = int(face[0] * W), int(face[1] * H)
        x1, y1 = max(x0 + 8, int((face[0] + face[2]) * W)), max(y0 + 8, int((face[1] + face[3]) * H))
        roi = g[y0:y1, x0:x1]
    else:
        roi = g[H // 4: 3 * H // 4, W // 4: 3 * W // 4]
    sharp = float(cv2.Laplacian(roi, cv2.CV_64F).var())
    eyes = 0
    if face is not None and roi.size and _eyes() is not None:
        top = roi[: max(8, roi.shape[0] * 3 // 5)]
        big = cv2.resize(top, None, fx=max(1.0, 160 / max(1, top.shape[1])), fy=max(1.0, 160 / max(1, top.shape[1])))
        eyes = min(2, len(_eyes().detectMultiScale(cv2.equalizeHist(big), 1.1, 5, minSize=(12, 12))))
    return {"sharp": round(sharp, 1), "eyes": eyes, "face": face is not None}


def rank(items: list[tuple[int, bytes]]) -> list[dict]:
    """items: (take id, jpeg). Returns ratings sorted best first, with a 0..1 score and plain words."""
    rated = [{"id": tid, **rate(jpg)} for tid, jpg in items]
    top = max((r["sharp"] for r in rated), default=1.0) or 1.0
    for r in rated:
        s = r["sharp"] / top
        eye = 1.0 if (not r["face"] or r["eyes"] >= 2) else 0.75 if r["eyes"] == 1 else 0.45
        r["score"] = round(s * eye, 3)
        words = ["sharp" if s >= 0.8 else "a little soft" if s >= 0.5 else "soft"]
        if r["face"]:
            words.append("eyes open" if r["eyes"] >= 2 else "eyes maybe closed")
        r["words"] = ", ".join(words)
    return sorted(rated, key=lambda r: -r["score"])


BEST_SYSTEM = (
    "You are a film director choosing between takes of the same shot. Each take is one still frame. "
    "The takes shown are already the sharpest ones with eyes open. Choose the best one or two by expression, "
    "gesture, eye line and energy. Answer in plain, short sentences a non-technical person understands."
)


def ask_claude(cands: list[tuple[int, bytes, dict]]) -> dict:
    """One Claude call: cands = (take id, jpeg, local rating). Returns {'best': [ids], 'why': str}."""
    from . import assistant
    key = assistant._key()
    if not key:
        raise assistant.AskError("NO_KEY")
    content = []
    for tid, jpg, r in cands:
        content += [{"type": "text", "text": f"Take {tid} (local check: {r['words']}):"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(jpg).decode()}}]
    ids = [c[0] for c in cands]
    schema = {"type": "object", "additionalProperties": False, "required": ["best", "why"],
              "properties": {"best": {"type": "array", "items": {"type": "integer"}}, "why": {"type": "string"}}}
    body = {"model": assistant.MODEL, "max_tokens": 4000, "system": BEST_SYSTEM,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}}, "fallbacks": "default",
            "messages": [{"role": "user", "content": content + [{"type": "text", "text": f"Choose from takes {ids}. Best first."}]}]}
    out = assistant._json_answer(assistant.call(body, key))
    best = [i for i in out.get("best", []) if i in ids][:2] or ids[:1]
    return {"best": best, "why": str(out.get("why", ""))[:600]}
