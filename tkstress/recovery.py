"""Did the driver come back? Judged from the pump's side only, so it needs nothing from the app.

After a round's faults are undone (Chaos.quiesce), a healthy TandemKit reconnects on its own, re-authenticates, and
answers a qualifying event with the request its suggested handler names. Each step is checked against faketandem's
snapshot and request log:

  1. session: the emulator reports a connected, authenticated central
  2. probe:   a `remainingInsulin` event (read-only: its handler is InsulinStatusRequest) gets that request back

A link that is up with no working session (TandemKit 7e58aac: a disconnect failed the connect that followed it, and
the driver held a socket nobody authenticated on until its connect timeout) fails step 1 or 2; a wedged command
queue fails step 2.
"""
from __future__ import annotations

import time

from harness.util import HarnessError

PROBE_BITMASK = 262144                      # QualifyingEvent.remainingInsulin
PROBE_REQUEST = "InsulinStatusRequest"


def session_state(pump) -> dict:
    s = pump.snapshot()
    return {"connected": s["connection"]["connected"], "authenticated": s["auth"]["authenticated"],
            "radio_enabled": s["connection"]["radio_enabled"]}


def wait_for_session(pump, timeout: float, poll: float = 1.0) -> tuple[bool, dict]:
    deadline = time.time() + timeout
    st: dict = {}
    while True:
        try:
            st = session_state(pump)
            if st["connected"] and st["authenticated"]:
                return True, st
        except HarnessError as e:
            st = {"error": str(e)}
        if time.time() >= deadline:
            return False, st
        time.sleep(poll)


def probe(pump, timeout: float, poll: float = 0.5) -> tuple[bool, dict]:
    mark = pump.request_log()["last_seq"]
    try:
        pump.qualifying_event(PROBE_BITMASK)
    except HarnessError as e:
        return False, {"error": f"could not send the probe event: {e}"}
    sent = time.time()
    while time.time() - sent < timeout:
        for e in pump.request_log(since=mark).get("entries", []):
            if e.get("seq", 0) > mark and e.get("kind") == "request" and e.get("message") == PROBE_REQUEST:
                return True, {"answered_after_s": round(time.time() - sent, 1)}
        time.sleep(poll)
    return False, {"waited_s": timeout}


def check(pump, *, session_timeout: float = 120.0, probe_timeout: float = 60.0) -> dict:
    """{"ok", "stage", "seconds", ...}. A probe that times out right after a reconnect gets one more try, since a
    reconnect in between drops the event the pump sent on the old link."""
    t0 = time.time()
    ok, st = wait_for_session(pump, session_timeout)
    if not ok:
        return {"ok": False, "stage": "session", "seconds": round(time.time() - t0, 1), "state": st}
    for attempt in (1, 2):
        ok, info = probe(pump, probe_timeout)
        if ok:
            return {"ok": True, "stage": "probe", "seconds": round(time.time() - t0, 1), **info, "attempts": attempt}
        ok, st = wait_for_session(pump, 15)
        if not ok:
            return {"ok": False, "stage": "session", "seconds": round(time.time() - t0, 1), "state": st}
    return {"ok": False, "stage": "probe", "seconds": round(time.time() - t0, 1), **info}
