# tandemkit-stress

Stress testing for [TandemKit](https://github.com/jwoglom/TandemKit), the LoopKit pump driver for the Tandem Mobi
and t:slim X2. It looks for crashes, data races, hangs and failures to recover. The driver runs inside a real host app
under load, with its Bluetooth link broken on purpose. Every finding becomes an issue in this repository.

The one lane built so far is Trio on the iOS Simulator against [faketandem](https://github.com/jwoglom/faketandem),
the Tandem pump emulator. It runs for six hours every night from 2am Eastern on a self-hosted Mac, and for as long
as asked (an hour by default) whenever it is started by hand.
- **Build:** nightscout/Trio `feat/tandem-closed-beta`, with its TandemKit submodule moved to TandemKit `dev`, built
  with the virtual pump and ThreadSanitizer.
- **Chaos:** seeded pump-side events, link faults, app kills and UI noise. Every round ends with a check that the
  driver reconnected and still answers the pump.

Only simulated devices are involved: faketandem and Trio's Glucose Simulator. No insulin is delivered.
[docs/lane.md](docs/lane.md) covers how a run works and what counts as a finding. [docs/roadmap.md](docs/roadmap.md)
lists the lanes still to build: decoder fuzzing, the driver on Linux, Loop, and the real Mobi.

## Running it on a Mac

No GitHub Actions involved: the same run as the workflows, from a terminal. Issues are filed with your own `gh` login.

Needs:
- macOS with Xcode
- Go and Java 17, which build faketandem and run pumpX2's cliparser
- Python 3.12+ (uv is used if installed)
- `gh`, logged in (`gh auth login`), for `--file-issues`

```
git clone --recurse-submodules https://github.com/jwoglom/tandemkit-stress && cd tandemkit-stress
./setup.sh                                          # venv with this repo's and trio-test-harness's requirements
./tandemkit-stress doctor                           # tools, cache, lane lock, and whether gh can file issues

./tandemkit-stress run --file-issues                # 1 hour, stress-sim-manual.yml's default
./tandemkit-stress run --minutes 360 --file-issues  # 6 hours, like the nightly
./tandemkit-stress run --tandemkit jwoglom/TandemKit#470 --seed 42 --minutes 30
./tandemkit-stress file-issues runs/<run>           # file a finished run's findings afterwards
./tandemkit-stress --help
```

How a run behaves:
- **Issues.** `--file-issues` checks before the run starts that `gh` can reach the issues repository, so a long run
  never ends unable to file. The repository is this checkout's `origin` (jwoglom/tandemkit-stress), or
  `--issues-repo` / `TKSTRESS_ISSUES_REPO`. Issues filed from a Mac name the run directory instead of an Actions run.
- **Sleep.** The run holds a `caffeinate` assertion, so an idle Mac does not sleep partway through. That does not
  cover a closed laptop on battery.
- **Background.** For a long run, use `tmux` or `nohup ./tandemkit-stress run … &` so closing the terminal does not
  end it.
- **Updates.** Pull the lane itself with `git pull --recurse-submodules`. TandemKit and Trio are fetched fresh on
  every run.

A run leaves `runs/<ts>-stress-<ref>/`:
- `summary.md`, `findings.json`, `actions.jsonl`
- the app's stderr per launch, crash reports and hang samples
- TandemKit's OSLog and faketandem's log and request log

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Clean, or built with no time left to run. |
| 1 | Findings. |
| 2 | Harness failure. |
| 3 | Trio does not build. |
| 4 | Another stress run on this Mac held the lane. `--wait` queues behind it instead. |

What the lane shares and what it keeps to itself:
- **trio-test-harness.** The lane builds on [trio-test-harness](https://github.com/jwoglom/trio-test-harness), the
  `trio-test-harness/` submodule: its source checkout, xcodebuild, simulator driver, WebDriverAgent and Trio UI
  flows. To use a checkout you already have, set `TKSTRESS_HARNESS_DIR`.
- **Its own state.** Neither a trio-test-harness run nor a CI job on the same Mac is disturbed:
  - its own simulator, `TandemKitStress`
  - its own Trio checkout, DerivedData and build directory, in a cache shared by every stress run on the machine,
    by hand or from CI, so the build stays warm. The cache is `/Volumes/work/cache/tandemkit-stress` where the CI
    Macs have that volume, else `~/Library/Caches/tandemkit-stress`, or `TKSTRESS_CACHE`.
- **One run at a time.** A lock in that cache allows one stress run on the Mac at once. A run started by hand while
  the nightly job runs on the same Mac exits with 4 and names the holder, and the other way round.
- **WebDriverAgent.** It always listens on port 8100. If one is running from another harness checkout, this lane
  runs without UI actions. A trio-test-harness run started while this lane's WebDriverAgent is up would drive this
  lane's simulator, so don't run both at once, or pass `--no-ui` here.

Machine settings go in `stress.env` (gitignored), as `KEY=value` lines. Examples: `TH_XCODE_APP`, `TH_SIM_DEVICE`,
`TKSTRESS_ISSUES_REPO`, or `FAKETANDEM_BIN` + `FAKETANDEM_JAR` to skip building faketandem. The harness's own `TH_*`
settings work too.

## CI

| Workflow | Runs on | What |
|---|---|---|
| `stress-sim-nightly.yml` | 02:00 America/New_York every night | The lane for 6 hours (360-minute run, 375-minute job). |
| `stress-sim-manual.yml` | by hand (`workflow_dispatch`) | The lane for `minutes` (60 by default, up to 1425; the job gets 15 more), with inputs for the TandemKit ref, seed, intensity and chaos mix. |
| `stress-sim-job.yml` | called by the two above | The job itself, on `[self-hosted, macOS, worker]`. Files issues for findings and build breaks. |
| `tests.yml` | `ubuntu-latest`, every push and PR | Unit tests, including one against a live faketandem. |

One-time setup:
1. **A runner on this repository.** jwoglom is a personal account, so a runner serves one repository. On a Mac,
   add one under Settings ▸ Actions ▸ Runners with the label `worker`. A Mac that already has a trio-test-harness
   runner can take a second one; see the WebDriverAgent note above, and expect both jobs at once to be slow. The Mac
   needs `/Applications/Xcode_26.2.app`, uv and the iPhone 17 simulator. `/Volumes/work` mirrors and a prebuilt WDA
   are used if present.
2. **Secrets `TRIO_E2E_APP_ID` and `TRIO_E2E_APP_KEY`.** Use the same GitHub App as trio-test-harness. It needs
   contents read on trio-test-harness and TandemKit, and both workflows use it to check out the harness.
3. **The default branch.** GitHub schedules and dispatches only workflows on the default branch.
4. **The submodule pin.** It points at the trio-test-harness commit that adds the build options this lane uses
   (`conditions`, `sanitizer`, `save`, `timeout`). Once that branch is merged, move the pin to the merged commit.

The first build is cold and long with TSan, so a one-hour manual run may spend all of it building: it is reported as
`build-incomplete` or `built-only`, not a failure. The build cache persists, so later runs rebuild little more than
TandemKit. A job that finds the lane held by a run started by hand on that Mac exits with 4. It ends with a warning,
not a failure.

## Issues

`file-issues` opens one issue per distinct failure, identified by kind plus the top non-system stack frames.
- **Repeats:** a repeat comments on the open issue with the commit and seed. A failure whose issue was closed gets a
  new issue linking the old one.
- **Labels:** `stress` and `stress:<kind>`.
- **Scrubbing:** bodies are scrubbed of keys and home paths. The full logs stay in the run's artifact, or
  in the run directory on the Mac for a run started by hand.
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
| `tkstress/lock.py` | one stress run per machine (per build cache) |
| `tests/` | unit tests and report fixtures |
