"""Tests for the stress lane's parts that run without Xcode: detectors, seeding, chaos scheduling, issue bodies.

    .venv/bin/python -m unittest discover -s tests -v
    FAKETANDEM_BIN=… FAKETANDEM_JAR=… .venv/bin/python -m unittest discover -s tests   # + a live emulator
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import random
import tempfile
import unittest
from pathlib import Path

from tkstress import chaos as C
from tkstress import detect as D
from tkstress import issues as I
from tkstress import seed as S
from harness.util import HarnessError

FIX = Path(__file__).parent / "fixtures"


class SanitizerReports(unittest.TestCase):
    def test_linux_reports_parse_into_one_finding_per_race(self):
        reports = D.parse_sanitizer_reports((FIX / "tsan-linux.txt").read_text())
        self.assertEqual(len(reports), 2)
        findings = [D.sanitizer_finding(r) for r in reports]
        self.assertEqual({f.kind for f in findings}, {"tsan"})
        self.assertEqual({f.component for f in findings}, {"TandemKit"})
        self.assertEqual(len({f.fp for f in findings}), 2)

    def test_only_the_access_stacks_make_the_signature(self):
        jpake = D.sanitizer_finding(D.parse_sanitizer_reports((FIX / "tsan-linux.txt").read_text())[0])
        self.assertIn("clearInstance", jpake.signature[1])
        self.assertFalse(any("pthread_create" in s or "nanosleep" in s for s in jpake.signature))

    def test_fingerprint_ignores_line_numbers_and_paths(self):
        text = (FIX / "tsan-linux.txt").read_text()
        moved = text.replace("JpakeAuthBuilder.swift:67:18", "JpakeAuthBuilder.swift:71:18").replace(
            "/work/TandemKit/", "/Users/someone/src/TandemKit/")
        a = D.sanitizer_finding(D.parse_sanitizer_reports(text)[0])
        b = D.sanitizer_finding(D.parse_sanitizer_reports(moved)[0])
        self.assertEqual(a.fp, b.fp)

    def test_darwin_report_skips_system_frames_and_names_the_driver(self):
        findings = D.scan_stderr((FIX / "tsan-darwin.txt").read_text())
        tsan = [f for f in findings if f.kind == "tsan"]
        self.assertEqual(len(tsan), 1)
        f = tsan[0]
        self.assertEqual(f.component, "TandemKit")
        self.assertIn("bolusProgressReport.getter", f.title)
        self.assertFalse(any("libdispatch" in s or "_dispatch" in s for s in f.signature))
        self.assertFalse(any("malloc" in s or "init(state" in s for s in f.signature))  # allocation stack excluded

    def test_main_thread_checker(self):
        mtc = [f for f in D.scan_stderr((FIX / "tsan-darwin.txt").read_text()) if f.kind == "main-thread"]
        self.assertEqual(len(mtc), 1)
        self.assertIn("setNeedsLayout", mtc[0].title)
        self.assertEqual(mtc[0].component, "TandemKit")


class CrashAndHangReports(unittest.TestCase):
    def test_ips(self):
        crash = D.parse_ips(FIX / "trio-crash.ips")
        self.assertEqual(crash["proc"], "Trio")
        f = D.crash_finding(crash)
        self.assertEqual(f.kind, "crash")
        self.assertEqual(f.component, "TandemKit")
        self.assertIn("HistoryLogStreamResponse.init(cargo:)", f.title)
        self.assertIn("Index out of range", f.details)
        self.assertFalse(any("assertionFailure" in s for s in f.signature))

    def test_find_crash_reports_by_process_and_time(self):
        with tempfile.TemporaryDirectory() as d:
            old, new, other = Path(d, "Trio-1.ips"), Path(d, "Trio-2.ips"), Path(d, "Other-1.ips")
            for p in (old, new, other):
                p.write_text("{}\n{}")
            os.utime(old, (1000, 1000))
            self.assertEqual(D.find_crash_reports([Path(d)], since=2000, proc="Trio"), [new])

    def test_sample_hang_names_the_deepest_app_frame(self):
        f = D.sample_finding((FIX / "trio-hang-sample.txt").read_text(), what="test")
        self.assertEqual(f.kind, "hang")
        self.assertIn("TandemPumpManager.status.getter", f.title)
        self.assertNotIn("commandQueue", f.details)          # only the main thread's stack

    def test_findings_reopen_with_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            first = D.Findings(Path(d) / "findings.json")
            first.add(D.Finding(kind="build", title="t", signature=["x"]))
            again = D.Findings(Path(d) / "findings.json")
            self.assertEqual([f.title for f in again], ["t"])
            again.add(D.Finding(kind="build", title="t", signature=["x"]))
            self.assertEqual(next(iter(again)).occurrences, 2)

    def test_findings_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            fs = D.Findings(Path(d, "findings.json"))
            a = D.Finding(kind="tsan", title="x", signature=["a", "b"])
            self.assertTrue(fs.add(a))
            self.assertFalse(fs.add(D.Finding(kind="tsan", title="y", signature=["a", "b"])))
            self.assertEqual(len(fs), 1)
            self.assertEqual(D.Findings.load(Path(d, "findings.json"))[0].occurrences, 2)


class Seeding(unittest.TestCase):
    def test_pump_manager_plist(self):
        raw = S.tandem_pump_manager_raw(peripheral_id="062878e9-7462-5a51-9c1f-c958acbff1bc",
                                        long_term_key=b"\x01" * 32, pairing_code="123456", time_zone="UTC")
        self.assertEqual(raw["managerIdentifier"], "TandemPumpManager")
        ps = raw["state"]["pumpState"]
        self.assertEqual(ps["peripheralUuid"], "062878E9-7462-5A51-9C1F-C958ACBFF1BC")   # uuidString is upper case
        self.assertIsInstance(ps["derivedSecret"], bytes)
        self.assertTrue(raw["state"]["hasSyncedBasalRateSchedule"])
        with tempfile.TemporaryDirectory() as d:
            p = S.write_pump_state(Path(d), raw)
            back = plistlib.loads(p.read_bytes())
            self.assertEqual(back["state"]["pumpState"]["derivedSecret"], b"\x01" * 32)
            self.assertEqual(back["state"]["basalRateSchedule"]["items"][0]["value"], S.BASAL_RATE)

    def test_settings_overlay_the_bundled_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            app, container = Path(d, "Trio.app"), Path(d, "container")
            (app / "json/defaults/freeaps").mkdir(parents=True)
            (app / "json/defaults/freeaps/freeaps_settings.json").write_text(
                json.dumps({"dosingMode": "open", "cgm": "none", "units": "mg/dL"}))
            S.write_trio_settings(container, app=app)
            got = json.loads((container / "Documents/freeaps/freeaps_settings.json").read_text())
            self.assertEqual(got, {"dosingMode": "closed", "cgm": "simulator", "units": "mg/dL", "cgmPluginIdentifier": ""})
            basal = json.loads((container / "Documents/settings/basal_profile.json").read_text())
            self.assertEqual(basal[0]["rate"], S.BASAL_RATE)
            self.assertEqual(S.read_back(container)["freeaps/freeaps_settings.json"]["dosingMode"], "closed")


class FakePump:
    """Enough of harness.faketandem.Client for the chaos actions."""

    def __init__(self, connected: bool = True):
        self.connected = connected
        self.calls: list[tuple] = []
        self.state = {"bolus": {"active": False}, "basal": {"suspended": False, "temp_active": False}}

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append((name, a, k))
            if name == "qualifying_event" and not self.connected:
                raise HarnessError("faketandem POST /api/state/qualifyingevent -> 409: no notifier")
            if name == "suspend":
                self.state["basal"]["suspended"] = True
            if name == "resume":
                self.state["basal"]["suspended"] = False
            return {"state": self.state}
        return call

    def snapshot(self):
        return self.state


class FakeControl:
    def __init__(self):
        self.disconnects = 0

    def disconnect_pump(self):
        self.disconnects += 1


def _plan(seed: int, steps: int = 25) -> list[str]:
    import time as _t
    real_sleep = _t.sleep
    _t.sleep = lambda s: None
    try:
        ch = C.Chaos(C.ChaosContext(random.Random(seed), FakePump(), FakeControl()), categories={"pump", "link"})
        return [r["action"] for _ in range(steps) for r in ch.step() if r["category"] != "undo"]
    finally:
        _t.sleep = real_sleep


class Chaos(unittest.TestCase):
    def test_a_seed_fixes_the_plan(self):
        self.assertEqual(_plan(7), _plan(7))
        self.assertNotEqual(_plan(7), _plan(8))

    def test_without_app_or_ui_those_actions_are_not_offered(self):
        ch = C.Chaos(C.ChaosContext(random.Random(1), FakePump(), FakeControl()))
        self.assertFalse({a.category for a in ch.actions} & {"app", "ui"})

    def test_no_session_is_a_skip_not_an_error(self):
        ctx = C.ChaosContext(random.Random(1), FakePump(connected=False), FakeControl())
        ch = C.Chaos(ctx, actions=[C.Action("qe_burst", "pump", 1, C.qe_burst)])
        rec = ch.step()[-1]
        self.assertEqual(rec["result"], "skipped")

    def test_quiesce_runs_pending_undos_and_resets_the_pump(self):
        pump = FakePump()
        ctx = C.ChaosContext(random.Random(3), pump, FakeControl())
        ch = C.Chaos(ctx, actions=[C.Action("pump_suspend", "pump", 1, C.pump_suspend)])
        ch.step()
        self.assertTrue(pump.state["basal"]["suspended"])
        recs = ch.quiesce()
        self.assertFalse(pump.state["basal"]["suspended"])
        self.assertIn("pump_resume", [r["action"] for r in recs])
        names = [c[0] for c in pump.calls]
        self.assertIn("clear_faults", names)
        self.assertIn({"kind": "radio_on"}, [c[1][0] for c in pump.calls if c[0] == "arm_fault"])

    def test_app_actions_skip_while_the_app_is_down(self):
        class DownApp:
            killed = 0

            def alive(self):
                return False

            def kill(self):
                DownApp.killed += 1

        ctx = C.ChaosContext(random.Random(1), FakePump(), FakeControl(), app=DownApp())
        ch = C.Chaos(ctx, actions=[C.Action("app_kill", "app", 1, C.app_kill)])
        self.assertEqual(ch.step()[-1]["result"], "skipped")
        self.assertEqual(DownApp.killed, 0)
        self.assertEqual(ctx.pop_all(), [])                       # no relaunch queued

    def test_no_actions_left_is_an_error(self):
        with self.assertRaises(HarnessError):
            C.Chaos(C.ChaosContext(random.Random(1), FakePump(), FakeControl()), categories={"ui"})


class Issues(unittest.TestCase):
    def test_scrub(self):
        key = "ab" * 32
        out = I.scrub(f"key={key} home={Path.home()}/x b64=" + "A" * 50)
        self.assertNotIn(key, out)
        self.assertNotIn(str(Path.home()), out)
        self.assertNotIn("A" * 50, out)

    def test_body_carries_the_fingerprint_marker_and_a_repro(self):
        f = D.Finding(kind="tsan", title="race", signature=["data race", "TandemKit`x"], component="TandemKit",
                      details="d" * 10)
        from dataclasses import asdict
        meta = {"seed": 5, "args": {"trio": "nightscout/Trio:feat/tandem-closed-beta", "tandemkit": "dev",
                                    "sanitizer": "thread", "intensity": "medium"},
                "git": {"sha": "abc", "submodules": {"TandemKit": "def"}}, "faketandem": {"ref": "main", "sha": "123"}}
        body = I.issue_body(asdict(f), meta, Path("."))
        self.assertIn(I.MARKER.format(fp=f.fp), body)
        self.assertIn("--tandemkit def --seed 5", body)

    def test_repository_from_a_github_remote(self):
        for url in ("https://github.com/jwoglom/tandemkit-stress.git", "https://github.com/jwoglom/tandemkit-stress",
                    "git@github.com:jwoglom/tandemkit-stress.git", "ssh://git@github.com/jwoglom/tandemkit-stress.git"):
            self.assertEqual(I.repo_from_remote(url + "\n"), "jwoglom/tandemkit-stress")
        self.assertIsNone(I.repo_from_remote("https://gitlab.com/jwoglom/tandemkit-stress.git"))

    def test_a_run_started_by_hand_names_its_run_directory(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": ""}):
            line = I.run_line(Path("/somewhere/runs/20261001-020000-stress-dev"))
        self.assertIn("`runs/20261001-020000-stress-dev`", line)
        self.assertNotIn("/somewhere", line)

    def test_build_failure_skips_quoted_source_lines(self):
        from tkstress.lane import build_failure
        f = build_failure("xcodebuild failed. Errors:\n    public private (set) var error: Error?\n"
                          "/a/TandemKit/X.swift:10:5: error: cannot find 'foo' in scope", "t", "dev")
        self.assertIn("cannot find 'foo' in scope", f.signature[0])

    def test_build_failure_fingerprint_ignores_paths_and_lines(self):
        from tkstress.lane import build_failure
        a = build_failure("/Users/a/Trio/TandemKit/X.swift:10:5: error: cannot find 'foo' in scope", "t", "dev")
        b = build_failure("/Volumes/w/Trio/TandemKit/X.swift:12:9: error: cannot find 'foo' in scope", "t", "dev")
        self.assertEqual(a.fp, b.fp)


class Lock(unittest.TestCase):
    def test_a_second_run_is_refused_and_told_who_holds_the_lane(self):
        from tkstress.lock import LaneBusy, hold
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lane.lock"
            with hold(path, "GitHub Actions run 1 (Stress nightly)"):
                with self.assertRaises(LaneBusy) as cm:
                    with hold(path, "by hand"):
                        pass
                self.assertIn("Stress nightly", str(cm.exception))
            with hold(path, "by hand"):
                pass

    def test_a_waiting_run_gives_up_when_told_to(self):
        from tkstress.lock import LaneBusy, hold
        polls = []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lane.lock"
            with hold(path, "first"):
                with self.assertRaises(LaneBusy):
                    with hold(path, "second", wait=True, give_up=lambda: polls.append(1) or len(polls) > 2, poll=0.01):
                        pass
        self.assertEqual(len(polls), 3)


class Lane(unittest.TestCase):
    def test_a_busy_lane_exits_4_with_a_summary(self):
        from unittest import mock
        from harness.config import CONFIG
        from tkstress import lane as L
        from tkstress.cli import build_parser
        from tkstress.lock import hold
        args = build_parser().parse_args(["run", "--minutes", "30", "--seed", "7"])
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(CONFIG.values, {"TH_RUNS_DIR": d}), mock.patch.object(L, "CACHE_DIR", Path(d)):
            with hold(Path(d) / "lane.lock", "the nightly"), contextlib.redirect_stdout(io.StringIO()):
                code = L.Lane(args).execute()
            result = json.loads(next(Path(d).glob("*/result.json")).read_text())
        self.assertEqual(code, L.EXIT_BUSY)
        self.assertEqual(result["status"], "busy")

    def _fake_build_env(self, d: str, build_side_effect=None):
        """A run directory root, lane lock and a stand-in app, faketandem and xcodebuild under `d`."""
        from unittest import mock
        from harness import build as B
        from harness.config import CONFIG
        from tkstress import faketandem as FT
        from tkstress import lane as L
        app = Path(d) / "Trio.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.example.trio",
                                                        "CFBundleExecutable": "Trio"}))
        (app / "Trio").write_bytes(b"binary")
        built = mock.Mock(app_path=str(app), bundle_id="org.example.trio", xcode="26.2")
        fk = FT.Faketandem(Path(d) / "faketandem", Path(d) / "cliparser.jar", {"ref": "main", "sha": "abc"})
        return app, [mock.patch.dict(CONFIG.values, {"TH_RUNS_DIR": d}), mock.patch.object(L, "CACHE_DIR", Path(d)),
                     mock.patch.object(FT.Faketandem, "prepare", return_value=fk),
                     mock.patch.object(B, "prepare_source", return_value=(Path(d), {"sha": "abc"})),
                     mock.patch.object(B, "build", side_effect=build_side_effect, return_value=built)]

    def test_build_records_the_app_and_run_build_refuses_one_rebuilt_since(self):
        from tkstress import lane as L
        from tkstress.cli import build_parser
        with tempfile.TemporaryDirectory() as d:
            app, patches = self._fake_build_env(d)
            with contextlib.ExitStack() as stack, contextlib.redirect_stdout(io.StringIO()):
                for pt in patches:
                    stack.enter_context(pt)
                code = L.Lane(build_parser().parse_args(["build", "--tandemkit", "my-branch"])).execute()
                run_dir = next(Path(d).glob("*-stress-my-branch"))
                self.assertEqual(code, L.EXIT_CLEAN)
                self.assertEqual(json.loads((run_dir / "result.json").read_text())["status"], "built")
                lane = L.Lane(build_parser().parse_args(["run", "--build", str(run_dir), "--minutes", "10"]))
                lane.run = L.open_run(run_dir)
                self.assertEqual(lane.load_build(), (app, "org.example.trio"))
                self.assertEqual(lane.args.tandemkit, "my-branch")
                (app / "Trio").write_bytes(b"another build")
                with self.assertRaises(HarnessError):
                    lane.load_build()

    def test_a_build_stopped_at_its_limit_is_incomplete_not_failed(self):
        from harness import build as B
        from tkstress import lane as L
        from tkstress.cli import build_parser
        with tempfile.TemporaryDirectory() as d:
            _, patches = self._fake_build_env(d, build_side_effect=B.BuildTimeout("xcodebuild stopped after 90 min"))
            with contextlib.ExitStack() as stack, contextlib.redirect_stdout(io.StringIO()):
                for pt in patches:
                    stack.enter_context(pt)
                code = L.Lane(build_parser().parse_args(["build", "--build-minutes", "90"])).execute()
                result = json.loads(next(Path(d).glob("*/result.json")).read_text())
        self.assertEqual(code, L.EXIT_CLEAN)
        self.assertEqual(result["status"], "build-incomplete")

    def test_minutes_is_the_stress_alone(self):
        from unittest import mock
        from tkstress import lane as L
        from tkstress.cli import build_parser
        clock = [1000.0]
        spans = []

        class FakeChaos:
            def __init__(self, *_, **__):
                self.counts, self.round, self.gaps = {}, 0, None

            def run_for(self, span, should_stop=None):
                spans.append(span)
                clock[0] += span
                return []

            def quiesce(self):
                pass

        def check(*_, **__):
            clock[0] += 30
            return {"ok": True, "stage": "probe", "seconds": 30}
        lane = L.Lane(build_parser().parse_args(["run", "--minutes", "10"]))
        lane.run, lane.fk, lane.ui = mock.Mock(path=Path(tempfile.gettempdir())), mock.Mock(), None
        lane.app = mock.Mock(died_unexpectedly=lambda: False, metrics=lambda: {})
        lane.dump_requests = lambda: None
        with mock.patch.object(L, "time", mock.Mock(time=lambda: clock[0])), \
                mock.patch("tkstress.chaos.Chaos", FakeChaos), mock.patch("tkstress.recovery.check", check):
            self.assertEqual(lane.left(), float("inf"))
            lane.deadline = clock[0] + 10 * 60
            lane.rounds_loop()
        self.assertEqual(spans, [240, 240, 60])

    def test_only_a_webdriveragent_this_checkout_started_counts_as_ours(self):
        from unittest import mock
        from harness import wda as W
        from tkstress.lane import wda_started_here
        with tempfile.TemporaryDirectory() as d:
            pid_file = Path(d) / "wda.pid"
            with mock.patch.object(W, "PID_FILE", pid_file):
                self.assertFalse(wda_started_here())
                pid_file.write_text(str(os.getpid()))
                self.assertTrue(wda_started_here())
                pid_file.write_text("999999999")
                self.assertFalse(wda_started_here())

    def test_the_lane_has_its_own_simulator(self):
        from harness.config import CONFIG
        self.assertEqual(CONFIG.get("TH_SIM_NAME"), os.environ.get("TH_SIM_NAME", "TandemKitStress"))
        self.assertNotEqual(CONFIG.get("TH_SIM_NAME"), "TrioHarness")


@unittest.skipUnless(os.environ.get("FAKETANDEM_BIN") and os.environ.get("FAKETANDEM_JAR"),
                     "set FAKETANDEM_BIN and FAKETANDEM_JAR to run against a real faketandem")
class LiveFaketandem(unittest.TestCase):
    def test_stage_a_paired_pump_and_arm_faults(self):
        from tkstress import recovery
        from tkstress.faketandem import CONNECTABLE_PAIRING_STATE, Faketandem
        with tempfile.TemporaryDirectory() as d, Faketandem.prepare().start(Path(d, "faketandem.log")) as pump:
            key = os.urandom(32)
            hello = pump.stage_paired_pump(key)
            self.assertEqual(hello["pairing_state"], CONNECTABLE_PAIRING_STATE)
            snap = pump.client.snapshot()
            self.assertTrue(snap["auth"]["long_term_key_present"])
            pump.client.arm_fault({"kind": "drop_response", "message": "CurrentBasalStatusRequest"})
            self.assertEqual(len(pump.client.faults()), 1)
            pump.client.clear_faults()
            self.assertEqual(pump.client.faults(), [])
            ok, st = recovery.wait_for_session(pump.client, timeout=1, poll=0.2)
            self.assertFalse(ok)                       # nothing connected to it
            self.assertFalse(st["connected"])


if __name__ == "__main__":
    unittest.main()
