# tandemkit-stress

Stress testing for [TandemKit](https://github.com/jwoglom/TandemKit), the LoopKit pump driver for the Tandem Mobi
and t:slim X2. It looks for crashes, data races, hangs and failures to recover. The driver runs inside a real host app
under load, with its Bluetooth link broken on purpose. Every finding becomes an issue in this repository.

The one lane built so far is Trio on the iOS Simulator against [faketandem](https://github.com/jwoglom/faketandem),
the Tandem pump emulator. It runs for six hours every night from 2am Eastern on a self-hosted Mac, and for an
hour whenever it is started by hand.
- **Build:** nightscout/Trio `feat/tandem-closed-beta`, with its TandemKit submodule moved to TandemKit `dev`, built
  with the virtual pump and ThreadSanitizer.
- **Chaos:** seeded pump-side events, link faults, app kills and UI noise. Every round ends with a check that the
  driver reconnected and still answers the pump.

Only simulated devices are involved: faketandem and Trio's Glucose Simulator. No insulin is delivered.
[docs/lane.md](docs/lane.md) covers how a run works and what counts as a finding. [docs/roadmap.md](docs/roadmap.md)
lists the lanes still to build: decoder fuzzing, the driver on Linux, Loop, and the real Mobi.

## Running it

macOS with Xcode for `run`. Also Go and Java 17, which build faketandem and run pumpX2's cliparser. Python 3.12+
(uv is used if installed).

```
git clone --recurse-submodules https://github.com/jwoglom/tandemkit-stress && cd tandemkit-stress
./setup.sh                                   # venv with this repo's and trio-test-harness's requirements
./tandemkit-stress doctor
./tandemkit-stress run --minutes 30          # closed-beta Trio + TandemKit dev, TSan, cycling intensity
./tandemkit-stress run --tandemkit jwoglom/TandemKit#470 --seed 42
./tandemkit-stress --help
```

A run leaves `runs/<ts>-stress-<ref>/`:
- `summary.md`, `findings.json`, `actions.jsonl`
- the app's stderr per launch, crash reports and hang samples
- TandemKit's OSLog and faketandem's log and request log

Exit codes: 0 clean (or built with no time left to run), 1 findings, 2 harness failure, 3 Trio does not build.

The lane builds on [trio-test-harness](https://github.com/jwoglom/trio-test-harness), the `trio-test-harness/`
submodule, using its source checkout, xcodebuild, simulator driver, WebDriverAgent and Trio UI flows. To use a
checkout you already have, set `TKSTRESS_HARNESS_DIR`. The lane also keeps its own state, so it never disturbs the
harness's normal runs:
- its own Trio checkout (`work/Trio`), DerivedData, `build/` and `runs/`
- its own simulator, `TandemKitStress`

Machine settings go in `stress.env` (gitignored), as `KEY=value` lines. Examples: `TH_XCODE_APP`, `TH_SIM_DEVICE`,
or `FAKETANDEM_BIN` + `FAKETANDEM_JAR` to skip building faketandem. The harness's own `TH_*` settings work too.

## CI

| Workflow | Runs on | What |
|---|---|---|
| `stress-sim-nightly.yml` | 02:00 America/New_York every night | The lane for 6 hours (360-minute run, 375-minute job). |
| `stress-sim-manual.yml` | by hand (`workflow_dispatch`) | The lane for 1 hour (60-minute run, 75-minute job), with inputs for the TandemKit ref, seed, intensity and chaos mix. |
| `stress-sim-job.yml` | called by the two above | The job itself, on `[self-hosted, macOS, worker]`. Files issues for findings and build breaks. |
| `tests.yml` | `ubuntu-latest`, every push and PR | Unit tests, including one against a live faketandem. |

One-time setup:
1. **A runner on this repository.** jwoglom is a personal account, so a runner serves one repository. On a Mac,
   add one under Settings ▸ Actions ▸ Runners with the label `worker`. A Mac that already has a trio-test-harness
   runner can take a second one.
   - Neither job can uninstall the other's Trio: this lane uses its own simulator.
   - A WebDriverAgent the other job started (it always takes port 8100) turns this lane's UI actions off, not
     its run.
   - Both jobs at once will still be slow.
   - The Mac needs `/Applications/Xcode_26.2.app`, uv and the iPhone 17 simulator. `/Volumes/work` mirrors and a
     prebuilt WDA are used if present.
2. **Secrets `TRIO_E2E_APP_ID` and `TRIO_E2E_APP_KEY`.** Use the same GitHub App as trio-test-harness. It needs
   contents read on trio-test-harness and TandemKit, and both workflows use it to check out the harness.
3. **The default branch.** GitHub schedules and dispatches only workflows on the default branch.
4. **The submodule pin.** It points at the trio-test-harness commit that adds the build options this lane uses
   (`conditions`, `sanitizer`, `save`, `timeout`). Once that branch is merged, move the pin to the merged commit.

The first build is cold and long with TSan, so a manual run may spend its whole hour on it: it is reported as
`build-incomplete` or `built-only`, not a failure. The checkout and DerivedData persist in
`/Volumes/work/cache/tandemkit-stress` (or `~/Library/Caches/tandemkit-stress`), so later nights rebuild little
more than TandemKit.

## Issues

`file-issues` opens one issue per distinct failure, identified by kind plus the top non-system stack frames.
- **Repeats:** a repeat comments on the open issue with the commit and seed. A failure whose issue was closed gets a
  new issue linking the old one.
- **Labels:** `stress` and `stress:<kind>`.
- **Scrubbing:** bodies are scrubbed of keys and home paths. The full logs stay in the run's artifact.
- **TandemKit:** nothing is filed there automatically. A confirmed driver bug gets a TandemKit issue or fix PR that
  links back here.

## Layout

| Path | |
|---|---|
| `tkstress/lane.py` | the run: build, seed, launch, rounds, collection, summary |
| `tkstress/chaos.py`, `recovery.py` | seeded actions and their undo; the post-round recovery check |
| `tkstress/faketandem.py` | build, start and drive faketandem (HTTP API, websocket, fault injection) |
| `tkstress/seed.py` | a paired TandemPumpManager and closed-loop settings written into Trio's container |
| `tkstress/app.py` | the app on the simulator: launch with env, stderr capture, OSLog, samples, metrics |
| `tkstress/detect.py` | sanitizer, Main Thread Checker, crash report and hang parsing; fingerprints |
| `tkstress/issues.py` | issue filing |
| `tests/` | unit tests and report fixtures |
