"""`./tandemkit-stress <command>`: the stress lane's commands (docs/lane.md)."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from . import CACHE_DIR, HARNESS_DIR, REPO_ROOT


def cmd_scan(args) -> int:
    from .detect import crash_finding, parse_ips, sample_finding, scan_stderr
    found = []
    for name in args.files:
        p = Path(name)
        crash = parse_ips(p) if p.suffix == ".ips" else None
        text = p.read_text(errors="replace")
        found += [crash_finding(crash)] if crash else (
            [sample_finding(text, what=p.name)] if "Call graph:" in text else scan_stderr(text))
    seen: dict[str, list] = {}
    for f in found:
        seen.setdefault(f.fp, [f, 0])[1] += 1
    for f, n in seen.values():
        print(f"{f.fp}  {n:>3}x  {f.kind:<11} {f.component:<9} {f.title}")
        if args.verbose:
            print("    " + "\n    ".join(f.signature))
    return 1 if found else 0


def cmd_faketandem(args) -> int:
    from .faketandem import DEFAULT_PAIRING_CODE, Faketandem
    fk = Faketandem.prepare(args.ref)
    with fk.start(Path(args.log)) as pump:
        print(f"faketandem GATT {pump.gatt_addr}  API {pump.api_url}  (log {args.log})")
        if args.paired:
            key = os.urandom(32)
            hello = pump.stage_paired_pump(key, DEFAULT_PAIRING_CODE)
            print(f"paired: peripheral {hello['peripheral_id'].upper()}  long-term key {key.hex()}")
        print(f"TANDEMKIT_VIRTUAL_PUMP={pump.gatt_addr}   (Ctrl-C stops it)")
        try:
            while pump.alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_doctor(args) -> int:
    from harness.util import HarnessError
    from . import setting
    ok = True

    def item(name, good, detail="", *, warn=False):
        nonlocal ok
        ok = ok and (good or warn)
        print(f"  [{'ok' if good else ('warn' if warn else 'MISSING')}] {name}{': ' + detail if detail else ''}")

    print("tandemkit-stress")
    item("trio-test-harness", True, str(HARNESS_DIR))
    item("build cache", True, str(CACHE_DIR))
    for key in ("TRIO_REPO", "TH_DERIVED_DATA", "TH_BUILD_DIR", "TH_RUNS_DIR", "TH_SIM_NAME"):
        item(key, True, os.environ.get(key, ""))
    from .lock import LaneBusy, hold
    try:
        with hold(CACHE_DIR / "lane.lock", "tandemkit-stress doctor"):
            item("stress lane", True, "free")
    except LaneBusy as e:
        item("stress lane", True, str(e))
    if setting("FAKETANDEM_BIN"):
        item("faketandem binary", Path(setting("FAKETANDEM_BIN")).exists(), setting("FAKETANDEM_BIN"))
    else:
        item("go (builds faketandem)", shutil.which("go"), shutil.which("go") or "brew install go, or set FAKETANDEM_BIN")
    item("java 17+ (pumpX2 cliparser)", shutil.which("java"), shutil.which("java") or "brew install openjdk@17")
    try:
        import websocket  # noqa: F401
        item("websocket-client", True)
    except ImportError:
        item("websocket-client", False, "./setup.sh")
    from .issues import check_gh, issues_repo
    repo = issues_repo()
    if not shutil.which("gh"):
        item("gh (--file-issues)", False, "brew install gh", warn=True)
    elif not repo:
        item("gh (--file-issues)", False, "no repository: set TKSTRESS_ISSUES_REPO", warn=True)
    else:
        try:
            check_gh(repo)
            item("gh (--file-issues)", True, f"can file into {repo}")
        except HarnessError as e:
            item("gh (--file-issues)", False, str(e)[:160], warn=True)
    try:
        from harness import xcode
        app = xcode.find_xcode()
        item("Xcode", app, f"{app} (v{xcode.xcode_version(app)})" if app else "needed for `run`", warn=not app)
    except Exception as e:  # noqa: BLE001 - doctor reports, it does not fail on a missing Xcode
        item("Xcode", False, str(e)[:120], warn=True)
    print("\nsummary:", "ready" if ok else "some required items missing")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    from .lane import add_run_arguments
    p = argparse.ArgumentParser(prog="tandemkit-stress",
                                description="TandemKit stress lane: Trio on the simulator against faketandem")
    sp = p.add_subparsers(dest="cmd", required=True)
    add_run_arguments(sp.add_parser("run", help="checkout + build + chaos rounds + recovery checks until a deadline"))
    fi = sp.add_parser("file-issues", help="one GitHub issue per distinct finding of a run (deduplicated)")
    fi.add_argument("run_dir")
    fi.add_argument("--repo", help="owner/repo (default TKSTRESS_ISSUES_REPO, GITHUB_REPOSITORY in CI, else this "
                                   "checkout's origin)")
    fi.add_argument("--kinds", help="only these finding kinds, comma-separated")
    fi.add_argument("--dry-run", action="store_true", help="print the issues instead of filing them")
    sc = sp.add_parser("scan", help="findings in sanitizer output, crash reports (.ips) or `sample` output files")
    sc.add_argument("files", nargs="+")
    sc.add_argument("-v", "--verbose", action="store_true")
    fk = sp.add_parser("faketandem", help="run faketandem in the foreground (manual testing with the virtual pump)")
    fk.add_argument("--ref")
    fk.add_argument("--paired", action="store_true", help="stage a random long-term key")
    fk.add_argument("--log", default=str(REPO_ROOT / "build" / "faketandem.log"))
    sp.add_parser("doctor", help="check the tools and settings the lane needs")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    from harness.util import HarnessError
    try:
        if args.cmd == "run":
            from .lane import cmd_run
            return cmd_run(args)
        if args.cmd == "file-issues":
            from .issues import cmd_file_issues
            return cmd_file_issues(args)
        return {"scan": cmd_scan, "faketandem": cmd_faketandem, "doctor": cmd_doctor}[args.cmd](args)
    except HarnessError as e:
        print(f"tandemkit-stress: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
