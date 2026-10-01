"""`tandemkit-stress run`: Trio on the iOS Simulator against faketandem, with chaos, until a deadline.

    ./tandemkit-stress run                          # closed-beta Trio + TandemKit dev, 60 min budget
    ./tandemkit-stress run --minutes 360            # what the nightly job runs
    ./tandemkit-stress run --minutes 20 --seed 42 --intensity high
    ./tandemkit-stress run --tandemkit jwoglom/TandemKit#470   # a TandemKit PR instead of dev
    ./tandemkit-stress run --app build/…/Trio.app   # skip checkout and build (an app built with the virtual pump)
    ./tandemkit-stress run --minutes 360 --file-issues   # by hand on a Mac, filing issues through the local `gh` login

Stages, each bounded by the deadline:
  1. faketandem fetched and built (tkstress/faketandem.py): first, while any CI token is still fresh
  2. checkout + build: Trio with its TandemKit submodule moved to the requested ref, compiled with
     TANDEMKIT_VIRTUAL_PUMP (CoreBluetooth replaced by a socket to faketandem) and, by default, ThreadSanitizer
  3. faketandem started on two free loopback ports, with a random long-term key staged
  4. simulator: fresh install, onboarding pre-seeded, Trio's settings and a paired Tandem pump written into the
     container (seed.py), launched with TANDEMKIT_VIRTUAL_PUMP=<faketandem> (app.py)
  5. rounds: chaos (chaos.py) for --round-minutes, then every fault undone and the recovery check (recovery.py);
     a dead app is a finding and is relaunched; the app's memory and thread count are recorded per round
  6. collection: sanitizer and Main Thread Checker reports from every launch's stderr, crash reports, TandemKit's
     OSLog stream and log file, faketandem's request log; findings.json, summary.md, result.json

Exit codes: 0 clean (or built but no time left to run), 1 findings, 2 harness/setup failure, 3 Trio does not build
against that TandemKit, 4 another stress run on this machine holds the lane (tkstress/lock.py; --wait queues).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

from harness.util import HarnessError, log
from . import CACHE_DIR, HARNESS_DIR
from .lock import LaneBusy, holder_description
from .detect import (Finding, Findings, crash_finding, default_crash_dirs, find_crash_reports, parse_ips,
                     sample_finding, scan_stderr)

DEFAULT_TRIO = "nightscout/Trio:feat/tandem-closed-beta"
DEFAULT_TANDEMKIT = "dev"
EXIT_CLEAN, EXIT_FINDINGS, EXIT_HARNESS, EXIT_BUILD, EXIT_BUSY = 0, 1, 2, 3, 4
FAILURE_KINDS = {"crash", "tsan", "asan", "main-thread", "no-recovery", "hang", "app-died"}

SETUP_RESERVE_S = 8 * 60        # simulator boot, install, launch, WDA, CGM
CHECK_RESERVE_S = 4 * 60        # one quiesce + recovery check at its slowest
COLLECT_RESERVE_S = 3 * 60      # stopping everything, crash reports, summary
MIN_ROUND_S = 60
AFTER_BUILD_S = SETUP_RESERVE_S + MIN_ROUND_S + CHECK_RESERVE_S + COLLECT_RESERVE_S
# --intensity cycle: rounds alternate, so a long run sees the driver both flooded and given room to settle
INTENSITY_CYCLE = ["medium", "high", "low", "high"]


def new_run(name: str, meta: dict):
    """A run directory under TH_RUNS_DIR. Not harness Run.new(): that repoints the harness checkout's runs/.current,
    which an interactive harness session on the same machine may be using."""
    from harness.report import Run
    from harness.util import now_slug, write_json
    from harness.config import CONFIG
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "run"
    run = Run(CONFIG.runs_dir / f"{now_slug()}-{slug}")
    write_json(run.meta_path, {"name": name, "created": dt.datetime.now().isoformat(timespec="seconds"),
                               "status": "running", **meta})
    return run


def wda_started_here() -> bool:
    """Whether the WebDriverAgent runner the harness recorded in this checkout's runs/.tmp is still alive."""
    from harness import wda as W
    try:
        os.kill(int(W.PID_FILE.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def tandemkit_spec(ref: str) -> str:
    """--tandemkit value -> a --submodule spec: a PR or owner/repo@branch as given, else TandemKit@<ref>."""
    return ref if ("/" in ref or "#" in ref or ref.startswith("http")) else f"TandemKit@{ref}"


def build_failure(errors: str, trio: str, tandemkit: str) -> Finding:
    """One issue per distinct break: keyed on the first compiler error with paths and line numbers removed."""
    first = next((l for l in errors.splitlines() if re.search(r"(?:^|: )error: ", l)),
                 errors.strip().splitlines()[0] if errors.strip() else "")
    norm = re.sub(r"(/[^\s:]+/)", "", first)
    norm = re.sub(r":\d+(:\d+)?", "", norm).strip()
    return Finding(kind="build", title=f"Trio ({trio}) does not build against TandemKit {tandemkit}"[:200],
                   signature=[norm[:300]], component="TandemKit", details=errors[-6000:])


class Lane:
    def __init__(self, args):
        self.args = args
        self.t0 = time.time()
        self.deadline = float(args.deadline) if args.deadline else self.t0 + args.minutes * 60
        self.seed = args.seed if args.seed is not None else random.randrange(1, 2**31)
        self.fk = None
        self.app = None
        self.logstream = None
        self.drv = None
        self.container: Path | None = None
        self.ui = None
        self.chaos = None
        self.rounds: list[dict] = []
        self.status = "running"
        self.note = ""
        self.build_only = ""
        self.req_seq = 0

    def left(self) -> float:
        return self.deadline - time.time()

    # ---------------- stages -----------------

    def checkout_and_build(self):
        from harness import build as B
        a = self.args
        if a.app:
            app = Path(a.app).expanduser().resolve()
            info = plistlib.loads((app / "Info.plist").read_bytes())
            self.run.update_meta(app=str(app), git={"note": "prebuilt app; commits unknown"})
            return app, info["CFBundleIdentifier"]
        src, info = B.prepare_source(branch=a.trio, submodules=[tandemkit_spec(a.tandemkit)])
        self.run.update_meta(git={k: info.get(k) for k in ("branch", "sha", "describe", "submodules",
                                                            "submodule_overrides")})
        budget = self.left() - AFTER_BUILD_S
        if budget < 60:
            budget = self.left() - COLLECT_RESERVE_S
            self.build_only = (f"A {(self.deadline - self.t0) / 60:.0f}-minute budget leaves no room for a round after "
                               f"the build, which needs {AFTER_BUILD_S // 60} more minutes (setup, one round, the "
                               "recovery check, collection), so this run only builds, warming the cache for the next.")
            log(self.build_only)
        try:
            bi = B.build(src, "sim", configuration="Debug", git_info=info, log_path=self.run.path / "xcodebuild.log",
                         conditions=("TANDEMKIT_VIRTUAL_PUMP",), sanitizer=None if a.sanitizer == "none" else a.sanitizer,
                         save=False, timeout=max(budget, 60) if a.enforce_deadline else None)
        except B.BuildTimeout as e:
            raise _Stop("build-incomplete", f"{e}. Not a failure: the next run continues from DerivedData. "
                        f"{self.build_only}".strip(), EXIT_CLEAN) from None
        except HarnessError as e:
            self.findings.add(build_failure(str(e), a.trio, a.tandemkit))
            raise _Stop("build-failed", "Trio did not build against this TandemKit", EXIT_BUILD) from None
        self.run.update_meta(build={"xcode": bi.xcode, "app": bi.app_path, "bundle_id": bi.bundle_id})
        return Path(bi.app_path), bi.bundle_id

    def prepare_faketandem(self):
        from .faketandem import Faketandem
        self.fk = Faketandem.prepare(self.args.faketandem_ref)
        self.run.update_meta(faketandem=self.fk.info)

    def start_faketandem(self):
        self.fk.start(self.run.path / "faketandem.log")
        self.run.update_meta(faketandem={**self.fk.info, "gatt": self.fk.gatt_addr, "api": self.fk.api_url})

    def install_and_seed(self, app: Path, bundle_id: str) -> bytes:
        from .faketandem import DEFAULT_PAIRING_CODE
        from harness.flows import trio as F
        from harness.sim import SimulatorDriver
        from . import seed
        # the driver itself, not the daemon proxy get_driver() hands out: the lane needs its udid and containers
        self.drv = SimulatorDriver()
        self.drv.boot()
        if not self.args.keep_installed:
            self.drv.uninstall(bundle_id)
        self.drv.install(app)
        self.container = self.drv.app_data_container(bundle_id)
        F.preseed_onboarding(self.container)
        seed.write_trio_settings(self.container, app=app)
        key = os.urandom(32)
        hello = self.fk.stage_paired_pump(key, DEFAULT_PAIRING_CODE)
        seed.write_pump_state(self.container, seed.tandem_pump_manager_raw(
            peripheral_id=hello["peripheral_id"], long_term_key=key, pairing_code=DEFAULT_PAIRING_CODE))
        self.run.log_step("seeded", f"onboarding, closed loop + Glucose Simulator, Tandem pump paired with faketandem "
                                    f"({hello['peripheral_id'].upper()})")
        return key

    def launch(self, bundle_id: str):
        from .app import LogStream, SimApp, main_thread_checker_dylib, sanitizer_runtime_dir
        env = {"TANDEMKIT_VIRTUAL_PUMP": self.fk.gatt_addr}
        if self.args.sanitizer == "thread":
            env["TSAN_OPTIONS"] = "halt_on_error=0:history_size=4:second_deadlock_stack=1"
        elif self.args.sanitizer == "address":
            env["ASAN_OPTIONS"] = "halt_on_error=1:detect_leaks=0"
        if self.args.sanitizer != "none" and (rt := sanitizer_runtime_dir()):
            env["DYLD_FALLBACK_LIBRARY_PATH"] = str(rt)
        if self.args.main_thread_checker:
            dylib = main_thread_checker_dylib()
            if dylib:
                env["DYLD_INSERT_LIBRARIES"] = str(dylib)
            else:
                log("Main Thread Checker dylib not found in this Xcode; running without it")
        self.app = SimApp(self.drv.udid, bundle_id, self.run.path / "app", env)
        self.logstream = LogStream(self.drv.udid, self.run.path / "tandemkit-oslog.txt")
        self.logstream.start()
        self.app.launch()

    def bring_up_ui(self):
        """WebDriverAgent, the home screen, and the Glucose Simulator if the seeded setting did not take. The lane
        runs without UI actions when any of it fails: the pump-side chaos does not need it."""
        from harness import wda as W
        from harness.flows import trio as F
        if self.args.no_ui:
            return
        if W.is_running() and not wda_started_here():
            # WDA always listens on the host's port 8100, so this one drives another checkout's simulator.
            log(f"WebDriverAgent on :{W.PORT} was not started from {HARNESS_DIR}; UI actions disabled")
            self.run.log_step("ui-unavailable", f"WebDriverAgent on :{W.PORT} belongs to another harness checkout")
            return
        try:
            if not W.is_running():
                W.start(self.drv.udid)
            self.drv.ensure_wda()
            time.sleep(6)
            F.settle(self.drv, self.run)
            if not F.at_home(self.drv):
                F.step(self.run, self.drv, "not-home", "home screen not detected; UI actions disabled")
                return
            if self.drv.find("Add CGM", exact=True):
                F.add_cgm_simulator(self.drv, self.run)
            F.step(self.run, self.drv, "home", "home screen, before the first round")
            self.ui = self.drv
        except Exception as e:  # noqa: BLE001 - no UI is a reduced run, not a failed one
            log(f"UI unavailable ({type(e).__name__}: {e}); continuing with pump, link and app chaos only")
            self.run.log_step("ui-unavailable", f"{type(e).__name__}: {e}"[:300])

    # ---------------- rounds -----------------

    def dump_requests(self) -> None:
        try:
            page = self.fk.client.request_log(since=self.req_seq)
        except HarnessError:
            return
        entries = [e for e in page.get("entries", []) if e.get("seq", 0) > self.req_seq]
        if not entries:
            return
        if page.get("oldest_seq", 0) > self.req_seq + 1 and self.req_seq:
            entries.insert(0, {"kind": "harness-note", "note": f"ring overflow: seq {self.req_seq + 1}.."
                                                                 f"{page['oldest_seq'] - 1} were dropped"})
        with open(self.run.path / "faketandem-requests.jsonl", "a") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        self.req_seq = max(e.get("seq", 0) for e in entries if "seq" in e)

    def recent_actions(self, n: int = 40) -> str:
        p = self.run.path / "actions.jsonl"
        lines = p.read_text().splitlines()[-n:] if p.exists() else []
        return "\n".join(lines)

    def check_app(self, r: int) -> None:
        if self.app.died_unexpectedly():
            pid = self.app.pid
            time.sleep(3)          # ReportCrash needs a moment to write the .ips
            self.findings.add(Finding(kind="app-died", title="Trio exited without the harness ending it",
                                      signature=["app-died"], round=r,
                                      details=f"pid {pid}; see crashes/ and app/launch-*.stderr\n\n"
                                              f"recent actions:\n{self.recent_actions()}"))
            self.collect_crashes()
            self.app.launch()

    def check_hang(self, r: int) -> None:
        """A WebDriverAgent query needs the app's main thread; minutes without an answer from a live app is a hang."""
        if self.ui is None or not self.app.alive():
            return
        t = time.time()
        try:
            self.ui.find("Settings", exact=True)
        except Exception:  # noqa: BLE001 - the timing is what matters
            pass
        took = time.time() - t
        if took < self.args.hang_seconds:
            return
        dest = self.run.path / "hangs" / f"round-{r:02d}.txt"
        dest.parent.mkdir(exist_ok=True)
        if self.app.sample(dest):
            f = sample_finding(dest.read_text(errors="replace"), what=f"accessibility query took {took:.0f}s")
            f.round, f.evidence = r, [str(dest.relative_to(self.run.path))]
            self.findings.add(f)

    def rounds_loop(self):
        from harness.flows import trio as F
        from . import recovery
        from .chaos import INTENSITY_GAPS, Chaos, ChaosContext
        a = self.args
        cats = set(a.categories.split(",")) if a.categories else None
        ctx = ChaosContext(random.Random(self.seed), self.fk.client, self.fk.control, app=self.app, ui=self.ui,
                           ui_flows=F)
        self.chaos = Chaos(ctx, intensity="medium" if a.intensity == "cycle" else a.intensity, categories=cats,
                           log_path=self.run.path / "actions.jsonl")
        round_s = a.round_minutes * 60
        r = 0
        while self.left() > min(round_s, MIN_ROUND_S) + CHECK_RESERVE_S + COLLECT_RESERVE_S:
            r += 1
            self.chaos.round = r
            intensity = INTENSITY_CYCLE[(r - 1) % len(INTENSITY_CYCLE)] if a.intensity == "cycle" else a.intensity
            self.chaos.gaps = INTENSITY_GAPS[intensity]
            span = min(round_s, self.left() - CHECK_RESERVE_S - COLLECT_RESERVE_S)
            log(f"== round {r}: {intensity} chaos for {span / 60:.1f} min")
            recs = self.chaos.run_for(span, should_stop=self.app.died_unexpectedly)
            self.check_app(r)
            self.chaos.quiesce()
            res = recovery.check(self.fk.client, session_timeout=a.session_timeout, probe_timeout=a.probe_timeout)
            self.dump_requests()
            applied = sum(1 for x in recs if x["result"] == "ok" and x["category"] not in ("undo", "quiesce"))
            metrics = self.app.metrics() if hasattr(self.app, "metrics") else {}
            self.rounds.append({"round": r, "intensity": intensity, "actions": applied, **metrics, **res})
            log(f"round {r}: {applied} actions applied; recovery {'ok' if res['ok'] else 'FAILED'} "
                f"({res.get('stage')}, {res.get('seconds')}s)")
            if not res["ok"]:
                ev = []
                if self.ui is not None:
                    shot = self.run.shot_path(f"round-{r:02d}-no-recovery")
                    try:
                        self.ui.screenshot(shot)
                        ev.append(str(shot.relative_to(self.run.path)))
                    except Exception:  # noqa: BLE001
                        pass
                sample = self.run.path / "hangs" / f"round-{r:02d}-no-recovery.txt"
                sample.parent.mkdir(exist_ok=True)
                if self.app.sample(sample):
                    ev.append(str(sample.relative_to(self.run.path)))
                self.findings.add(Finding(
                    kind="no-recovery", title=f"TandemKit did not recover after the faults stopped ({res['stage']} check)",
                    signature=[res["stage"]], component="TandemKit", round=r, evidence=ev,
                    details=json.dumps(res, indent=2) + f"\n\nlast actions:\n{self.recent_actions()}"))
            self.check_hang(r)
            self.check_app(r)
        return r

    # ---------------- collection -----------------

    def collect_crashes(self) -> None:
        dest = self.run.path / "crashes"
        for p in find_crash_reports(default_crash_dirs(), since=self.t0, proc="Trio"):
            if (dest / p.name).exists():
                continue
            dest.mkdir(exist_ok=True)
            shutil.copy2(p, dest / p.name)
            crash = parse_ips(dest / p.name)
            if not crash:
                continue
            if crash["exception"].get("signal") in ("SIGKILL", "SIGTERM") and self.app and \
                    crash.get("pid") in self.app.ended_by_us:
                continue           # the harness ended this process
            f = crash_finding(crash)
            f.evidence = [f"crashes/{p.name}"]
            self.findings.add(f)

    def collect(self) -> None:
        for step, fn in (("chaos quiesce", lambda: self.chaos and self.chaos.quiesce()),
                         ("request log", self.dump_requests),
                         ("log stream", lambda: self.logstream and self.logstream.stop()),
                         ("app", lambda: self.app and self.app.terminate()),
                         ("crash reports", self.collect_crashes),
                         ("sanitizer reports", self.collect_stderr),
                         ("app logs", self.copy_app_logs),
                         ("faketandem", lambda: self.fk and self.fk.stop())):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - collect everything that can be collected
                log(f"collect: {step} failed: {type(e).__name__}: {e}")

    def collect_stderr(self) -> None:
        if not self.app:
            return
        for p in self.app.stderr_files():
            for f in scan_stderr(p.read_text(errors="replace")):
                f.evidence = [f"app/{p.name}"]
                self.findings.add(f)

    def copy_app_logs(self) -> None:
        if not self.container:
            return
        dest = self.run.path / "logs"
        for rel in ("Documents/tandem/tandem_log.txt", "Documents/tandem/tandem_log_prev.txt",
                    "Documents/logs/log.txt", "Documents/logs/log_prev.txt"):
            p = self.container / rel
            if p.exists():
                dest.mkdir(exist_ok=True)
                shutil.copy2(p, dest / p.name)
        from . import seed
        self.run.update_meta(trio_settings_after=seed.read_back(self.container))

    # ---------------- report -----------------

    def summary(self, code: int) -> str:
        meta = self.run.meta
        git = meta.get("git") or {}
        subs = git.get("submodules") or {}
        fk = meta.get("faketandem") or {}
        counts = self.chaos.counts if self.chaos else {}
        applied = sum(v for k, v in counts.items() if k.endswith(":ok"))
        skipped = sum(v for k, v in counts.items() if k.endswith(":skipped"))
        errors = sum(v for k, v in counts.items() if k.endswith(":error"))
        failures = [f for f in self.findings if f.kind in FAILURE_KINDS or f.kind == "build"]
        lines = [f"## TandemKit stress (simulator + faketandem): {self.status}", "",
                 f"Trio `{self.args.trio}` @ `{git.get('sha', '?')}` · TandemKit `{self.args.tandemkit}` @ "
                 f"`{subs.get('TandemKit', '?')}` · faketandem `{fk.get('ref', fk.get('source', '?'))}` @ "
                 f"`{fk.get('sha', '?')}`",
                 f"seed `{self.seed}` · sanitizer `{self.args.sanitizer}` · intensity `{self.args.intensity}` · "
                 f"{len(self.rounds)} rounds · {applied} actions applied ({skipped} skipped, {errors} errors) · "
                 f"{len(self.app.launches) if self.app else 0} app launches · "
                 f"{(time.time() - self.t0) / 60:.0f} min", ""]
        if self.note:
            lines += [self.note, ""]
        if failures:
            lines += [f"### Findings ({len(failures)})", "", "| kind | component | finding | fingerprint | seen |",
                      "|---|---|---|---|---|"]
            lines += [f"| {f.kind} | {f.component} | {f.title.replace('|', '/')} | `{f.fp}` | {f.occurrences} |"
                      for f in failures]
            lines.append("")
        elif self.rounds:
            lines += ["No findings.", ""]
        if self.rounds:
            rss = [r["rss_mb"] for r in self.rounds if r.get("rss_mb")]
            threads = [r["threads"] for r in self.rounds if r.get("threads")]
            if rss:
                lines += [f"App memory {rss[0]} → {rss[-1]} MB (peak {max(rss)}), threads {threads[0] if threads else '?'}"
                          f" → {threads[-1] if threads else '?'} (peak {max(threads) if threads else '?'}); sampled after "
                          "each round, sanitizer overhead included.", ""]
            lines += ["### Rounds", "", "| # | intensity | actions | recovery | after | MB | threads |",
                      "|---|---|---|---|---|---|---|"]
            lines += [f"| {r['round']} | {r.get('intensity', '')} | {r['actions']} | "
                      f"{'ok' if r['ok'] else 'FAILED at ' + r['stage']} | {r.get('seconds', '?')}s | "
                      f"{r.get('rss_mb', '')} | {r.get('threads', '')} |" for r in self.rounds]
            lines.append("")
        lines.append(f"Reproduce: `./tandemkit-stress run --trio {self.args.trio} --tandemkit "
                     f"{subs.get('TandemKit') or self.args.tandemkit} --seed {self.seed} --intensity {self.args.intensity}`"
                     " (the seed fixes the plan of actions, not thread timing)")
        return "\n".join(lines) + "\n"

    # ---------------- driver -----------------

    def execute(self) -> int:
        from . import lock
        a = self.args
        self.run = new_run("stress-" + re.sub(r"[^\w.-]+", "-", f"{a.tandemkit}"),
                           {"kind": "stress", "title": f"TandemKit stress: Trio {a.trio} + TandemKit {a.tandemkit}",
                            "seed": self.seed, "deadline": dt.datetime.fromtimestamp(self.deadline).isoformat(
                                timespec="seconds"), "args": {k: v for k, v in vars(a).items() if k != "fn"}})
        self.findings = Findings(self.run.path / "findings.json")
        log(f"stress run {self.run.path} (seed {self.seed}, {self.left() / 60:.0f} min budget, cache {CACHE_DIR})")
        code = EXIT_CLEAN
        minimum = AFTER_BUILD_S
        try:
            with lock.hold(CACHE_DIR / "lane.lock", f"{holder_description()} (seed {self.seed})", wait=a.wait,
                           give_up=lambda: self.left() < minimum):
                try:
                    self.prepare_faketandem()
                    app, bundle_id = self.checkout_and_build()
                    if self.left() < AFTER_BUILD_S:
                        raise _Stop("built-only", self.build_only or f"built with {self.left() / 60:.0f} min left: "
                                    "not enough to run a round. The next run starts from a warm build.", EXIT_CLEAN)
                    self.start_faketandem()
                    self.install_and_seed(app, bundle_id)
                    self.launch(bundle_id)
                    self.bring_up_ui()
                    from . import recovery
                    ok, st = recovery.wait_for_session(self.fk.client, a.session_timeout + 60)
                    if not ok:
                        self.findings.add(Finding(
                            kind="no-recovery", title="TandemKit never connected to faketandem after launch",
                            signature=["initial-session"], component="TandemKit",
                            details=f"faketandem state: {st}\nThe pump was seeded as paired (PumpManagerState.plist) "
                                    "with the key faketandem holds; see tandemkit-oslog.txt and faketandem.log."))
                        raise _Stop("no-initial-session", "TandemKit never connected; no rounds run", EXIT_FINDINGS)
                    self.run.log_step("connected", "TandemKit authenticated with faketandem")
                    self.rounds_loop()
                    self.status = "completed"
                except _Stop as s:
                    self.status, self.note, code = s.status, s.note, s.code
                finally:
                    self.collect()
        except LaneBusy as e:
            self.status, self.note, code = "busy", f"Not run: {e}.", EXIT_BUSY
            log(str(e))
        except HarnessError as e:
            self.status, self.note, code = "harness-error", f"{type(e).__name__}: {e}", EXIT_HARNESS
            log(f"stress run failed in the harness: {e}")
        if code == EXIT_CLEAN and any(f.kind in FAILURE_KINDS for f in self.findings):
            code = EXIT_FINDINGS
        self.findings.save()
        text = self.summary(code)
        (self.run.path / "summary.md").write_text(text)
        result = {"status": self.status, "exit_code": code, "run_dir": str(self.run.path), "seed": self.seed,
                  "findings": len(self.findings), "kinds": sorted({f.kind for f in self.findings}),
                  "rounds": len(self.rounds)}
        (self.run.path / "result.json").write_text(json.dumps(result, indent=2))
        if a.result_json:
            Path(a.result_json).write_text(json.dumps(result, indent=2))
        self.run.finish("passed" if code == EXIT_CLEAN else "failed", self.note or self.status)
        print(text)
        print(f"run: {self.run.path}")
        return code


class _Stop(Exception):
    def __init__(self, status: str, note: str, code: int):
        super().__init__(note)
        self.status, self.note, self.code = status, note, code


def keep_awake() -> None:
    """A Mac left alone would otherwise idle-sleep partway through a long run."""
    if shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_run(args) -> int:
    from . import issues
    repo = None
    if args.file_issues:
        repo = args.issues_repo or issues.issues_repo()
        if not repo:
            raise HarnessError("--file-issues: no repository; pass --issues-repo owner/repo or set TKSTRESS_ISSUES_REPO")
        issues.check_gh(repo)
    keep_awake()
    lane = Lane(args)
    code = lane.execute()
    if repo and code in (EXIT_FINDINGS, EXIT_BUILD):
        try:
            issues.file_issues(lane.run.path, repo)
        except HarnessError as e:
            log(f"filing issues failed: {e}\nretry with: ./tandemkit-stress file-issues {lane.run.path} --repo {repo}")
    return code


def add_run_arguments(p) -> None:
    p.add_argument("--trio", default=DEFAULT_TRIO, help=f"Trio branch spec (default {DEFAULT_TRIO})")
    p.add_argument("--tandemkit", default=DEFAULT_TANDEMKIT,
                   help="TandemKit ref for Trio's submodule: a branch or sha, owner/repo@branch, or a PR (default dev)")
    p.add_argument("--faketandem-ref", default=None, help="faketandem branch/tag/sha (default FAKETANDEM_REF, main)")
    p.add_argument("--app", help="a Trio.app already built with TANDEMKIT_VIRTUAL_PUMP: skip checkout and build")
    p.add_argument("--minutes", type=float, default=60,
                   help="whole-run budget from now, build included (default 60; the nightly job runs 360)")
    p.add_argument("--deadline", help="end of the run as epoch seconds (overrides --minutes); CI passes its job limit")
    p.add_argument("--no-enforce-deadline", dest="enforce_deadline", action="store_false",
                   help="let the build run past the deadline instead of stopping it")
    p.add_argument("--round-minutes", type=float, default=4, help="chaos per round before each recovery check")
    p.add_argument("--seed", type=int, help="seed for the chaos plan (default: random, printed and recorded)")
    p.add_argument("--intensity", choices=["cycle", "low", "medium", "high"], default="cycle",
                   help="gap between actions; cycle (default) alternates medium, high, low, high by round")
    p.add_argument("--categories", help="comma-separated subset of pump,link,app,ui (default all available)")
    p.add_argument("--sanitizer", choices=["thread", "address", "none"], default="thread")
    p.add_argument("--main-thread-checker", action="store_true", help="inject Xcode's libMainThreadChecker.dylib")
    p.add_argument("--no-ui", action="store_true", help="no WebDriverAgent: pump, link and app chaos only")
    p.add_argument("--keep-installed", action="store_true", help="keep the app's data container from an earlier run")
    p.add_argument("--session-timeout", type=float, default=120, help="seconds to wait for a re-authenticated session")
    p.add_argument("--probe-timeout", type=float, default=60, help="seconds to wait for the probe's request")
    p.add_argument("--hang-seconds", type=float, default=45, help="an accessibility query slower than this is a hang")
    p.add_argument("--wait", action="store_true",
                   help="wait for another stress run on this machine (by hand or CI) instead of exiting with 4")
    p.add_argument("--file-issues", action="store_true",
                   help="file findings and build breaks as issues through `gh` (checked before the run starts)")
    p.add_argument("--issues-repo", help="owner/repo for --file-issues (default TKSTRESS_ISSUES_REPO, else this "
                                         "checkout's origin)")
    p.add_argument("--result-json", help="also write result.json here")
