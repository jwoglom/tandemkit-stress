"""The chaos the stress lane throws at TandemKit while its host app (Trio or Loop) runs: pump-side events, link faults,
app lifecycle, UI.

A seeded `random.Random` picks every action and its parameters, so a seed reproduces the plan. It does not
reproduce the interleaving: whether an action applies depends on live state (a bolus already running, no session
to send an event to), and thread scheduling is the point of the exercise.

Anything an action turns off (a suspend, the pump's radio, a clock skew, a killed app) gets an undo scheduled for
later, and `quiesce()` runs every pending undo at once and clears the rest, so each round's recovery check starts
from a pump that should be reachable.
"""
from __future__ import annotations

import datetime as dt
import heapq
import itertools
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from harness.util import HarnessError

# Requests TandemKit sends (TandemPumpManager*, QualifyingEventFetch), as faketandem names them in fault scopes.
READ_REQUESTS = [
    "CurrentBasalStatusRequest", "CurrentBolusStatusRequest", "InsulinStatusRequest", "CurrentBatteryV2Request",
    "LastBolusStatusV2Request", "LastBolusStatusV3Request", "TimeSinceResetRequest", "HistoryLogStatusRequest",
    "HistoryLogRequest", "CGMStatusV2Request", "AlertStatusRequest", "AlarmStatusRequest", "TempRateRequest",
    "PumpGlobalsRequest",
]
CONTROL_REQUESTS = [
    "SetTempRateRequest", "StopTempRateRequest", "BolusPermissionRequest", "InitiateBolusRequest",
    "CancelBolusRequest", "SuspendPumpingRequest", "ResumePumpingRequest", "SetMaxBolusLimitRequest",
    "SetMaxBasalLimitRequest",
]
# error_response is applied after faketandem's handler ran (TandemKit docs/integration-harness.md, the open D3 gap), so
# on InitiateBolus it would describe a pump that refused a bolus it is delivering: left out.
ERRORABLE_REQUESTS = READ_REQUESTS + ["SetTempRateRequest", "StopTempRateRequest", "BolusPermissionRequest"]

# Qualifying-event bits (TandemCore QualifyingEvent.id) whose suggested handlers only read status.
QE = {"alert": 1, "homeScreenChange": 32, "basalChange": 512, "bolusChange": 1024, "iobChange": 2048,
      "profileChange": 8192, "cgmChange": 32768, "battery": 65536, "remainingInsulin": 262144,
      "timeChange": 256, "controlIQInfo": 4194304, "globalPumpSettings": 16777216}
BENIGN_QE = ["alert", "homeScreenChange", "basalChange", "bolusChange", "iobChange", "profileChange", "battery",
             "remainingInsulin"]

INTENSITY_GAPS = {"low": (10.0, 40.0), "medium": (3.0, 15.0), "high": (0.5, 5.0)}


class Skip(Exception):
    """The action does not apply right now (no session, a bolus already running, ...)."""


@dataclass(order=True)
class _Pending:
    due: float
    seq: int
    name: str = field(compare=False)
    fn: Callable[[], object] = field(compare=False)


class ChaosContext:
    """What actions act on. `app` (app.SimApp) and `ui` (a Driver with WebDriverAgent) are optional:
    without them the app and UI actions are not offered. `host` (tkstress/hosts.py) picks the UI actions, and
    `ui_flows` is that app's harness flows module."""

    def __init__(self, rng: random.Random, pump, control, app=None, ui=None, ui_flows=None, host: str = "trio"):
        self.rng, self.pump, self.control, self.app, self.ui, self.ui_flows = rng, pump, control, app, ui, ui_flows
        self.host = host
        self._pending: list[_Pending] = []
        self._seq = itertools.count()

    def later(self, delay: float, name: str, fn: Callable[[], object]) -> None:
        heapq.heappush(self._pending, _Pending(time.time() + delay, next(self._seq), name, fn))

    def pop_due(self, now: float | None = None) -> list[_Pending]:
        now = time.time() if now is None else now
        out = []
        while self._pending and self._pending[0].due <= now:
            out.append(heapq.heappop(self._pending))
        return out

    def pop_all(self) -> list[_Pending]:
        out = sorted(self._pending)
        self._pending.clear()
        return out

    def qualifying_event(self, *names: str) -> int:
        mask = 0
        for n in names:
            mask |= QE[n]
        try:
            self.pump.qualifying_event(mask)
        except HarnessError as e:
            if "409" in str(e):            # no central attached: nothing to notify
                raise Skip("no session to notify") from None
            raise
        return mask


