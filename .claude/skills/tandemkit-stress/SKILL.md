---
name: tandemkit-stress
description: Run, read and triage the TandemKit stress lane (`./tandemkit-stress run`). It runs Trio on the iOS Simulator with TandemKit built against faketandem (the Tandem pump emulator) under ThreadSanitizer, with seeded chaos (pump-side events, link faults, app kills) and recovery checks. Use when asked to stress test or fuzz TandemKit, look for races or crashes in the Tandem driver, run or re-run the nightly stress job, reproduce a stress finding from its seed, or triage a `stress` issue in jwoglom/tandemkit-stress.
---

# tandemkit-stress

Run commands from the tandemkit-stress checkout (`./setup.sh` once). Full description: `docs/lane.md`.

## Run
| Want | Command |
|---|---|
| The nightly configuration (closed-beta Trio + TandemKit dev, TSan, intensity cycling) | `./tandemkit-stress run --minutes 200` (build first, then `--minutes` of stress; default 60) |
| Only build, or stress a build made earlier | `./tandemkit-stress build [--build-minutes 120]` · `./tandemkit-stress run --build runs/<run>` |
| On a Mac without Actions, filing issues with the local `gh` login | add `--file-issues` (checks `gh` first; repo: this checkout's origin) |
| Shorter / reproducible / harsher | `./tandemkit-stress run --minutes 25 --seed 42 --intensity high` |
| A TandemKit branch or PR | `--tandemkit my-branch` · `--tandemkit jwoglom/TandemKit#470` |
| Skip checkout + build (an app already built with the virtual pump) | `--app <path to Trio.app>` |
| Only some chaos | `--categories pump,link` (also `app`, `ui`); `--no-ui` skips WebDriverAgent |
| In CI | `gh workflow run stress-sim-manual.yml -R jwoglom/tandemkit-stress -f tandemkit=… -f seed=… [-f minutes=240] [-f build_minutes=180]` |

One stress run per machine: a lock in the build cache covers runs started by hand and CI jobs alike. A run that
finds it held exits with 4 and names the holder; `--wait` queues instead. The lane uses its own simulator
(`TandemKitStress`). It needs Go and Java 17 for faketandem and pumpX2's cliparser, or `FAKETANDEM_BIN`
plus `FAKETANDEM_JAR`. `./tandemkit-stress doctor` checks all of this.

## Read a run (`runs/<ts>-stress-<ref>/`)
- `summary.md`: verdict, commits, seed, findings table, and per-round intensity, recovery, memory and threads.
- `findings.json`: kind, component, title, fingerprint, signature, excerpt, evidence paths.
- `actions.jsonl`: every chaos action with round, parameters and result (`ok` / `skipped` / `error`).
- `app/launch-NN.stderr`: the app's stderr per launch, where TSan reports land. Also `crashes/*.ips` and
  `hangs/*.txt`.
- Logs:
  - `tandemkit-oslog.txt`: TandemKit's debug log.
  - `logs/tandem_log.txt`.
  - `faketandem.log`.
  - `faketandem-requests.jsonl`: every message the pump saw, with sequence numbers.
- Exit code and `result.json` status: 0 clean or built, 1 findings, 2 harness failure or interrupted, 3 Trio does
  not build, 4 lane busy, 5 nothing tested (build stopped at `--build-minutes`, or `--until` left no time).

## Triage a finding
1. `./tandemkit-stress scan <stderr|.ips|sample file> -v` prints the finding(s) and the frames the fingerprint uses.
2. For `no-recovery`, read the actions just before the failed round in `actions.jsonl`. Then read the request log
   around it, and check whether the driver:
   - reconnected (`kind: connection`)
   - authenticated (`Jpake3/4`)
   - answered the probe (`InsulinStatusRequest`)

   `tandemkit-oslog.txt` has the driver's side.
3. Reproduce with the issue's command (same TandemKit sha and seed). The seed fixes the plan, not the timing, so
   run it a few times before calling it fixed or flaky.
4. A confirmed driver bug gets a TandemKit issue or fix PR that links the stress issue. A deterministic regression
   test belongs in TandemKit's unit or integration suites.

Only simulated devices are involved (faketandem, Glucose Simulator). Still keep generated keys out of anything
posted. `./tandemkit-stress file-issues` scrubs them.
