# TandemKit stress lane: Trio on the simulator against faketandem

`./tandemkit-stress run` looks for crashes, data races, hangs and failures to recover in TandemKit while it runs inside
Trio, by putting the driver under load and breaking its link on purpose. It builds Trio with its TandemKit
submodule moved to TandemKit `dev` (or any ref or PR), compiled with `TANDEMKIT_VIRTUAL_PUMP` and ThreadSanitizer.
The app runs on the iOS Simulator against [faketandem](https://github.com/jwoglom/faketandem), the Go Tandem pump
emulator. Seeded chaos then runs for `--minutes`: pump-side events, link faults, app kills and UI noise, each round
ending in a check that the driver came back. The build is a stage of its own (`build`, CI's first step), and the
stress clock starts at the first round: neither the build nor the simulator setup counts against `--minutes`. Only simulated devices are involved.

```
./tandemkit-stress run                                     # build closed-beta Trio + TandemKit dev, then 60 min of stress
./tandemkit-stress run --minutes 200                       # about what each nightly job gets
./tandemkit-stress run --minutes 200 --file-issues         # the same by hand, filing through your `gh` login
./tandemkit-stress run --minutes 25 --seed 42 --intensity high
./tandemkit-stress run --tandemkit jwoglom/TandemKit#470   # a TandemKit PR
./tandemkit-stress run --app <Trio.app> --minutes 15       # reuse a virtual-pump build, skip checkout + build
./tandemkit-stress build --build-minutes 120               # only the build, into a new run directory
./tandemkit-stress run --build runs/<run> --minutes 30     # stress that run directory's build
./tandemkit-stress file-issues runs/<run>                  # one GitHub issue per new finding
./tandemkit-stress scan app/launch-03.stderr crashes/*.ips # triage: the findings in sanitizer output / crash reports
./tandemkit-stress faketandem --paired                     # the emulator alone, for poking at it by hand
```

The lane builds on [trio-test-harness](https://github.com/jwoglom/trio-test-harness) (the `trio-test-harness/`
submodule, or `TKSTRESS_HARNESS_DIR`): its source checkout and `xcodebuild`, simulator driver, WebDriverAgent and
Trio UI flows. It keeps its own Trio checkout, DerivedData and build directory, so a stress build never invalidates
the harness's normal one. These live in a cache shared by every stress run on the machine, started by hand or from
CI (`tkstress/__init__.py`). A lock in it (`tkstress/lock.py`) lets one run at a time use the cache and the lane's
simulator. A run that finds the lane held exits with 4 and names the holder; with `--wait`, it queues.
- **Issues from a hand-started run.** `--file-issues` files them at the end through the local `gh` login. `gh`'s
  access to the repository is checked before the run starts.
- **Sleep.** The run holds a `caffeinate` assertion against idle sleep.

## How a run goes

| Stage | What happens | Code |
|---|---|---|
| faketandem source | Build stage. Fetched from `FAKETANDEM_REPO`@`--faketandem-ref` and built (or `FAKETANDEM_BIN` + `FAKETANDEM_JAR`), before anything long: a CI job's GitHub App token expires after an hour. | `tkstress/faketandem.py` |
| checkout + build | `prepare_source(branch=--trio, submodules=[TandemKit@--tandemkit])`, then `xcodebuild` with `TANDEMKIT_VIRTUAL_PUMP` and `ENABLE_THREAD_SANITIZER[sdk=iphonesimulator*] = YES` in the harness xcconfig. The sanitizer is set per SDK because Trio's scheme embeds a watchOS app, which the sanitizers do not support. A stress build never touches `build/last-build.json`. Its own xcconfig is `build/override-sim-tandemkit_virtual_pump-sanitized.xcconfig`. `--build-minutes` stops it (exit 5, status `build-incomplete`; the next build continues from DerivedData), so a CI job's limit never kills it without a report. `build.json` in the run directory records the app (with its executable's size and mtime), the faketandem binary and what was built; `run --build` refuses an app that changed since. | trio-test-harness `harness/build.py` |
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
| 0 | Clean, or (`build`) built. |
| 1 | Findings. |
| 2 | Harness or setup failure, or stopped by a signal (status `interrupted`; a summary is still written). |
| 3 | Trio does not build against that TandemKit. |
| 4 | Not run: another stress run on this machine held the lane. |
| 5 | Not run: the build stopped at `--build-minutes` (status `build-incomplete`; the next build continues from DerivedData), or `--until` left no time for a round (status `no-time`). |

Each run leaves `summary.md`, `result.json` and `findings.json` in `runs/<ts>-stress-<ref>/`.

## What counts as a finding

| kind | detected by | fingerprint |
|---|---|---|
| `tsan` / `asan` | sanitizer reports on the app's stderr | report kind + the top 3 non-system frames of the access and the conflicting access, in either order for TSan, which prints the later access first (allocation, thread creation and mutex stacks are left out) |
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

| Workflow | Trigger | Build limit | Stress |
|---|---|---|---|
| `stress-sim-nightly.yml` | 02:00 and 06:00 America/New_York every night (the schedule's `timezone`, so it follows daylight saving) | 120 minutes | until 20 minutes before the runner's limit: about 3 hours 20 after a warm build |
| `stress-sim-manual.yml` | `workflow_dispatch`, with inputs for the TandemKit ref, seed, intensity, sanitizer and chaos categories | `build_minutes`, 120 by default | `minutes`, 60 by default, cut at the same point |

- **Two steps.** The job's Build step runs `tandemkit-stress build --build-minutes …`, and its Stress step runs
  `tandemkit-stress run --build <that run directory> --minutes …` only if the build finished. The job is green
  only when the stress ran and found nothing: a build stopped at its limit, a busy lane or a harness error fail it
  too, each with its own annotation. The next run continues a stopped build from DerivedData.
- **The runner's 4-hour limit.** The self-hosted runners stop a job 4 hours after it starts ("The runner has
  received a shutdown signal"). Whatever has not been uploaded by then is lost, issues included. So the job keeps
  its own clock (`runner_limit_minutes`, 240):
  - The Build step's limit is `build_minutes`, or less if the build would leave under 50 minutes before the wall.
  - The Stress step passes `--until` 20 minutes before the wall, so the last recovery check, collection, issues and
    uploads finish inside it.
  - A job that started late, because the runner was busy, is cut by the same wall.
- **Time.** `minutes` is the stress alone, from the first round; the build has its own limit. A limit is a ceiling,
  not time held: a warm build takes minutes and the job ends that much sooner. The nightly asks for 240 minutes and
  gets what fits, about 50 rounds at 4 minutes each per job. When `--until` cuts a run short, its summary says so.
  A signal (job cancelled, runner shutting down) still writes a summary and collects what it can.
- **One at a time.** Both workflows share the `stress-sim` concurrency group: a manual run started during the
  nightly waits for it, and the other way round. GitHub keeps only one waiting run per group, so a second manual
  run started while one waits replaces it.
- **Runs started by hand.** The concurrency group does not cover a run started by hand on the runner's Mac; the
  lane lock does. A job that finds the lane held fails with "lane busy".
- **Persistent cache.** `TRIO_REPO`, `TH_DERIVED_DATA` and `TH_BUILD_DIR` live in the lane's cache, which survives
  between jobs: `/Volumes/work/cache/tandemkit-stress` where that volume is writable, else
  `~/Library/Caches/tandemkit-stress`. A warm build is incremental (usually only TandemKit changed). A cold one with
  TSan is long; if it reaches the build limit, the next run continues it.
- **Checkouts and tokens.** This repository is checked out with the job's own token. trio-test-harness is checked
  out at the commit the submodule pins (or `harness_ref`), and Trio's TandemKit submodule is fetched, with a GitHub
  App token (`TRIO_E2E_APP_ID` / `TRIO_E2E_APP_KEY`, contents read on trio-test-harness and TandemKit). That token
  lasts an hour, so the build step fetches everything from GitHub in its first minutes, and the stress step needs
  nothing from GitHub. faketandem, which is public, is fetched anonymously first.
- **Issues.** The Build and Stress steps run with `--file-issues` and `TKSTRESS_ISSUES_TOKEN` set to the workflow's
  `GITHUB_TOKEN` (`issues: write`). Each new finding is filed the moment it is seen; a final step files whatever a
  stopped run did not get to.
  - one issue per fingerprint, marked `<!-- stress-fingerprint: … -->`, labelled `stress` and `stress:<kind>`
  - the body carries the full report and the context captured when it appeared (actions, faketandem messages,
    TandemKit's log, round/launch/memory), so it reads without the artifact
  - the finding's files (`.ips`, hang sample, launch stderr, screenshots) are attached as assets of the monthly
    pre-release `stress-attachments-YYYY-MM`, which needs `contents: write`; a crash report is uploaded without the
    machine's crash-reporter identifiers, home path or simulator device ID, image UUIDs kept
  - a repeat within the run gets one closing comment with its count; one in a later run comments on the open issue
  - a fingerprint whose issue was closed gets a new issue that links the old one
  - bodies are scrubbed of long hex/base64 strings, UUIDs, simulator device and app container IDs and home paths
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
