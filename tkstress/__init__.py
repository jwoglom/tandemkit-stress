"""tandemkit-stress: TandemKit under load and broken links, inside Trio on the iOS Simulator, against faketandem.

Builds on trio-test-harness (its `harness` package): source checkout and xcodebuild, the simulator, WebDriverAgent and
the Trio UI flows. That checkout is the `trio-test-harness/` submodule, or TKSTRESS_HARNESS_DIR, e.g. the working copy
a harness Mac already has, so the device lock in its runs/.tmp is shared with its e2e runs.

The stress lane keeps its own Trio checkout, DerivedData and build directory (a TSan + virtual-pump build would
otherwise invalidate the harness's normal one) in a build cache shared by every run on the machine, by hand or from
CI, so the build stays warm. One run at a time holds it (tkstress/lock.py). The cache is TKSTRESS_CACHE, else
/Volumes/work/cache/tandemkit-stress where the CI Macs have that volume, else ~/Library/Caches/tandemkit-stress.
The lane also has its own simulator: it reinstalls Trio, which would pull the app out from under a trio-test-harness
run. Defaults, each overridable from the environment:
    TRIO_REPO        <cache>/Trio
    TH_DERIVED_DATA  <cache>/DerivedData
    TH_BUILD_DIR     <cache>/build
    TH_RUNS_DIR      <repo>/runs
    TH_SIM_NAME      TandemKitStress
Everything else the harness reads (TH_XCODE_APP, TH_SIM_DEVICE, git mirrors, TH_WDA_PREBUILT, ...) comes from the
environment or the harness checkout's harness.env, as for any harness command. <repo>/stress.env (gitignored) is
loaded first, for this machine's settings.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), os.path.expanduser(v.split(" #", 1)[0].strip().strip('"').strip("'")))


_load_env_file(REPO_ROOT / "stress.env")

HARNESS_DIR = Path(os.environ.get("TKSTRESS_HARNESS_DIR") or REPO_ROOT / "trio-test-harness").expanduser().resolve()
if not (HARNESS_DIR / "harness" / "__init__.py").exists():
    raise ImportError(f"trio-test-harness not found at {HARNESS_DIR}: run `git submodule update --init` "
                      "or set TKSTRESS_HARNESS_DIR to a trio-test-harness checkout")
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))


def _cache_dir() -> Path:
    if os.environ.get("TKSTRESS_CACHE"):
        return Path(os.environ["TKSTRESS_CACHE"]).expanduser()
    shared = Path("/Volumes/work/cache/tandemkit-stress")
    if os.access(shared if shared.exists() else shared.parent, os.W_OK):
        return shared
    return Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "tandemkit-stress"


CACHE_DIR = _cache_dir()

for _key, _value in {
    "TRIO_REPO": CACHE_DIR / "Trio",
    "TH_DERIVED_DATA": CACHE_DIR / "DerivedData",
    "TH_BUILD_DIR": CACHE_DIR / "build",
    "TH_RUNS_DIR": REPO_ROOT / "runs",
    "TH_SIM_NAME": "TandemKitStress",
}.items():
    os.environ.setdefault(_key, str(_value))


def setting(name: str, default: str = "") -> str:
    """A tandemkit-stress setting: the environment (stress.env included), else `default`."""
    return os.environ.get(name, default)
