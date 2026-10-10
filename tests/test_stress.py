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
import time
import unittest
from dataclasses import asdict
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

    def test_swift_access_race_signs_with_both_access_stacks(self):
        [f] = D.scan_stderr((FIX / "tsan-darwin-swift-access.txt").read_text())
        self.assertEqual(f.component, "Trio")
        self.assertIn("rawPumpManager.setter", f.title)
        self.assertTrue(any("pumpManager.didset" in s for s in f.signature))
        self.assertTrue(any("pumpManagerDidUpdateState" in s for s in f.signature))
        self.assertFalse(any("malloc" in s or "NSRecursiveLock" in s for s in f.signature))

    def test_a_race_signs_the_same_whichever_access_tsan_prints_first(self):
        text = (FIX / "tsan-darwin-swift-access.txt").read_text()
        i, j, k = text.index("  Modifying access"), text.index("  Previous modifying"), text.index("  Location is")
        swapped = text[:i] + text[j:k] + text[i:j] + text[k:]
        [a], [b] = D.scan_stderr(text), D.scan_stderr(swapped)
        self.assertEqual(a.fp, b.fp)
        self.assertEqual(a.title, b.title)

    def test_read_only_swift_access_counts_as_an_access_stack(self):
        text = (FIX / "tsan-darwin-swift-access.txt").read_text().replace(
            "Previous modifying access", "Previous read-only access")
        [f] = D.scan_stderr(text)
        self.assertTrue(any("pumpManagerDidUpdateState" in s for s in f.signature))

    def test_main_thread_checker(self):
        mtc = [f for f in D.scan_stderr((FIX / "tsan-darwin.txt").read_text()) if f.kind == "main-thread"]
        self.assertEqual(len(mtc), 1)
        self.assertIn("setNeedsLayout", mtc[0].title)
        self.assertEqual(mtc[0].component, "TandemKit")


    def test_a_swift_access_race_between_two_writes_signs_with_both_stacks(self):
        f = D.scan_stderr((FIX / "tsan-swift-access.txt").read_text())[0]
        self.assertEqual(f.component, "Trio")
        self.assertIn("rawPumpManager.setter", f.title)
        self.assertTrue(any("pumpManager.didset" in s for s in f.signature))
        self.assertTrue(any("pumpManagerDidUpdateState" in s for s in f.signature))
        self.assertEqual(f.aliases, [])      # the old rule saw no frames: every such race had one fingerprint

    def test_a_race_is_one_finding_whichever_side_tsan_caught(self):
        text = (FIX / "tsan-swift-access.txt").read_text()
        blocks = text.split("\n\n")

        def split(block):
            lines = block.split("\n")
            n = next(i for i, line in enumerate(lines) if line.startswith("    #"))
            return lines[:n], lines[n:]
        (h0, f0), (h1, f1) = split(blocks[0]), split(blocks[1])
        blocks[0], blocks[1] = "\n".join(h0 + f1), "\n".join(h1 + f0)
        a, b = (D.scan_stderr(t)[0] for t in (text, "\n\n".join(blocks)))
        self.assertEqual(a.fp, b.fp)

    def test_a_signature_that_changed_keeps_the_old_fingerprint_as_an_alias(self):
        text = (FIX / "tsan-swift-access.txt").read_text().replace(
            "Previous modifying access of Swift variable", "Previous read of size 8")
        f = D.scan_stderr(text)[0]
        old = D.fingerprint("tsan", "Swift access race",
                            "Trio`Trio.BaseDeviceDataManager.rawPumpManager.setter : "
                            "Swift.Optional<Swift.Dictionary<Swift.String, Any>>",
                            "Trio`Trio.BaseDeviceDataManager.pumpManagerDidUpdateState(LoopKit.PumpManager) -> ()",
                            "TandemKit`closure #1 @Sendable () -> () in "
                            "TandemKit.TandemPumpManager.notifyDelegateStateUpdated() -> ()")
        self.assertEqual(f.aliases, [old])
        self.assertNotEqual(f.fp, old)


