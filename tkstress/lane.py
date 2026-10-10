"""`tandemkit-stress build` and `tandemkit-stress run`: Trio on the iOS Simulator against faketandem, with chaos.

    ./tandemkit-stress run                          # build closed-beta Trio + TandemKit dev, then 60 min of stress
    ./tandemkit-stress run --minutes 200            # about what each nightly job gets
    ./tandemkit-stress run --minutes 20 --seed 42 --intensity high
    ./tandemkit-stress run --tandemkit jwoglom/TandemKit#470   # a TandemKit PR instead of dev
    ./tandemkit-stress run --app build/…/Trio.app   # skip checkout and build (an app built with the virtual pump)
    ./tandemkit-stress run --minutes 200 --file-issues   # by hand on a Mac, filing issues through the local `gh` login
    ./tandemkit-stress build                        # only the build, into a new run directory (CI's first step)
    ./tandemkit-stress run --build runs/<run>       # stress the build that run directory holds (CI's second step)

The build is its own stage, and --minutes is the stress alone: the clock starts at the first round, so neither the
build nor the simulator setup nor collection counts against it.
  1. build (`build`, or the start of `run`): faketandem fetched and built (tkstress/faketandem.py), then Trio with
     its TandemKit submodule moved to the requested ref, compiled with TANDEMKIT_VIRTUAL_PUMP (CoreBluetooth
     replaced by a socket to faketandem) and, by default, ThreadSanitizer. build.json in the run directory records
     what was built, for `run --build`.
  2. setup: faketandem started on two free loopback ports with a random long-term key staged; fresh install,
     onboarding pre-seeded, Trio's settings and a paired Tandem pump written into the container (seed.py), launched
     with TANDEMKIT_VIRTUAL_PUMP=<faketandem> (app.py)
  3. rounds, for --minutes: chaos (chaos.py) for --round-minutes, then every fault undone and the recovery check
     (recovery.py); a dead or hung app is a finding and is relaunched; the app's memory and thread count are recorded
  4. collection: sanitizer and Main Thread Checker reports from every launch's stderr, crash reports, TandemKit's
     OSLog stream and log file, faketandem's request log; findings.json, summary.md, result.json

Exit codes: 0 clean (`build`: built), 1 findings, 2 harness/setup failure (or stopped by a signal), 3 Trio does not
build against that TandemKit, 4 another stress run on this machine holds the lane (tkstress/lock.py; --wait queues),
5 nothing was stress-tested: the build stopped at --build-minutes (the next build continues from DerivedData), or
--until left no time for a round.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import random
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from harness.util import HarnessError, log
from . import CACHE_DIR, HARNESS_DIR
from .lock import LaneBusy, holder_description
from .detect import (Finding, Findings, crash_finding, default_crash_dirs, find_crash_reports, main_thread_waiting,
                     parse_ips, sample_finding, stderr_reports)

DEFAULT_TRIO = "nightscout/Trio:feat/tandem-closed-beta"
DEFAULT_TANDEMKIT = "dev"
EXIT_CLEAN, EXIT_FINDINGS, EXIT_HARNESS, EXIT_BUILD, EXIT_BUSY, EXIT_INCOMPLETE = 0, 1, 2, 3, 4, 5
FAILURE_KINDS = {"crash", "tsan", "asan", "main-thread", "no-recovery", "hang", "app-died"}

MIN_ROUND_S = 60
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


def stress_window(started: float, minutes: float, until: float | None) -> tuple[float, bool]:
    """When the rounds end: --minutes after the first, or --until if that is sooner (True when it is)."""
    end = started + minutes * 60
    return (until, True) if until is not None and until < end else (end, False)


def open_run(path: Path):
    from harness.report import Run
    run = Run(path.expanduser().resolve())
    if not (run.path / "build.json").exists():
        raise HarnessError(f"{run.path} holds no build (build.json): make one with `tandemkit-stress build`")
    return run


def app_stamp(app: Path) -> list[int]:
    """The main executable's size and mtime: a rebuild of the app relinks it."""
    info = plistlib.loads((app / "Info.plist").read_bytes())
    st = (app / info.get("CFBundleExecutable", "Trio")).stat()
    return [st.st_size, st.st_mtime_ns]


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


