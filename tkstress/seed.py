"""Put a freshly installed Trio into the state the stress lane starts from, with no UI: a Tandem pump already paired
with faketandem, closed loop on, the Glucose Simulator as CGM.

Facts about Trio (nightscout/Trio feat/tandem-closed-beta) this relies on:
  * The pump manager is restored from Documents/PumpManagerState.plist, a plist of
    {"managerIdentifier": <the manager's pluginIdentifier>, "state": <its rawState>}
    (Trio/Sources/APS/DeviceDataManager.swift `rawPumpManager`, PersistedProperty, PumpManagerExtensions.swift).
  * Settings are JSON files under Documents/ that decode key by key with defaults for anything missing, and fall back
    to the copies bundled in Trio.app/json/defaults/ when a file is absent (OpenAPS.defaults).

Facts about TandemKit (TandemPumpManagerState / PumpState rawValue):
  * A restored driver with `derivedSecret` + `pincode` authenticates through JPAKE rounds 3/4 only and learns
    `serverNonce` on its first connect (TandemPeripheralManager.performInitialSetup), which is exactly what
    faketandem accepts for a long-term key given over `setLongTermKey`.
  * It reconnects by scanning for `peripheralUuid` and comparing it as a string with
    `CBPeripheral.identifier.uuidString`, which Foundation spells in upper case.
  * `enactTempBasal` refuses everything but a cancel until `hasSyncedBasalRateSchedule` (#443).
"""
from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

TANDEM_PLUGIN_ID = "TandemPumpManager"
PUMP_STATE_FILE = "Documents/PumpManagerState.plist"

# faketandem's default profile is one segment at 0.85 U/hr; Trio's own basal profile and TandemKit's schedule are set
# to the same rate so neither the algorithm nor TandemKit's basal check sees a mismatch that is only the harness's.
BASAL_RATE = 0.85
MAX_BOLUS = 10.0
MAX_BASAL = 2.0

# Simulated devices only (faketandem + Glucose Simulator): values chosen to make Trio command the pump, not therapy.
TRIO_OVERRIDES: dict[str, object] = {
    "freeaps/freeaps_settings.json": {"dosingMode": "closed", "cgm": "simulator", "cgmPluginIdentifier": ""},
    "preferences.json": {"max_iob": 3, "enableSMB_always": True},
    "settings/settings.json": {"maxBolus": MAX_BOLUS, "maxBasal": MAX_BASAL},
    "settings/basal_profile.json": [{"start": "00:00:00", "minutes": 0, "rate": BASAL_RATE}],
}


def host_time_zone() -> str:
    """IANA name of this Mac's zone (the simulator's), for TandemKit's basal schedule; UTC when it cannot be told."""
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return os.environ.get("TZ") or "UTC"


def tandem_pump_manager_raw(*, peripheral_id: str, long_term_key: bytes, pairing_code: str,
                            basal_rate: float = BASAL_RATE, max_bolus: float = MAX_BOLUS,
                            max_basal: float = MAX_BASAL, time_zone: str | None = None) -> dict:
    """The plist Trio restores a paired TandemPumpManager from."""
    return {
        "managerIdentifier": TANDEM_PLUGIN_ID,
        "state": {
            "onboarded": True,
            "pumpState": {
                "peripheralUuid": peripheral_id.upper(),
                "derivedSecret": bytes(long_term_key),
                "pincode": pairing_code,
            },
            "hasSyncedBasalRateSchedule": True,
            "basalRateSchedule": {
                "items": [{"startTime": 0.0, "value": float(basal_rate)}],
                "timeZoneIdentifier": time_zone or host_time_zone(),
            },
            "settings": {"maxBolus": float(max_bolus), "maxTempBasalRate": float(max_basal)},
        },
    }


def write_pump_state(container: Path, raw: dict) -> Path:
    path = container / PUMP_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(raw, fmt=plistlib.FMT_BINARY))
    return path


def merged_settings(rel: str, override: object, *, app: Path | None, container: Path) -> object:
    """`override` laid over what Trio would read for `rel`: the container's copy, else the bundled default."""
    if not isinstance(override, dict):
        return override
    base: dict = {}
    for cand in (container / "Documents" / rel, (app / "json" / "defaults" / rel) if app else None):
        if cand and cand.is_file():
            try:
                loaded = json.loads(cand.read_text())
            except ValueError:
                continue
            if isinstance(loaded, dict):
                base = loaded
                break
    return {**base, **override}


def write_trio_settings(container: Path, *, app: Path | None,
                        overrides: dict[str, object] | None = None) -> list[Path]:
    written = []
    for rel, override in (overrides or TRIO_OVERRIDES).items():
        dest = container / "Documents" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(merged_settings(rel, override, app=app, container=container), indent=2))
        written.append(dest)
    return written


def read_back(container: Path) -> dict:
    """What Trio kept after launching: it rewrites its settings files, so a value it rejected shows up here."""
    out = {}
    for rel, override in TRIO_OVERRIDES.items():
        p = container / "Documents" / rel
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            out[rel] = None
            continue
        out[rel] = {k: data.get(k) for k in override} if isinstance(override, dict) and isinstance(data, dict) else data
    return out