@dataclass
class Action:
    name: str
    category: str
    weight: float
    fn: Callable[[ChaosContext], dict]
    hosts: tuple[str, ...] | None = None        # the host apps it applies to; None: any


# ---------------- pump side -----------------

def _snap(ctx: ChaosContext) -> dict:
    return ctx.pump.snapshot()


def pump_bolus(ctx: ChaosContext) -> dict:
    s = _snap(ctx)
    if s["bolus"]["active"]:
        raise Skip("a bolus is already running")
    if s["basal"]["suspended"]:
        raise Skip("pump suspended")
    units = ctx.rng.choice([0.05, 0.1, 0.25, 0.5, 1.0, 2.0])
    # "remote" is the app's source id: a bolus TandemKit did not program but history says the app did (a lost ack)
    source = ctx.rng.choices(["pump", "remote"], weights=[4, 1])[0]
    r = ctx.pump.start_bolus(units, source=source)
    return {"units": units, "source": source, "bolus_id": r.get("bolus_id")}


def pump_bolus_abort(ctx: ChaosContext) -> dict:
    if not _snap(ctx)["bolus"]["active"]:
        raise Skip("no bolus running")
    ctx.pump.abort_bolus()
    return {}


def pump_suspend(ctx: ChaosContext) -> dict:
    if _snap(ctx)["basal"]["suspended"]:
        raise Skip("already suspended")
    reason = ctx.rng.choice(["user", "user", "occlusion", "alarm"])
    ctx.pump.suspend(reason)
    after = ctx.rng.uniform(20, 150)
    ctx.later(after, "pump_resume", lambda: ctx.pump.resume(clear_alarms=True))
    return {"reason": reason, "resume_after_s": round(after)}


def pump_temp_basal(ctx: ChaosContext) -> dict:
    if _snap(ctx)["basal"]["temp_active"]:
        raise Skip("temp basal already running")
    # not 0: faketandem's body has `percent,omitempty`, so 0% reads as "no percent given" and is refused
    percent, minutes = ctx.rng.choice([10, 50, 150, 200]), ctx.rng.choice([15, 30, 60])
    ctx.pump.start_temp_basal(percent, minutes)
    after = ctx.rng.uniform(30, 240)
    ctx.later(after, "pump_temp_basal_stop", ctx.pump.stop_temp_basal)
    return {"percent": percent, "minutes": minutes, "stop_after_s": round(after)}


def qe_burst(ctx: ChaosContext) -> dict:
    n = ctx.rng.randint(2, 12)
    masks = []
    for _ in range(n):
        names = ctx.rng.sample(BENIGN_QE, k=ctx.rng.randint(1, 3))
        masks.append(ctx.qualifying_event(*names))
        time.sleep(ctx.rng.uniform(0, 0.3))
    return {"events": n, "masks": masks}


def battery_change(ctx: ChaosContext) -> dict:
    pct = ctx.rng.randint(3, 100)
    ctx.pump.set_state(battery_percent=pct)
    ctx.qualifying_event("battery")
    return {"battery_percent": pct}


def reservoir_change(ctx: ChaosContext) -> dict:
    units = round(ctx.rng.uniform(2, 300), 1)
    ctx.pump.set_state(reservoir_units=units)
    ctx.qualifying_event("remainingInsulin")
    return {"reservoir_units": units}


