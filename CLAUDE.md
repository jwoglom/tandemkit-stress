# tandemkit-stress — agent instructions

Stress lanes for TandemKit. Read first:
1. `README.md`: what exists and the CI setup.
2. `docs/lane.md`: the simulator lane.
3. `docs/roadmap.md`: the lanes still to build.

Conventions:
- Everything goes through `./tandemkit-stress` (`tkstress/cli.py`).
- trio-test-harness is a library here (the submodule, or `TKSTRESS_HARNESS_DIR`). When the lane needs something
  from the build, simulator or UI flows, add it to trio-test-harness and bump the pin; don't copy harness code in.
  Never commit or push in the lane's Trio checkout (`<cache>/Trio`; `./tandemkit-stress doctor` prints the cache).
- Harness `Config` reads only the environment variables it has defaults for. Read tandemkit-stress's own settings
  with `tkstress.setting()`.
- Tests:
  - `.venv/bin/python -m unittest discover -s tests`.
  - The live test needs `FAKETANDEM_BIN` and `FAKETANDEM_JAR`.
  - Lint workflows with `actionlint`.
- Comments: as in TandemKit's CLAUDE.md, default to none. Write one only for an emulator gap, a platform quirk, or a
  non-obvious ordering, in one line.
- `docs/lane.md` lists what is not yet verified on a Mac. Take an item off only when a run on a worker confirmed it.

Data and safety:
- Only simulated devices and fake glucose in this lane. Keep generated keys, pump serials, peripheral UUIDs and home
  paths out of issues, commits and docs. `file-issues` scrubs bodies; anything posted by hand gets the same.
- `stress.env`, `runs/`, `build/` and `work/` are gitignored. Never commit them, and never hardcode a token.
- Issues go to this repository only. A confirmed driver bug is promoted to TandemKit by a person, or on their
  request.
- Anything that drives a real pump follows the safety rules in `docs/roadmap.md` before it runs.