class CrashAndHangReports(unittest.TestCase):
    def test_crash_details_carry_the_other_threads(self):
        crash = D.parse_ips(FIX / "trio-crash.ips")
        details = D.crash_finding(crash).details
        self.assertIn("crashed thread:", details)
        self.assertIn("other threads (top app frames):", details)

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

    def test_a_hang_inside_a_tsan_race_report_is_named_by_the_reported_access(self):
        f = D.sample_finding((FIX / "trio-hang-tsan-report.txt").read_text(), what="test")
        self.assertEqual((f.kind, f.component), ("hang", "Trio"))
        self.assertIn("deadlocked reporting a race in BaseDeviceDataManager.rawPumpManager.setter", f.title)
        self.assertEqual(f.signature, ["ThreadSanitizer race report", "Trio`BaseDeviceDataManager.rawPumpManager.setter"])
        self.assertIn("Thread_1003 was inside ThreadSanitizer's race report in all 2091 samples", f.details)
        self.assertIn("PersistedProperty.wrappedValue.setter", f.details)      # the main thread follows

    def test_a_tsan_report_hang_is_one_finding_whichever_thread_reports(self):
        a = D.sample_finding((FIX / "trio-hang-tsan-report.txt").read_text(), what="a")
        b = D.sample_finding((FIX / "trio-hang-tsan-report-main.txt").read_text(), what="b")
        self.assertEqual(a.fp, b.fp)

    def test_a_thread_passing_through_a_tsan_report_does_not_name_the_hang(self):
        text = (FIX / "trio-hang-tsan-report.txt").read_text().replace("2091 __tsan::ReportRace", "3 __tsan::ReportRace")
        f = D.sample_finding(text, what="test")
        self.assertIn("Main thread unresponsive (test) in PersistedProperty.wrappedValue.setter", f.title)

    def test_a_main_thread_waiting_in_its_run_loop_is_not_stuck(self):
        self.assertTrue(D.main_thread_waiting((FIX / "trio-idle-sample.txt").read_text()))
        self.assertFalse(D.main_thread_waiting((FIX / "trio-hang-sample.txt").read_text()))
        self.assertFalse(D.main_thread_waiting((FIX / "trio-hang-tsan-report.txt").read_text()))
        self.assertFalse(D.main_thread_waiting("no call graph"))

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