def clock_skew(ctx: ChaosContext) -> dict:
    offset = ctx.rng.choice([-900, -300, -60, 60, 300, 900])
    ctx.pump.set_clock(pump_offset_seconds=offset)
    after = ctx.rng.uniform(60, 300)

    def revert():
        ctx.pump.set_clock(pump_offset_seconds=0)
        try:
            ctx.qualifying_event("timeChange")
        except Skip:
            pass
    ctx.later(after, "clock_skew_revert", revert)
    try:
        ctx.qualifying_event("timeChange")
    except Skip:
        pass
    return {"pump_offset_seconds": offset, "revert_after_s": round(after)}


def control_iq_flip(ctx: ChaosContext) -> dict:
    ctx.pump.set_state(closed_loop_enabled=True)
    after = ctx.rng.uniform(60, 180)

    def revert():
        ctx.pump.set_state(closed_loop_enabled=False)
        try:
            ctx.qualifying_event("controlIQInfo")
        except Skip:
            pass
    ctx.later(after, "control_iq_off", revert)
    try:
        ctx.qualifying_event("controlIQInfo")
    except Skip:
        pass
    return {"revert_after_s": round(after)}


# ---------------- link -----------------

def drop_response(ctx: ChaosContext) -> dict:
    spec = {"kind": "drop_response", "message": ctx.rng.choice(READ_REQUESTS + CONTROL_REQUESTS),
            "count": ctx.rng.randint(1, 2)}
    ctx.pump.arm_fault(spec)
    return spec


def delay_response(ctx: ChaosContext) -> dict:
    spec = {"kind": "delay_response", "message": ctx.rng.choice(READ_REQUESTS + CONTROL_REQUESTS),
            "delay_ms": ctx.rng.choice([250, 1000, 3000, 8000])}
    ctx.pump.arm_fault(spec)
    return spec


def error_response(ctx: ChaosContext) -> dict:
    spec = {"kind": "error_response", "message": ctx.rng.choice(ERRORABLE_REQUESTS)}
    ctx.pump.arm_fault(spec)
    return spec


def disconnect_mid_response(ctx: ChaosContext) -> dict:
    spec = {"kind": "disconnect", "after": "partial_response", "fragments_sent": 1}
    if ctx.rng.random() < 0.5:
        spec["message"] = ctx.rng.choice(READ_REQUESTS)
    ctx.pump.arm_fault(spec)
    return spec


def disconnect_after_request(ctx: ChaosContext) -> dict:
    spec = {"kind": "disconnect", "after": "request", "message": ctx.rng.choice(READ_REQUESTS + CONTROL_REQUESTS)}
    ctx.pump.arm_fault(spec)
    return spec


def drop_fragment(ctx: ChaosContext) -> dict:
    spec = {"kind": "drop_fragment", "every_nth": ctx.rng.choice([2, 3, 4]), "count": ctx.rng.randint(1, 3)}
    ctx.pump.arm_fault(spec)
    return spec


def radio_flap(ctx: ChaosContext) -> dict:
    ctx.pump.arm_fault({"kind": "radio_off"})
    after = ctx.rng.uniform(2, 90)
    ctx.later(after, "radio_on", lambda: ctx.pump.arm_fault({"kind": "radio_on"}))
    return {"radio_on_after_s": round(after)}


def pump_drops_link(ctx: ChaosContext) -> dict:
    ctx.control.disconnect_pump()
    return {}


def mtu_change(ctx: ChaosContext) -> dict:
    mtu = ctx.rng.choice([23, 185, 247])
    ctx.pump.set_att_mtu(mtu)
    return {"att_mtu": mtu, "note": "applies from the next connection"}


# ---------------- app -----------------

def _need_app(ctx: ChaosContext) -> None:
    if not ctx.app.alive():
        raise Skip("app not running (a relaunch is pending)")


def app_kill(ctx: ChaosContext) -> dict:
    _need_app(ctx)
    ctx.app.kill()
    after = ctx.rng.uniform(1, 10)
    ctx.later(after, "app_relaunch", ctx.app.launch)
    return {"relaunch_after_s": round(after, 1)}


def app_terminate(ctx: ChaosContext) -> dict:
    _need_app(ctx)
    ctx.app.terminate()
    after = ctx.rng.uniform(1, 5)
    ctx.later(after, "app_relaunch", ctx.app.launch)
    return {"relaunch_after_s": round(after, 1)}


