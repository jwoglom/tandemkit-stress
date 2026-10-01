# TandemKit stress lane: Trio on the simulator against faketandem

`./tandemkit-stress run` looks for crashes, data races, hangs and failures to recover in TandemKit while it runs inside
Trio, by putting the driver under load and breaking its link on purpose. It builds Trio with its TandemKit
submodule moved to TandemKit `dev` (or any ref or PR), compiled with `TANDEMKIT_VIRTUAL_PUMP` and ThreadSanitizer.
The app runs on the iOS Simulator against [faketandem](https://github.com/jwoglom/faketandem), the Go Tandem pump
emulator. Seeded chaos then runs for the rest of the time budget: pump-side events, link faults, app kills and UI
noise, each round ending in a check that the driver came back. Only simulated devices are involved.

```
./tandemkit-stress run                                     # closed-beta Trio + TandemKit dev, 60 min budget
./tandemkit-stress run --minutes 360                       # the nightly job's length
./tandemkit-stress run --minutes 25 --seed 42 --intensity high
./tandemkit-stress run --tandemkit jwoglom/TandemKit#470   # a TandemKit PR
./tandemkit-stress run --app <Trio.app> --minutes 15       # reuse a virtual-pump build, skip checkout + build
./tandemkit-stress file-issues runs/<run>                  # one GitHub issue per new finding (CI does this)
./tandemkit-stress scan app/launch-03.stderr crashes/*.ips # triage: the findings in sanitizer output / crash reports
./tandemkit-stress faketandem --paired                     # the emulator alone, for poking at it by hand
```

The lane builds on [trio-test-harness](https://github.com/jwoglom/trio-test-harness) (the `trio-test-harness/`
submodule, or `TKSTRESS_HARNESS_DIR`): its source checkout and `xcodebuild`, simulator driver, WebDriverAgent and
Trio UI flows. It keeps its own Trio checkout, DerivedData, build and run directories (see `tkstress/__init__.py`), so
a stress build never invalidates the harness's normal one.

## How a run goes

| Stage | What happens | Code |
|---|---|---|
| faketandem source | Fetched from `FAKETANDEM_REPO`@`--faketandem-ref` and built (or `FAKETANDEM_BIN` + `FAKETANDEM_JAR`), before anything long: a CI job's GitHub App token expires after an hour. | `tkstress/faketandem.py` |
| checkout + build | `prepare_source(branch=--trio, submodules=[TandemKit@--tandemkit])`, then `xcodebuild` with `TANDEMKIT_VIRTUAL_PUMP` and `ENABLE_THREAD_SANITIZER[sdk=iphonesimulator*] = YES` in the harness xcconfig. The sanitizer is set per SDK because Trio's scheme embeds a watchOS app, which the sanitizers do not support. A stress build never touches `build/last-build.json`. Its own xcconfig is `build/override-sim-tandemkit_virtual_pump-sanitized.xcconfig`. The build is stopped at the deadline, so a CI job's limit never kills it without a report. | trio-test-harness `harness/build.py` |
| faketandem | Started on two free loopback ports. Set to `PairStep2` with a random 32-byte long-term key over its websocket. | `tkstress/faketandem.py` |
| simulator | Its own simulator, `TandemKitStress` (`TH_SIM_NAME`), so it never reinstalls the app a trio-test-harness job is using. Fresh install, onboarding pre-seeded, and three files written into the container. `Documents/PumpManagerState.plist` holds a TandemPumpManager already paired with that key. `freeaps/freeaps_settings.json` sets closed loop and the Glucose Simulator. `preferences.json` and `settings/` set max IOB 3, SMB always, and a basal of 0.85 U/hr to match faketandem. Settings overlay the defaults bundled in the built app. | `tkstress/seed.py` |
| launch | `simctl launch --stdout/--stderr=<run>/app/launch-NN.*` with `SIMCTL_CHILD_TANDEMKIT_VIRTUAL_PUMP=<faketandem>` and `TSAN_OPTIONS`, plus the toolchain's clang runtime directory as `DYLD_FALLBACK_LIBRARY_PATH` in case the sanitizer runtime was not embedded. TandemKit's OSLog is streamed to `tandemkit-oslog.txt`. WebDriverAgent comes up if it can, and the Glucose Simulator is added through the UI if the seeded setting did not take. A WebDriverAgent started from another harness checkout turns UI actions off: WDA always listens on port 8100, so its taps would land on that checkout's simulator. | `tkstress/app.py`, `lane.py` |
| rounds | `--round-minutes` of chaos, then every fault undone, then the recovery check. A dead app is a finding and is relaunched. A slow accessibility query from a live app is sampled as a possible hang. The app's resident memory and thread count are recorded per round (the summary shows first, last and peak). | `chaos.py`, `recovery.py` |
| collection | Sanitizer and Main Thread Checker reports from every launch's stderr. `Trio*.ips` from `~/Library/Logs/DiagnosticReports`. TandemKit's and Trio's log files. faketandem's request log, appended per round to `faketandem-requests.jsonl`. | `detect.py`, `lane.py` |

How the pump gets paired without UI:
- A restored TandemKit with `derivedSecret` + `pincode` authenticates through JPAKE rounds 3/4 only, which is what faketandem accepts for a key given over `setLongTermKey`. It learns `serverNonce` on that first connect.
- It finds the pump by scanning for `peripheralUuid`, compared as a string with `CBPeripheral.identifier.uuidString`. So the seed uses the `peripheral_id` from faketandem's `hello` frame, in upper case.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Clean. Also used for `built-only` / `build-incomplete`: the build used the budget, and the next run starts warm. |
| 1 | Findings. |
| 2 | Harness or setup failure. |
| 3 | Trio does not build against that TandemKit. |

Each run leaves `summary.md`, `result.json` and `findings.json` in `runs/<ts>-stress-<ref>/`.

## What counts as a finding

| kind | detected by | fingerprint |
|---|---|---|
| `tsan` / `asan` | sanitizer reports on the app's stderr | report kind + the top 3 non-system frames of the access and the conflicting access (allocation, thread creation and mutex stacks are left out) |
| `main-thread` | Main Thread Checker on stderr (`--main-thread-checker`) | API + top 3 app frames |
| `crash` | `Trio*.ips` written during the run (skipping SIGKILL/SIGTERM of a pid the harness ended) | exception + top 4 non-system frames of the crashed thread |
| `app-died` | the app's pid gone without the harness ending it | (one per run; the crash report carries the detail) |
| `no-recovery` | after a round's faults are undone, either no authenticated session within `--session-timeout`, or a `remainingInsulin` qualifying event (whose handler only reads `InsulinStatusRequest`) not answered within `--probe-timeout` | the stage that failed |
| `hang` | a WebDriverAgent query to the live app slower than `--hang-seconds`; `sample` of the process | deepest 3 app frames of the main thread |
| `build` | xcodebuild fails | first compiler error, paths and line numbers removed |

Fingerprints use function names only, so the same race on a later commit, or with the code moved a few lines, maps
to the same issue. Frames are classed as TandemKit / Trio / LoopKit / other from module and path, and the class is
the finding's `component`.

## Chaos

A seeded `random.Random` picks every action and its parameters (`--seed`, recorded in `run.json` and every
issue). The seed reproduces the plan, not the interleaving.

| category | actions |
|---|---|
| pump | pump-UI or "remote" bolus (0.05–2 U), bolus abort, suspend (user/occlusion/alarm; resumed 20–150 s later), pump-side temp basal, bursts of read-only qualifying events, battery/reservoir changes, pump clock skew (±15 min), Control-IQ on/off |
| link | `drop_response`, `delay_response` (to 8 s), `error_response` (not on InitiateBolus), disconnect after a request or mid-response, `drop_fragment`, radio off for 2–90 s, pump-side link drop, ATT MTU 23/185/247 |
| app | SIGKILL then relaunch, `simctl terminate` then relaunch, background for 3–45 s |
| ui | Settings ▸ Devices ▸ Insulin Pump (TandemKit's settings screen), tab hops |

`--intensity` sets the gap between actions: low is 10–40 s, medium 3–15 s, high 0.5–5 s. The default, `cycle`,
alternates medium, high, low, high by round, so a long run sees the driver both flooded and given room to settle. `--categories` limits
the families. App actions need the simulator, and UI actions need WebDriverAgent. Without WDA (`--no-ui`, or it
did not come up), the run continues with pump, link and app chaos.

## CI

Two workflows run the lane on the self-hosted macOS workers. The job itself is `stress-sim-job.yml`, a reusable
workflow with no trigger of its own; both of them call it.

| Workflow | Trigger | Run budget | Job limit |
|---|---|---|---|
| `stress-sim-nightly.yml` | 02:00 America/New_York every night (the schedule's `timezone`, so it follows daylight saving) | 360 minutes | 375 minutes |
| `stress-sim-manual.yml` | `workflow_dispatch`, with inputs for the TandemKit ref, seed, intensity, sanitizer and chaos categories | 60 minutes (`minutes`, at most 60) | 75 minutes |

- **Time budget.** The run's deadline counts from the start of the job, build included, and the last 15 minutes of
  the job are kept for issues and artifacts. The build is stopped at whatever leaves time for setup and one round.
  After a warm build, the nightly run gets about 75 rounds at 4 minutes each, and the manual run about 10.
- **One at a time.** Both workflows share the `stress-sim` concurrency group: a manual run started during the
  nightly waits for it, and the other way round. GitHub keeps only one waiting run per group, so a second manual
  run started while one waits replaces it.
- **Persistent cache.** `TRIO_REPO`, `TH_DERIVED_DATA` and `TH_BUILD_DIR` live in a cache that survives between
  jobs: `/Volumes/work/cache/tandemkit-stress` if writable, else `~/Library/Caches/tandemkit-stress`. A warm build is
  incremental (usually only TandemKit changed). A cold one with TSan can use up a manual run's hour, and is
  reported as `build-incomplete` or `built-only` when it does; the next run starts warm.
- **Checkouts and tokens.** This repository is checked out with the job's own token. trio-test-harness is checked
  out at the commit the submodule pins (or `harness_ref`), and Trio's TandemKit submodule is fetched, with a GitHub
  App token (`TRIO_E2E_APP_ID` / `TRIO_E2E_APP_KEY`, contents read on trio-test-harness and TandemKit). That token
  lasts an hour, so the lane fetches everything from GitHub in its first minutes; faketandem, which is public, is
  fetched anonymously first.
- **Issues.** Findings (exit 1) and build breaks (exit 3) go to this repository through `file-issues` with the
  workflow's `GITHUB_TOKEN` (`issues: write`):
  - one issue per fingerprint, marked `<!-- stress-fingerprint: … -->`, labelled `stress` and `stress:<kind>`
  - a repeat comments on the open issue
  - a fingerprint whose issue was closed gets a new issue that links the old one
  - bodies are scrubbed of long hex/base64 strings and home paths
  - nothing is filed in TandemKit; a confirmed driver bug is promoted there by hand
- **Artifacts.** The whole run directory, kept for 14 days.

`.github/workflows/tests.yml` runs the unit tests on every push and pull request, on a hosted Ubuntu runner, against
a faketandem built from its default branch.

## Emulator gaps that shape the chaos

- faketandem accepts `InitiateBolusRequest` while a bolus runs or delivery is suspended. A real pump refuses. The
  lane has no dose oracle yet, so this skews nothing it checks today.
- An armed `error_response` is applied after the handler ran (TandemKit `docs/integration-harness.md`, D3), so
  the chaos never arms it on InitiateBolus.
- `POST /api/state/tempbasal/start` drops `percent: 0` (`omitempty`), so a pump-side 0% temp basal cannot be
  staged.
- Messages faketandem sends natively (history log stream, alarm status, qualifying events) bypass message-level
  faults and the request log; only `drop_fragment` reaches them.
- `delay_response` sleeps on the connection's serial worker and delays everything queued behind it.
- One attached central per emulator process: a second app instance would be refused as `busy`.
- No Bluetooth power cycling on the phone side: TandemKit's virtual `CBCentralManager` is always `.poweredOn`.
  Radio faults are the pump's radio.

## Not verified on a Mac yet

The faketandem side, the chaos actions, the recovery check, the detectors and the issue bodies were run on Linux
(while this lane still lived in trio-test-harness, before the move):
- against a real faketandem
- against a real TandemKit driver, the Linux TSan build of TandemKit's integration target, running a host loop
  while the chaos ran

Everything that needs Xcode was written from documentation and code reading. The first run on a worker should
confirm:
- the SDK-conditional `ENABLE_THREAD_SANITIZER` setting producing a TSan-instrumented Trio (and leaving the watch app alone)
- `simctl launch --stdout/--stderr` capturing the app's TSan output
- `SIMCTL_CHILD_*` reaching the app
- Trio restoring the seeded pump and honouring the seeded settings (`summary.md` and `run.json`
  `trio_settings_after` show what Trio kept)
- the bundled defaults living at `Trio.app/json/defaults/`
- simulator crash reports landing in `~/Library/Logs/DiagnosticReports`
- the Main Thread Checker dylib path (hence opt-in)