class LiveFiling(unittest.TestCase):
    """The publisher and file-issues against a stand-in `gh`."""

    def setUp(self):
        from unittest import mock
        self.calls = []

        def gh(args, **_):
            self.calls.append(args)
            out = "[]" if args[:2] == ["issue", "list"] else (
                "https://github.com/o/r/issues/7\n" if args[:2] == ["issue", "create"] else "")
            return mock.Mock(returncode=0, stdout=out, stderr="")
        self.patch = mock.patch.object(I, "_gh", gh)
        self.patch.start()
        self.dir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.dir.name)
        (self.run_dir / "run.json").write_text(json.dumps({"args": {"trio": "t", "tandemkit": "dev"}, "seed": 3}))

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def finding(self, occurrences=1, rounds=(2,)):
        f = D.Finding(kind="tsan", title="race", signature=["data race", "a", "b"], round=rounds[0],
                      occurrences=occurrences, rounds=list(rounds), details="report",
                      context={"When": "round 2", "TandemKit log (last 200 lines)": "log line"})
        return asdict(f)

    def creates_and_comments(self):
        return ([c for c in self.calls if c[:2] == ["issue", "create"]],
                [c for c in self.calls if c[:2] == ["issue", "comment"]])

    def test_the_publisher_files_once_and_closes_out_a_repeat_with_its_count(self):
        first = self.finding()
        publisher = I.Publisher("o/r", self.run_dir)
        publisher.submit(first, {"args": {}, "seed": 3})
        (self.run_dir / "findings.json").write_text(json.dumps([self.finding(occurrences=3, rounds=(2, 5, 9))]))
        with contextlib.redirect_stdout(io.StringIO()):
            publisher.close(timeout=30)
        creates, comments = self.creates_and_comments()
        self.assertEqual(len(creates), 1)
        self.assertIn("TandemKit log", creates[0][creates[0].index("--body") + 1])
        self.assertEqual(len(comments), 1)
        self.assertIn("3× in all, in rounds 2, 5, 9", comments[0][comments[0].index("--body") + 1])
        self.assertEqual(I.read_ledger(self.run_dir)[first["fp"]]["number"], 7)
        with contextlib.redirect_stdout(io.StringIO()):
            I.file_issues(self.run_dir, "o/r")
        self.assertEqual(self.creates_and_comments(), (creates, comments))

    def test_a_crash_report_is_attached_scrubbed_with_its_image_uuids_kept(self):
        from unittest import mock
        uploaded, releases = {}, set()

        def gh(args, **_):
            self.calls.append(args)
            if args[:2] == ["release", "view"]:
                return mock.Mock(returncode=0 if args[2] in releases else 1, stdout="", stderr="release not found")
            if args[:2] == ["release", "create"]:
                releases.add(args[2])
            if args[:2] == ["release", "upload"]:
                uploaded[Path(args[3]).name] = Path(args[3]).read_text()
            out = "[]" if args[:2] == ["issue", "list"] else (
                "https://github.com/o/r/issues/8\n" if args[:2] == ["issue", "create"] else "")
            return mock.Mock(returncode=0, stdout=out, stderr="")
        sim = f"{Path.home()}/Library/Developer/CoreSimulator/Devices/FF1FF95A-5DF6-43E3-8D02-208D4C55F851/data"
        ips = (json.dumps({"app_name": "Trio", "crashReporterKey": "machine-key", "incident_id": "1234"}) + "\n"
               + json.dumps({"procPath": f"{sim}/Trio.app/Trio", "threads": [],
                             "usedImages": [{"uuid": "4c4c4489-5555-3144-a1b2-c3d4e5f60718", "name": "TandemKit"}]}))
        (self.run_dir / "crashes").mkdir()
        (self.run_dir / "crashes" / "Trio-2026-10-01-155804.ips").write_text(ips)
        f = self.finding()
        f.update(kind="crash", evidence=["crashes/Trio-2026-10-01-155804.ips"])
        with mock.patch.object(I, "_gh", gh):
            entry = I.publish(f, "o/r", {"args": {}}, self.run_dir)
        (name, text), = uploaded.items()
        self.assertTrue(name.startswith(f["fp"]) and name.endswith("Trio-2026-10-01-155804.ips"))
        self.assertNotIn("machine-key", text)
        self.assertNotIn("1234", text)
        self.assertIn("~/Library/Developer/CoreSimulator/Devices/<device>/data", text)
        self.assertIn("4c4c4489-5555-3144-a1b2-c3d4e5f60718", text)
        body = next(c for c in self.calls if c[:2] == ["issue", "create"])
        body = body[body.index("--body") + 1]
        self.assertIn(f"/releases/download/{time.strftime('stress-attachments-%Y-%m')}/{name}", body)
        self.assertEqual(len(entry["attachments"]), 1)

    def test_a_finding_whose_signature_changed_comments_on_the_issue_it_had(self):
        from unittest import mock
        f = self.finding()
        f["aliases"] = ["0123456789ab"]

        def gh(args, **_):
            self.calls.append(args)
            hit = args[:2] == ["issue", "list"] and "0123456789ab in:body" in args
            issue = {"number": 5, "state": "OPEN", "title": "t", "url": "https://github.com/o/r/issues/5",
                     "body": I.MARKER.format(fp="0123456789ab")}
            return mock.Mock(returncode=0, stdout=json.dumps([issue] if hit else []), stderr="")
        with mock.patch.object(I, "_gh", gh):
            entry = I.publish(f, "o/r", {"args": {}}, self.run_dir)
        self.assertEqual((entry["action"], entry["number"]), ("commented", 5))
        self.assertEqual(self.creates_and_comments()[0], [])

    def test_a_body_with_huge_context_still_fits(self):
        f = self.finding()
        f["details"] = "x" * 100000
        f["context"] = {f"section {i}": "log line\n" * 20000 for i in range(5)}
        body = I.issue_body(f, {"args": {}}, self.run_dir)
        self.assertLessEqual(len(body), I.BODY_LIMIT)
        self.assertIn("section 4", body)