def app_background(ctx: ChaosContext) -> dict:
    _need_app(ctx)
    ctx.ui.home()
    after = ctx.rng.uniform(3, 45)
    ctx.later(after, "app_foreground", ctx.app.foreground)
    return {"foreground_after_s": round(after)}


# ---------------- UI -----------------

def ui_pump_settings(ctx: ChaosContext) -> dict:
    """Settings ▸ Devices ▸ Insulin Pump: TandemKit's own settings screen, which reads pump state while it is open."""
    F = ctx.ui_flows
    try:
        F.open_settings(ctx.ui)
        for label in ("Devices", "Insulin Pump"):
            ctx.ui.tap_text(label)
            time.sleep(1.2)
        time.sleep(ctx.rng.uniform(1, 5))
    except LookupError as e:
        raise Skip(f"navigation: {e}") from None
    finally:
        F.reset_to_home(ctx.ui)
    return {}


def ui_tab_hop(ctx: ChaosContext) -> dict:
    tab = ctx.rng.choice(["History", "Main", "Adjustments"])
    try:
        ctx.ui_flows.settle(ctx.ui, rounds=2)      # pump alarms raise alerts in Trio that would take the tap
        ctx.ui.tap_text(tab, exact=True, ocr_fallback=False)
    except LookupError as e:
        raise Skip(f"no {tab} tab: {e}") from None
    return {"tab": tab}


def ui_loop_pump_settings(ctx: ChaosContext) -> dict:
    """Loop's Settings ▸ the pump ("Tandem mobi", TandemPumpManager.localizedTitle): TandemKit's own settings screen,
    which reads pump state while it is open."""
    F = ctx.ui_flows
    try:
        F.open_settings(ctx.ui)
        ctx.ui.tap_text("Tandem")
        time.sleep(ctx.rng.uniform(1, 5))
    except LookupError as e:
        raise Skip(f"navigation: {e}") from None
    finally:
        F.reset_to_home(ctx.ui)
    return {}


def ui_chart_hop(ctx: ChaosContext) -> dict:
    """One of Loop's status charts, opened and closed: Active Insulin and Insulin Delivery list the doses DoseStore holds
    from the driver, Glucose the prediction."""
    chart = ctx.rng.choice(["Active Insulin", "Insulin Delivery", "Glucose"])
    try:
        ctx.ui_flows.settle(ctx.ui, rounds=2)
        ctx.ui.tap_text(chart, exact=True, ocr_fallback=False)
        time.sleep(ctx.rng.uniform(1, 4))
    except LookupError as e:
        raise Skip(f"no {chart} chart: {e}") from None
    finally:
        ctx.ui_flows.reset_to_home(ctx.ui)
    return {"chart": chart}


ACTIONS = [
    Action("pump_bolus", "pump", 3, pump_bolus),
    Action("pump_bolus_abort", "pump", 1, pump_bolus_abort),
    Action("pump_suspend", "pump", 1.5, pump_suspend),
    Action("pump_temp_basal", "pump", 1, pump_temp_basal),
    Action("qe_burst", "pump", 3, qe_burst),
    Action("battery_change", "pump", 0.7, battery_change),
    Action("reservoir_change", "pump", 0.7, reservoir_change),
    Action("clock_skew", "pump", 0.5, clock_skew),
    Action("control_iq_flip", "pump", 0.3, control_iq_flip),
    Action("drop_response", "link", 2, drop_response),
    Action("delay_response", "link", 1.5, delay_response),
    Action("error_response", "link", 0.7, error_response),
    Action("disconnect_mid_response", "link", 1, disconnect_mid_response),
    Action("disconnect_after_request", "link", 1, disconnect_after_request),
    Action("drop_fragment", "link", 1, drop_fragment),
    Action("radio_flap", "link", 1, radio_flap),
    Action("pump_drops_link", "link", 1, pump_drops_link),
    Action("mtu_change", "link", 0.5, mtu_change),
    Action("app_kill", "app", 1, app_kill),
    Action("app_terminate", "app", 0.3, app_terminate),
    Action("app_background", "app", 1, app_background),
    Action("ui_pump_settings", "ui", 0.8, ui_pump_settings, hosts=("trio",)),
    Action("ui_tab_hop", "ui", 0.8, ui_tab_hop, hosts=("trio",)),
    Action("ui_loop_pump_settings", "ui", 0.8, ui_loop_pump_settings, hosts=("loop",)),
    Action("ui_chart_hop", "ui", 0.8, ui_chart_hop, hosts=("loop",)),
]


