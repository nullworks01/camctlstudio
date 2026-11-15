"""The camera's last answers, for the "Check this camera" report: which operation Studio sent and the camera's answer
code and size. Never the data itself (no pictures, no names); HTTP cameras clean their answers in camctl/report.py.
Only records: what is sent to the camera and what comes back are not changed."""
from __future__ import annotations

import collections


def note(driver, ask: str, answer: str) -> None:
    log = getattr(driver, "replies", None)
    if log is None:
        log = driver.replies = collections.deque(maxlen=60)
    if not any(r["ask"] == ask and r["answer"] == answer for r in log):     # the same question and answer once only
        log.append({"ask": ask, "answer": answer})


def record_ptp(driver, client) -> None:
    """Wrap client.transaction so every PTP operation and its response code is noted on the driver."""
    if getattr(driver, "replies", None) is None:
        driver.replies = collections.deque(maxlen=60)
    inner = client.transaction

    def transaction(opcode, params=(), data_out=None):
        ask = f"0x{opcode:04X}" + "".join(f" 0x{p & 0xFFFFFFFF:X}" for p in list(params)[:3])
        try:
            r = inner(opcode, params, data_out=data_out) if data_out is not None else inner(opcode, params)
        except Exception as exc:
            note(driver, ask, f"no answer {type(exc).__name__}")
            raise
        note(driver, ask, f"0x{r.code:04X} {len(r.data or b'')} bytes")
        return r
    try:
        client.transaction = transaction
    except AttributeError:
        pass