class Lane(unittest.TestCase):
    def test_a_report_still_being_written_waits_for_its_summary(self):
        from unittest import mock
        from tkstress import lane as L
        from tkstress.cli import build_parser
        report = (FIX / "tsan-darwin.txt").read_text().splitlines(keepends=True)
        cut = next(i for i, line in enumerate(report) if line.startswith("SUMMARY"))
        with tempfile.TemporaryDirectory() as d:
            stderr = Path(d) / "launch-01.stderr"
            stderr.write_text("".join(report[:cut]))
            lane = L.Lane(build_parser().parse_args(["run"]))
            lane.run = mock.Mock(path=Path(d))
            lane.findings = D.Findings(Path(d) / "findings.json")
            lane.app = mock.Mock(stderr_files=lambda: [stderr], alive=lambda: True, metrics=lambda: {}, launches=[{}],
                                 pid=1)
            lane.scan_stderr()
            self.assertEqual(len(lane.findings), 0)
            stderr.write_text("".join(report))
            lane.scan_stderr()
            self.assertEqual([f.kind for f in lane.findings], ["tsan"])
            lane.scan_stderr(final=True)
            found = list(lane.findings)
        # the fixture's Main Thread Checker report has no end marker, so only the final scan takes it
        self.assertEqual([(f.kind, f.occurrences) for f in found], [("tsan", 1), ("main-thread", 1)])
        self.assertIn("When", found[0].context)

    def test_request_log_lines_read_as_a_conversation(self):
        from tkstress.lane import format_requests
        text = format_requests([
            json.dumps({"seq": 4, "time": "2026-10-01T12:00:01.123456Z", "kind": "request",
                        "message": "InsulinStatusRequest", "tx_id": 9, "cargo": {"a": 1}}),
            json.dumps({"seq": 5, "time": "2026-10-01T12:00:01.200000Z", "kind": "response",
                        "message": "InsulinStatusResponse", "fragments": ["aa", "bb"], "fragments_sent": 1,
                        "fault": "disconnect"}),
            json.dumps({"kind": "harness-note", "note": "ring overflow"})])
        self.assertEqual(text.splitlines(), [
            '12:00:01.123 #4 request      InsulinStatusRequest tx=9 {"a":1}',
            "12:00:01.200 #5 response     InsulinStatusResponse 1/2 fragments sent fault=disconnect",
            "[ring overflow]"])

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

    def test_a_build_stopped_at_its_limit_is_incomplete(self):
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
        self.assertEqual(code, L.EXIT_INCOMPLETE)
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
        lane.app = mock.Mock(died_unexpectedly=lambda: False, metrics=lambda: {}, stderr_files=lambda: [])
        lane.dump_requests = lambda: None
        with mock.patch.object(L, "time", mock.Mock(time=lambda: clock[0])), \
                mock.patch("tkstress.chaos.Chaos", FakeChaos), mock.patch("tkstress.recovery.check", check):
            self.assertEqual(lane.left(), float("inf"))
            lane.deadline = clock[0] + 10 * 60
            lane.rounds_loop()
        self.assertEqual(spans, [240, 240, 60])

    def test_a_hung_app_is_sampled_relaunched_and_not_also_a_failed_recovery(self):
        from unittest import mock
        from tkstress import lane as L
        from tkstress.cli import build_parser
        clock = [1000.0]

        class FakeChaos:
            def __init__(self, *_, **__):
                self.counts, self.round, self.gaps = {}, 0, None

            def run_for(self, span, should_stop=None):
                clock[0] += span
                return []

            def quiesce(self):
                pass

        def find(*_, **__):
            clock[0] += 90

        def sample(dest):
            dest.write_text((FIX / "trio-hang-tsan-report.txt").read_text())
            return dest
        with tempfile.TemporaryDirectory() as d:
            lane = L.Lane(build_parser().parse_args(["run", "--minutes", "4"]))
            lane.run, lane.fk, lane.ui = mock.Mock(path=Path(d)), mock.Mock(), mock.Mock(find=find)
            lane.findings = D.Findings(Path(d) / "findings.json")
            lane.app = mock.Mock(died_unexpectedly=lambda: False, alive=lambda: True, metrics=lambda: {},
                                 stderr_files=lambda: [], sample=sample, launches=[{}], pid=1)
            lane.dump_requests = lambda: None
            failed = {"ok": False, "stage": "session", "seconds": 120}
            with mock.patch.object(L, "time", mock.Mock(time=lambda: clock[0])), \
                    mock.patch("tkstress.chaos.Chaos", FakeChaos), mock.patch("tkstress.recovery.check", lambda *_, **__: failed):
                lane.deadline = clock[0] + 4 * 60
                lane.rounds_loop()
            found = list(lane.findings)
        self.assertEqual([f.kind for f in found], ["hang"])
        self.assertEqual(found[0].evidence, ["hangs/round-01.txt"])
        lane.app.kill.assert_called_once()
        lane.app.launch.assert_called_once()

    def _one_round(self, d: str, *, query_seconds: float | None, quiesced: list[dict], sample: str):
        """A round whose recovery check fails. `query_seconds`: how long WebDriverAgent's query takes, None for no
        WebDriverAgent; `sample` is the fixture standing in for the app's `sample`."""
        from unittest import mock
        from tkstress import lane as L
        from tkstress.cli import build_parser
        clock = [1000.0]

        class FakeChaos:
            def __init__(self, *_, **__):
                self.counts, self.round, self.gaps = {}, 0, None

            def run_for(self, span, should_stop=None):
                clock[0] += span
                return []

            def quiesce(self):
                return quiesced

        def find(*_, **__):
            clock[0] += query_seconds

        def take_sample(dest):
            dest.write_text((FIX / sample).read_text())
            return dest
        lane = L.Lane(build_parser().parse_args(["run", "--minutes", "4"]))
        lane.run, lane.fk = mock.Mock(path=Path(d)), mock.Mock()
        lane.ui = None if query_seconds is None else mock.Mock(find=find)
        lane.findings = D.Findings(Path(d) / "findings.json")
        lane.app = mock.Mock(died_unexpectedly=lambda: False, alive=lambda: True, metrics=lambda: {},
                             stderr_files=lambda: [], sample=take_sample, launches=[{}], pid=1)
        lane.dump_requests = lambda: None
        failed = {"ok": False, "stage": "session", "seconds": 120}
        with mock.patch.object(L, "time", mock.Mock(time=lambda: clock[0])), \
                mock.patch("tkstress.chaos.Chaos", FakeChaos), mock.patch("tkstress.recovery.check", lambda *_, **__: failed):
            lane.deadline = clock[0] + 4 * 60
            lane.rounds_loop()
        return lane, list(lane.findings)

    def test_without_webdriveragent_a_foreground_that_timed_out_finds_the_hang(self):
        timed_out = [{"action": "quiesce_app", "category": "quiesce", "result": "error",
                      "detail": "HarnessError: timed out after 60s: xcrun simctl launch <uuid> org.example.trio"}]
        with tempfile.TemporaryDirectory() as d:
            lane, found = self._one_round(d, query_seconds=None, quiesced=timed_out, sample="trio-hang-tsan-report.txt")
        self.assertEqual([f.kind for f in found], ["hang"])
        self.assertIn("simctl launch timed out", found[0].title)
        lane.app.kill.assert_called_once()
        lane.app.launch.assert_called_once()

    def test_a_slow_query_from_an_app_waiting_for_events_is_not_a_hang(self):
        with tempfile.TemporaryDirectory() as d:
            lane, found = self._one_round(d, query_seconds=90, quiesced=[], sample="trio-idle-sample.txt")
        self.assertEqual([f.kind for f in found], ["no-recovery"])
        lane.app.kill.assert_not_called()
        lane.app.launch.assert_not_called()

    def test_until_cuts_the_stress_short_only_when_it_is_sooner(self):
        from tkstress.lane import stress_window
        self.assertEqual(stress_window(1000, 10, None), (1600, False))
        self.assertEqual(stress_window(1000, 10, 5000), (1600, False))
        self.assertEqual(stress_window(1000, 240, 9000), (9000, True))

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