class Chaos:
    def __init__(self, ctx: ChaosContext, *, intensity: str = "medium", categories: set[str] | None = None,
                 log_path: Path | None = None, actions: list[Action] | None = None):
        self.ctx = ctx
        self.gaps = INTENSITY_GAPS[intensity]
        wanted = set(categories or {"pump", "link", "app", "ui"})
        if ctx.app is None:
            wanted.discard("app")
        if ctx.ui is None:
            wanted.discard("ui")
        # backgrounding goes through the UI driver's home button
        self.actions = [a for a in (actions or ACTIONS) if a.category in wanted
                        and (a.hosts is None or ctx.host in a.hosts)
                        and not (a.name == "app_background" and ctx.ui is None)]
        if not self.actions:
            raise HarnessError(f"no chaos actions left for categories {sorted(wanted)}")
        self.log_path = log_path
        self.round = 0
        self.counts: dict[str, int] = {}

    def _record(self, name: str, category: str, result: str, params: dict | None = None, detail: str = "") -> dict:
        rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "round": self.round, "action": name,
               "category": category, "result": result, "params": params or {}, "detail": detail[:500]}
        self.counts[f"{name}:{result}"] = self.counts.get(f"{name}:{result}", 0) + 1
        if self.log_path:
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        return rec

    def _run(self, name: str, category: str, fn: Callable[[], object]) -> dict:
        try:
            out = fn()
            return self._record(name, category, "ok", out if isinstance(out, dict) else {})
        except Skip as e:
            return self._record(name, category, "skipped", detail=str(e))
        except Exception as e:  # noqa: BLE001 - a failed action is recorded, never fatal to the run
            return self._record(name, category, "error", detail=f"{type(e).__name__}: {e}")

    def run_due(self) -> list[dict]:
        return [self._run(p.name, "undo", p.fn) for p in self.ctx.pop_due()]

    def step(self) -> list[dict]:
        """Due undos, then one action picked by weight. Returns what was recorded."""
        recs = self.run_due()
        a = self.ctx.rng.choices(self.actions, weights=[x.weight for x in self.actions])[0]
        recs.append(self._run(a.name, a.category, lambda: a.fn(self.ctx)))
        return recs

    def gap(self) -> float:
        return self.ctx.rng.uniform(*self.gaps)

    def run_for(self, seconds: float, *, should_stop: Callable[[], bool] = lambda: False) -> list[dict]:
        recs: list[dict] = []
        end = time.time() + seconds
        while time.time() < end and not should_stop():
            recs += self.step()
            pause_end = min(end, time.time() + self.gap())
            while time.time() < pause_end and not should_stop():
                recs += self.run_due()
                time.sleep(min(0.5, max(0.0, pause_end - time.time())))
        return recs

    def quiesce(self) -> list[dict]:
        """Every pending undo now, then a pump that should be reachable: no faults, radio on, no skew, not suspended."""
        recs = [self._run(p.name, "undo", p.fn) for p in self.ctx.pop_all()]
        pump = self.ctx.pump

        def reset():
            pump.clear_faults()
            pump.arm_fault({"kind": "radio_on"})
            pump.set_clock(pump_offset_seconds=0)
            pump.set_state(closed_loop_enabled=False)
            if pump.snapshot()["basal"]["suspended"]:
                pump.resume(clear_alarms=True)
        recs.append(self._run("quiesce_pump", "quiesce", reset))
        if self.ctx.app is not None:
            recs.append(self._run("quiesce_app", "quiesce", self.ctx.app.foreground))
        return recs