def tail_lines(path: Path, n: int, max_bytes: int = 400_000) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            return fh.read().decode(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def format_actions(lines: list[str]) -> str:
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        params = " ".join(f"{k}={v}" for k, v in (r.get("params") or {}).items())
        out.append(f"{(r.get('t') or '')[11:]} r{r.get('round')} {r.get('category')}/{r.get('action')}"
                   + (f" {params}" if params else "") + f" -> {r.get('result')}"
                   + (f": {r['detail']}" if r.get("detail") else ""))
    return "\n".join(out)


def format_requests(lines: list[str]) -> str:
    """faketandem's request log, one message per line: time, seq, kind, message, txId, link state, faults, cargo."""
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "harness-note":
            out.append(f"[{e.get('note')}]")
            continue
        bits = [(e.get("time") or "")[11:23], f"#{e.get('seq')}", f"{e.get('kind', ''):<12}", e.get("message") or ""]
        if e.get("tx_id") is not None:
            bits.append(f"tx={e['tx_id']}")
        if e.get("connected") is not None:
            bits.append("connected" if e["connected"] else "disconnected")
        frags = e.get("fragments") or []
        if frags and e.get("fragments_sent", 0) != len(frags):
            bits.append(f"{e.get('fragments_sent', 0)}/{len(frags)} fragments sent")
        if e.get("fault"):
            bits.append(f"fault={e['fault']}")
        if e.get("note"):
            bits.append(str(e["note"]))
        if e.get("cargo"):
            bits.append(json.dumps(e["cargo"], separators=(",", ":"), default=str)[:300])
        out.append(" ".join(b for b in bits if b))
    return "\n".join(out)


class Lane:
    def __init__(self, args, issues_repo: str | None = None):
        self.args = args
        self.t0 = time.time()
        self.stress = hasattr(args, "minutes")
        self.deadline: float | None = None          # set when the first round starts
        self.stress_started: float | None = None
        self.cut_short = False
        seed = getattr(args, "seed", None)
        self.seed = seed if seed is not None else random.randrange(1, 2**31)
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
        self.req_seq = 0
        self.issues_repo = issues_repo
        self.publisher = None
        self.intensity_now: str | None = None
        self.stderr_seen: set[tuple[str, int]] = set()
        self.stderr_sizes: dict[str, int] = {}
        self.last_scan = 0.0

    def left(self) -> float:
        return self.deadline - time.time() if self.deadline else float("inf")

    # ---------------- findings -----------------

    def add_finding(self, f: Finding) -> None:
        """Record a finding; the first time its fingerprint appears, with the context its issue needs, filed at once."""
        if f.round is None and self.chaos and self.chaos.round:
            f.round = self.chaos.round
        new = f.fp not in self.findings.items
        if new:
            try:
                f.context = {**self.capture_context(), **f.context}
            except Exception as e:  # noqa: BLE001 - a finding without context beats no finding
                log(f"capturing context for {f.kind} failed: {type(e).__name__}: {e}")
        self.findings.add(f)
        if new and self.publisher:
            self.publisher.submit(asdict(f), self.run.meta)

    def capture_context(self) -> dict[str, str]:
        """What a reader of the issue would otherwise go to the run's logs for, as of now."""
        if self.fk and self.fk.alive():
            self.dump_requests()
        when = []
        if self.chaos and self.chaos.round:
            when.append(f"round {self.chaos.round}" + (f" ({self.intensity_now} chaos)" if self.intensity_now else ""))
        if self.stress_started:
            when.append(f"{(time.time() - self.stress_started) / 60:.0f} min into the stress")
        if self.app and self.app.launches:
            when.append(f"app launch {len(self.app.launches)}, pid {self.app.pid}")
            m = self.app.metrics()
            if m:
                when.append(f"{m.get('rss_mb')} MB resident, {m.get('threads')} threads")
        when.append(dt.datetime.now().isoformat(timespec="seconds"))
        p = self.run.path
        return {k: v for k, v in {
            "When": " · ".join(when),
            "Chaos actions before it (last 30)": format_actions(tail_lines(p / "actions.jsonl", 30)),
            "Messages faketandem saw (last 80)": format_requests(tail_lines(p / "faketandem-requests.jsonl", 80)),
            "TandemKit log (last 200 lines)": "\n".join(tail_lines(p / "tandemkit-oslog.txt", 200)),
        }.items() if v}

    def scan_stderr(self, *, final: bool = False) -> None:
        """New sanitizer and Main Thread Checker reports in the app's stderr. A report the live launch is still
        writing waits for the next scan; `final` takes everything."""
        if not self.app:
            return
        files = self.app.stderr_files()
        live = files[-1] if files and not final and self.app.alive() else None
        for p in files:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if p != live and self.stderr_sizes.get(p.name) == size:
                continue
            complete_scan = True
            for start, complete, f in stderr_reports(p.read_text(errors="replace")):
                if (p.name, start) in self.stderr_seen:
                    continue
                if not complete and p == live:
                    complete_scan = False
                    continue
                self.stderr_seen.add((p.name, start))
                f.evidence = [f"app/{p.name}"]
                self.add_finding(f)
            if complete_scan and p != live:
                self.stderr_sizes[p.name] = size

    def tick(self) -> bool:
        """Chaos's should_stop: also scans stderr every 30 s, so a sanitizer report is filed minutes after it."""
        if time.time() - self.last_scan > 30:
            self.last_scan = time.time()
            self.scan_stderr()
        return self.app.died_unexpectedly()

    # ---------------- stages -----------------

    def checkout_and_build(self):
        from harness import build as B
        a = self.args
        if getattr(a, "app", None):
            app = Path(a.app).expanduser().resolve()
            info = plistlib.loads((app / "Info.plist").read_bytes())
            self.run.update_meta(git={"note": "prebuilt app; commits unknown"})
            return app, info["CFBundleIdentifier"], None
        src, info = B.prepare_source(branch=a.trio, submodules=[tandemkit_spec(a.tandemkit)])
        self.run.update_meta(git={k: info.get(k) for k in ("branch", "sha", "describe", "submodules",
                                                            "submodule_overrides")})
        try:
            bi = B.build(src, "sim", configuration="Debug", git_info=info, log_path=self.run.path / "xcodebuild.log",
                         conditions=("TANDEMKIT_VIRTUAL_PUMP",), sanitizer=None if a.sanitizer == "none" else a.sanitizer,
                         save=False, timeout=a.build_minutes * 60 if a.build_minutes else None)
        except B.BuildTimeout as e:
            raise _Stop("build-incomplete", f"{e}. Nothing was stress-tested; the next build continues from "
                        "DerivedData.", EXIT_INCOMPLETE) from None
        except HarnessError as e:
            f = build_failure(str(e), a.trio, a.tandemkit)
            f.context = {"xcodebuild log (last 150 lines)": "\n".join(tail_lines(self.run.path / "xcodebuild.log", 150))}
            self.add_finding(f)
            raise _Stop("build-failed", "Trio did not build against this TandemKit", EXIT_BUILD) from None
        return Path(bi.app_path), bi.bundle_id, bi.xcode

    def build_phase(self) -> tuple[Path, str]:
        a = self.args
        t = time.time()
        self.prepare_faketandem()
        app, bundle_id, xcode = self.checkout_and_build()
        build = {"app": str(app), "bundle_id": bundle_id, "stamp": app_stamp(app), "xcode": xcode,
                 "trio": a.trio, "tandemkit": a.tandemkit, "sanitizer": a.sanitizer,
                 "minutes": round((time.time() - t) / 60, 1),
                 "faketandem": {"binary": str(self.fk.binary), "jar": str(self.fk.jar), "info": self.fk.info}}
        (self.run.path / "build.json").write_text(json.dumps(build, indent=2))
        self.run.update_meta(build={k: v for k, v in build.items() if k != "faketandem"})
        log(f"built in {build['minutes']:.0f} min: {app}")
        return app, bundle_id

    def load_build(self) -> tuple[Path, str]:
        """The build `tandemkit-stress build` left in this run directory, refused if the app changed since."""
        from .faketandem import Faketandem
        b = json.loads((self.run.path / "build.json").read_text())
        app = Path(b["app"])
        if not app.exists() or app_stamp(app) != b["stamp"]:
            raise HarnessError(f"{app} is not the app this run's build step made: it was rebuilt or removed since "
                               "(another stress run on this machine?); build again")
        for k in ("trio", "tandemkit", "sanitizer"):
            setattr(self.args, k, b[k])
        fk = b["faketandem"]
        self.fk = Faketandem(Path(fk["binary"]), Path(fk["jar"]), fk["info"])
        return app, b["bundle_id"]

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

    def check_app(self, r: int) -> None:
        if self.app.died_unexpectedly():
            pid = self.app.pid
            time.sleep(3)          # ReportCrash needs a moment to write the .ips
            self.add_finding(Finding(kind="app-died", title="Trio exited without the harness ending it",
                                     signature=["app-died"], round=r,
                                     details=f"pid {pid} (launch {len(self.app.launches)}) exited; its crash report, "
                                             "if one was written, is filed as its own finding"))
            self.collect_crashes()
            self.scan_stderr()
            self.app.launch()

    def check_hang(self, r: int, *, foreground_timed_out: bool = False) -> bool:
        """A live app that leaves a WebDriverAgent query (without one, the quiesce's `simctl launch`) unanswered for
        minutes, and whose sample does not show the main thread waiting for events, is a hang. It is relaunched: a
        deadlock never clears, and left alone it is sampled again every round."""
        if not self.app.alive():
            return False
        if self.ui is not None:
            t = time.time()
            try:
                self.ui.find("Settings", exact=True)
            except Exception:  # noqa: BLE001 - the timing is what matters
                pass
            took = time.time() - t
            if took < self.args.hang_seconds:
                return False
            what = f"accessibility query took {took:.0f}s"
        elif foreground_timed_out:
            what = "simctl launch timed out"
        else:
            return False
        if not self.app.alive():
            return False
        dest = self.run.path / "hangs" / f"round-{r:02d}.txt"
        dest.parent.mkdir(exist_ok=True)
        if self.app.sample(dest):
            text = dest.read_text(errors="replace")
            if main_thread_waiting(text):
                log(f"round {r}: the app was slow to answer, but its main thread is waiting for events: not a hang")
                return False
            f = sample_finding(text, what=what)
            f.round, f.evidence = r, [str(dest.relative_to(self.run.path))]
            self.add_finding(f)
        self.app.kill()
        self.app.launch()
        return True

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
        while r == 0 or self.left() >= MIN_ROUND_S:
            r += 1
            self.chaos.round = r
            intensity = INTENSITY_CYCLE[(r - 1) % len(INTENSITY_CYCLE)] if a.intensity == "cycle" else a.intensity
            self.chaos.gaps = INTENSITY_GAPS[intensity]
            self.intensity_now = intensity
            span = min(round_s, max(self.left(), MIN_ROUND_S))
            log(f"== round {r}: {intensity} chaos for {span / 60:.1f} min")
            recs = self.chaos.run_for(span, should_stop=self.tick)
            self.check_app(r)
            quiesced = self.chaos.quiesce() or []
            foreground_timed_out = any(x["action"] == "quiesce_app" and x["result"] == "error"
                                       and "timed out" in x["detail"] for x in quiesced)
            res = recovery.check(self.fk.client, session_timeout=a.session_timeout, probe_timeout=a.probe_timeout)
            self.dump_requests()
            applied = sum(1 for x in recs if x["result"] == "ok" and x["category"] not in ("undo", "quiesce"))
            metrics = self.app.metrics() if hasattr(self.app, "metrics") else {}
            self.rounds.append({"round": r, "intensity": intensity, "actions": applied, **metrics, **res})
            log(f"round {r}: {applied} actions applied; recovery {'ok' if res['ok'] else 'FAILED'} "
                f"({res.get('stage')}, {res.get('seconds')}s)")
            # a hung app cannot reconnect either: the hang is the finding, not TandemKit
            if not self.check_hang(r, foreground_timed_out=foreground_timed_out) and not res["ok"]:
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
                try:
                    pump = self.fk.client.snapshot()
                except HarnessError as e:
                    pump = f"unavailable: {e}"
                self.add_finding(Finding(
                    kind="no-recovery", title=f"TandemKit did not recover after the faults stopped ({res['stage']} check)",
                    signature=[res["stage"]], component="TandemKit", round=r, evidence=ev,
                    details=json.dumps({"recovery check": res, "faketandem state after it": pump}, indent=2,
                                       default=str)))
            self.check_app(r)
            self.scan_stderr()
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
            self.add_finding(f)

    def collect(self) -> None:
        for step, fn in (("chaos quiesce", lambda: self.chaos and self.chaos.quiesce()),
                         ("request log", self.dump_requests),
                         ("log stream", lambda: self.logstream and self.logstream.stop()),
                         ("app", lambda: self.app and self.app.terminate()),
                         ("crash reports", self.collect_crashes),
                         ("sanitizer reports", lambda: self.scan_stderr(final=True)),
                         ("app logs", self.copy_app_logs),
                         ("faketandem", lambda: self.fk and self.fk.stop())):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - collect everything that can be collected
                log(f"collect: {step} failed: {type(e).__name__}: {e}")

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
        build = meta.get("build") or {}
        lines = [f"## TandemKit stress (simulator + faketandem): {self.status}", "",
                 f"Trio `{self.args.trio}` @ `{git.get('sha', '?')}` · TandemKit `{self.args.tandemkit}` @ "
                 f"`{subs.get('TandemKit', '?')}` · faketandem `{fk.get('ref', fk.get('source', '?'))}` @ "
                 f"`{fk.get('sha', '?')}`",
                 " · ".join([f"sanitizer `{self.args.sanitizer}`"]
                            + ([f"built in {build['minutes']:.0f} min"] if build.get("minutes") is not None else [])
                            + ([f"Xcode {build['xcode']}"] if build.get("xcode") else []))]
        if self.stress:
            stress_min = (time.time() - self.stress_started) / 60 if self.stress_started else 0
            window = (f"{stress_min:.0f} min (cut from {self.args.minutes:g} by --until, the job's time limit)"
                      if self.cut_short else f"{stress_min:.0f} of {self.args.minutes:g} min")
            lines.append(f"seed `{self.seed}` · intensity `{self.args.intensity}` · {len(self.rounds)} rounds in {window} · "
                         f"{applied} actions applied ({skipped} skipped, {errors} errors) · "
                         f"{len(self.app.launches) if self.app else 0} app launches")
        lines.append("")
        if self.note:
            lines += [self.note, ""]
        if failures:
            from .issues import read_ledger
            filed = read_ledger(self.run.path)
            lines += [f"### Findings ({len(failures)})", "", "| kind | component | finding | fingerprint | seen | issue |",
                      "|---|---|---|---|---|---|"]
            lines += [f"| {f.kind} | {f.component} | {f.title.replace('|', '/')} | `{f.fp}` | {f.occurrences} | "
                      f"{filed.get(f.fp, {}).get('url', '')} |" for f in failures]
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
        tk = subs.get("TandemKit") or self.args.tandemkit
        if self.stress:
            lines.append(f"Reproduce: `./tandemkit-stress run --trio {self.args.trio} --tandemkit {tk} --seed {self.seed} "
                         f"--intensity {self.args.intensity}` (the seed fixes the plan of actions, not thread timing)")
        else:
            lines.append(f"Rebuild: `./tandemkit-stress build --trio {self.args.trio} --tandemkit {tk}`")
        return "\n".join(lines) + "\n"

    # ---------------- driver -----------------

    def stress_phase(self, app: Path, bundle_id: str) -> None:
        from . import recovery
        a = self.args
        self.start_faketandem()
        self.install_and_seed(app, bundle_id)
        self.launch(bundle_id)
        self.bring_up_ui()
        ok, st = recovery.wait_for_session(self.fk.client, a.session_timeout + 60)
        if not ok:
            self.add_finding(Finding(
                kind="no-recovery", title="TandemKit never connected to faketandem after launch",
                signature=["initial-session"], component="TandemKit",
                details=f"faketandem state: {st}\nThe pump was seeded as paired (PumpManagerState.plist) "
                        "with the key faketandem holds; see tandemkit-oslog.txt and faketandem.log."))
            raise _Stop("no-initial-session", "TandemKit never connected; no rounds run", EXIT_FINDINGS)
        self.run.log_step("connected", "TandemKit authenticated with faketandem")
        self.stress_started = time.time()
        self.deadline, self.cut_short = stress_window(self.stress_started, a.minutes, a.until)
        if self.cut_short and self.left() < MIN_ROUND_S:
            raise _Stop("no-time", "--until (the job's time limit) left no time for a round after setup: nothing was "
                                   "stress-tested.", EXIT_INCOMPLETE)
        self.rounds_loop()

    def execute(self) -> int:
        from . import lock
        a = self.args
        reuse = getattr(a, "build", None)
        own = {k: v for k, v in vars(a).items() if k != "fn"}
        if reuse:
            self.run = open_run(Path(reuse))
            meta = self.run.meta
            self.run.update_meta(seed=self.seed, status="running", args={**(meta.get("args") or {}), **own})
        else:
            self.run = new_run("stress-" + re.sub(r"[^\w.-]+", "-", f"{a.tandemkit}"),
                               {"kind": "stress", "title": f"TandemKit stress: Trio {a.trio} + TandemKit {a.tandemkit}",
                                "args": own, **({"seed": self.seed} if self.stress else {})})
        self.findings = Findings(self.run.path / "findings.json")
        if self.issues_repo:
            from .issues import Publisher
            self.publisher = Publisher(self.issues_repo, self.run.path)
        log(f"{'stress run' if self.stress else 'build'} {self.run.path} (cache {CACHE_DIR}"
            + (f", seed {self.seed}, {a.minutes:g} min of stress after setup)" if self.stress else ")"))
        code = EXIT_CLEAN
        what = f"{holder_description()} ({f'seed {self.seed}' if self.stress else 'building'})"
        try:
            with lock.hold(CACHE_DIR / "lane.lock", what, wait=a.wait):
                try:
                    app, bundle_id = self.load_build() if reuse else self.build_phase()
                    if self.stress:
                        self.stress_phase(app, bundle_id)
                    self.status = "completed" if self.stress else "built"
                except _Stop as s:
                    self.status, self.note, code = s.status, s.note, s.code
                except KeyboardInterrupt:
                    self.status, code = "interrupted", EXIT_HARNESS
                    self.note = "Stopped by a signal (job cancelled, or the runner shutting down) before the run ended."
                finally:
                    if self.stress:
                        self.collect()
        except LaneBusy as e:
            self.status, self.note, code = "busy", f"Not run: {e}.", EXIT_BUSY
            log(str(e))
        except HarnessError as e:
            self.status, self.note, code = "harness-error", f"{type(e).__name__}: {e}", EXIT_HARNESS
            log(f"{'stress run' if self.stress else 'build'} failed in the harness: {e}")
        if code == EXIT_CLEAN and any(f.kind in FAILURE_KINDS for f in self.findings):
            code = EXIT_FINDINGS
        self.findings.save()
        if self.publisher:
            self.publisher.close()
        text = self.summary(code)
        (self.run.path / "summary.md").write_text(text)
        result = {"status": self.status, "exit_code": code, "run_dir": str(self.run.path),
                  "findings": len(self.findings), "kinds": sorted({f.kind for f in self.findings})}
        if self.stress:
            result.update(seed=self.seed, rounds=len(self.rounds))
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


def _lane_with_issues(args) -> int:
    from . import issues
    repo = None
    if args.file_issues:
        repo = args.issues_repo or issues.issues_repo()
        if not repo:
            raise HarnessError("--file-issues: no repository; pass --issues-repo owner/repo or set TKSTRESS_ISSUES_REPO")
        issues.check_gh(repo)
    keep_awake()
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    return Lane(args, issues_repo=repo).execute()


def cmd_run(args) -> int:
    if args.build and args.app:
        raise HarnessError("--build and --app both name the app to stress; pass one")
    return _lane_with_issues(args)


def cmd_build(args) -> int:
    return _lane_with_issues(args)


def add_build_arguments(p) -> None:
    g = p.add_argument_group("build (with `run --build`, taken from that build instead)")
    g.add_argument("--trio", default=DEFAULT_TRIO, help=f"Trio branch spec (default {DEFAULT_TRIO})")
    g.add_argument("--tandemkit", default=DEFAULT_TANDEMKIT,
                   help="TandemKit ref for Trio's submodule: a branch or sha, owner/repo@branch, or a PR (default dev)")
    g.add_argument("--faketandem-ref", default=None, help="faketandem branch/tag/sha (default FAKETANDEM_REF, main)")
    g.add_argument("--sanitizer", choices=["thread", "address", "none"], default="thread")
    g.add_argument("--build-minutes", type=float,
                   help="stop xcodebuild after this many minutes (exit 5, status build-incomplete; the next build "
                        "continues from DerivedData). Default: no limit")
    p.add_argument("--wait", action="store_true",
                   help="wait for another stress run on this machine (by hand or CI) instead of exiting with 4")
    p.add_argument("--file-issues", action="store_true",
                   help="file findings and build breaks as issues through `gh` (checked before anything starts)")
    p.add_argument("--issues-repo", help="owner/repo for --file-issues (default TKSTRESS_ISSUES_REPO, else this "
                                         "checkout's origin)")
    p.add_argument("--result-json", help="also write result.json here")


def add_run_arguments(p) -> None:
    add_build_arguments(p)
    g = p.add_argument_group("stress")
    g.add_argument("--minutes", type=float, default=60,
                   help="minutes of stress, from the first round: the build, setup and collection are not counted "
                        "(default 60)")
    g.add_argument("--until", type=float, metavar="EPOCH",
                   help="end the stress by this time (epoch seconds) even if --minutes has not run out; CI passes "
                        "its runner's job limit less the time collection and uploads need")
    g.add_argument("--build", metavar="RUN_DIR", help="stress the build `tandemkit-stress build` made in RUN_DIR, "
                                                       "and add this run's results to that directory")
    g.add_argument("--app", help="a Trio.app already built with TANDEMKIT_VIRTUAL_PUMP: skip checkout and build")
    g.add_argument("--round-minutes", type=float, default=4, help="chaos per round before each recovery check")
    g.add_argument("--seed", type=int, help="seed for the chaos plan (default: random, printed and recorded)")
    g.add_argument("--intensity", choices=["cycle", "low", "medium", "high"], default="cycle",
                   help="gap between actions; cycle (default) alternates medium, high, low, high by round")
    g.add_argument("--categories", help="comma-separated subset of pump,link,app,ui (default all available)")
    g.add_argument("--main-thread-checker", action="store_true", help="inject Xcode's libMainThreadChecker.dylib")
    g.add_argument("--no-ui", action="store_true", help="no WebDriverAgent: pump, link and app chaos only")
    g.add_argument("--keep-installed", action="store_true", help="keep the app's data container from an earlier run")
    g.add_argument("--session-timeout", type=float, default=120, help="seconds to wait for a re-authenticated session")
    g.add_argument("--probe-timeout", type=float, default=60, help="seconds to wait for the probe's request")
    g.add_argument("--hang-seconds", type=float, default=45, help="an accessibility query slower than this is a hang")
