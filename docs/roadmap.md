# Roadmap

The simulator lane ([lane.md](lane.md)) is the first of several. Each lane below is more realistic, and more
expensive per hour, than the one before. All of them belong in this repository, and all of them file issues here.

## Decoder fuzzing (Linux, minutes)

Random bytes into TandemKit's decoders under AddressSanitizer:
- `BTResponseParser.parse`, including messages split across packets
- every message type's `init(cargo:)`
- the history-log parser
- `QualifyingEvent.fromRawBytes`

Seed the corpus from the hex in TandemKit's `Tests/TandemCoreTests`, which comes from pumpX2, and later from pump
captures. A prototype already found a trap: `BTResponseParser` skips its length check for stream and
variable-length messages, so a 4-byte HISTORY_LOG notification reaches an unchecked `cargo[0]`.

## The driver under load on Linux (hosted runners)

TandemKit's virtual-pump build (`-DTANDEMKIT_VIRTUAL_PUMP`) under ThreadSanitizer, against faketandem, with the same
chaos and recovery check as the simulator lane. The host app is replaced by simulated Trio and Loop call patterns.
They must copy the calling thread, not only the call:
- Trio calls `ensureCurrentPumpData` from its delivery queue.
- Loop blocks its queues until a command finishes, and cancels boluses from the main thread.
- Both read status on the main thread and relaunch from saved state.

TSan works on this build: the prototype found two races (`bolusProgressReport` written on the command queue and read
from the telemetry and host threads; the startup reconnect reading `pumpManager` while `init` sets it) and an
overlapping-pairing race on `JpakeAuthBuilder.instance`. It needs no Mac, so every TandemKit PR could get a short
fixed-seed run.
- **Structure:** a Swift package here that depends on TandemKit `dev`. `@testable import` works across packages
  under `swift test`.
- **TandemKit change it needs:** moving `FaketandemClient`, `FaketandemHarness` and the dose-store oracles out of
  `Tests/` into a support target, so this lane and the integration tests share one copy.

## Loop on the simulator

Built: `--host loop` ([lane.md](lane.md#loop---host-loop)), nightly at 06:00, not yet run on a worker. It covers what
Trio does not: upstream LoopKit rather than Trio's loopandlearn fork, the `TandemKitPlugin.loopplugin` build, and
Loop's dosing loop on its CGM Simulator. Loop restores a paired TandemPumpManager from the same
`Documents/PumpManagerState.plist` as Trio; its onboarding and settings are seeded through its app group's
preferences, and trio-test-harness has Loop's build and UI flows (`--app loop`).

## The real Mobi (the phone Mac, daytime)

Trio, and later Loop, on the jailbroken iPhone, with the dedicated test Mobi. Sessions run on the Mac the phone is
plugged into, through trio-test-harness's phone targets. The Mac files the issues; the phone needs no GitHub access.
Only TandemKit commits that already passed the simulator lane go to the phone.

Safety rules come before any code:
- **Serial allowlist.** Check the connected pump's serial against an allowlist in untracked local config before every
  command, so the lane can never drive a pump someone is wearing.
- **The pump's limits are the ceiling.** Set a low max bolus (e.g. 0.5 U) and a minimum basal on the pump itself.
- **Small doses.** Mostly 0.05 U boluses, into a container, with the pump on its charger.
- **Stop for a person.** Stop sending dosing commands and notify a person whenever the pump needs one: an empty
  cartridge, an alarm it cannot acknowledge, an unexpected suspend.
- **No `deleteSavedPumpState`.** Re-pairing needs a person.

Link disruption without faketandem:
- the app's own disconnect
- killing and relaunching the app over SSH
- Bluetooth off and on through two Shortcuts run with `uiopen`, which needs no root
- harsher, with a one-line sudo rule: `killall -9 bluetoothd`, which makes apps see the "resetting" state

ThreadSanitizer does not run on devices, so the build uses AddressSanitizer, if its runtime loads under the
harness's signing.
