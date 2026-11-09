"""Did the driver come back? Judged from the pump's side only, so it needs nothing from the app.

After a round's faults are undone (Chaos.quiesce), a healthy TandemKit reconnects on its own, re-authenticates, and
answers a qualifying event with the request its suggested handler names. Each step is checked against faketandem's
snapshot and request log:

  1. session:      the emulator reports a connected, authenticated central
  2. subscription: that central has subscribed to QUALIFYING_EVENTS again (faketandem drops a notification sent
                   before it has; a faketandem that does not report subscriptions skips this step)
  3. probe:        a `remainingInsulin` event (read-only: its handler is InsulinStatusRequest) gets that request back

A link that is up with no working session (TandemKit 7e58aac: a disconnect failed the connect that followed it, and
the driver held a socket nobody authenticated on until its connect timeout) fails step 1 or 3; a wedged command
queue fails step 3. A central that connects and never subscribes fails step 2.
"""
from __future__ import annotations

import time

from harness.util import HarnessError

PROBE_BITMASK = 262144                      # QualifyingEvent.remainingInsulin
PROBE_REQUEST = "InsulinStatusRequest"
SUBSCRIPTION = "QUALIFYING_EVENTS"
NOT_SUBSCRIBED = ("not subscribed", "no notifier registered")     # the second is an older faketandem's text for both causes


def session_state(pump) -> dict:
    s = pump.snapshot()
    st = {"connected": s["connection"]["connected"], "authenticated": s["auth"]["authenticated"],
          "radio_enabled": s["connection"]["radio_enabled"]}
    if "subscribed" in s["connection"]:
        st["subscribed"] = s["connection"]["subscribed"]
    return st


def wait_for_session(pump, timeout: float, poll: float = 1.0) -> tuple[bool, dict]:
    """Waits for a connected, authenticated central that has subscribed to QUALIFYING_EVENTS when faketandem reports
    subscriptions. The returned state says how long each condition took (`session_after_s`, `subscribed_after_s`)
    and, when it is False, which one was still missing (`waiting_for`)."""
    t0 = time.time()
    deadline = t0 + timeout
    st: dict = {}
    seen: dict = {}
    while True:
        try:
            st = session_state(pump)
            up = st["connected"] and st["authenticated"]
            if up:
                seen.setdefault("session_after_s", round(time.time() - t0, 1))
            subscribed = "subscribed" not in st or SUBSCRIPTION in st["subscribed"]
            if up and subscribed:
                if "subscribed" in st:
                    seen.setdefault("subscribed_after_s", round(time.time() - t0, 1))
                return True, {**st, **seen}
            st["waiting_for"] = "connected+authenticated" if not up else f"{SUBSCRIPTION} subscription"
        except HarnessError as e:
            st = {"error": str(e)}
        if time.time() >= deadline:
            return False, {**st, **seen}
        time.sleep(poll)


def probe(pump, timeout: float, poll: float = 0.5) -> tuple[bool, dict]:
    """Raises the event, retrying for up to `timeout` while faketandem says the central has not subscribed yet, then
    waits up to `timeout` for the request the event names."""
    mark = pump.request_log()["last_seq"]
    began = time.time()
    retries = 0
    while True:
        try:
            pump.qualifying_event(PROBE_BITMASK)
            break
        except HarnessError as e:
            msg = str(e)
            if not any(t in msg for t in NOT_SUBSCRIBED) or time.time() - began >= timeout:
                return False, {"error": f"could not send the probe event: {msg}", "send_attempts": retries + 1,
                               "send_waited_s": round(time.time() - began, 1)}
            retries += 1
            time.sleep(poll)
    sent = time.time()
    while time.time() - sent < timeout:
        for e in pump.request_log(since=mark).get("entries", []):
            if e.get("seq", 0) > mark and e.get("kind") == "request" and e.get("message") == PROBE_REQUEST:
                return True, {"answered_after_s": round(time.time() - sent, 1), "send_retries": retries}
        time.sleep(poll)
    return False, {"waited_s": timeout, "send_retries": retries}


def check(pump, *, session_timeout: float = 120.0, probe_timeout: float = 60.0) -> dict:
    """{"ok", "stage", "seconds", "waited", ...}. `waited` lists what each step waited on and for how long, so an
    issue shows where the time went. A probe that times out right after a reconnect gets one more try, since a
    reconnect in between drops the event the pump sent on the old link."""
    t0 = time.time()
    waited: list[dict] = []

    def done(ok: bool, stage: str, **extra) -> dict:
        return {"ok": ok, "stage": stage, "seconds": round(time.time() - t0, 1), "waited": waited, **extra}

    def wait(timeout: float):
        t = time.time()
        ok, st = wait_for_session(pump, timeout)
        waited.append({"step": "session", "waited_for": ("connected+authenticated"
                       + (f"+{SUBSCRIPTION}" if "subscribed_after_s" in st or "subscribed" in st else "")),
                       "seconds": round(time.time() - t, 1), "ok": ok})
        return ok, st

    def session_failure(st: dict) -> dict:
        stage = "subscription" if st.get("waiting_for", "").startswith(SUBSCRIPTION) else "session"
        return done(False, stage, state=st)

    ok, st = wait(session_timeout)
    if not ok:
        return session_failure(st)
    for attempt in (1, 2):
        t = time.time()
        ok, info = probe(pump, probe_timeout)
        waited.append({"step": "probe", "waited_for": f"{PROBE_REQUEST} after a qualifying event",
                       "seconds": round(time.time() - t, 1), "ok": ok})
        if ok:
            return done(True, "probe", **info, attempts=attempt)
        ok, st = wait(15)
        if not ok:
            return session_failure(st)
    return done(False, "probe", **info)
