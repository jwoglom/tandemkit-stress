"""The app under stress on the simulator: launched with the environment the virtual pump and the sanitizers need,
killed and relaunched at will, its stderr kept per launch.

Every launch goes through `SimApp.launch`. The TandemKit endpoint reaches the app only as an environment variable
(`SIMCTL_CHILD_TANDEMKIT_VIRTUAL_PUMP`, which simctl passes on with the prefix removed), so an app started any other
way (a tap on its icon, WebDriverAgent activating it) runs without one and sees Bluetooth as unsupported.

Simulator apps are host processes, so their pid can be signalled and sampled directly.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import signal
import subprocess
from pathlib import Path

from harness import xcode
from harness.util import HarnessError, log, run

TANDEMKIT_SUBSYSTEM = "com.jwoglom.TandemKit"


def _xcrun(args: list[str], *, env: dict | None = None, **kw):
    return run(["xcrun", *args], env={**xcode.developer_env(), **(env or {})}, **kw)


def sanitizer_runtime_dir() -> Path | None:
    """The toolchain's clang runtime directory (libclang_rt.tsan_iossim_dynamic.dylib and friends). Xcode embeds the
    runtime in the app when a sanitizer is on; this is the fallback for when it did not, since a simulator app can
    load it from the host."""
    try:
        clang = Path(_xcrun(["--find", "clang"], quiet=True).stdout.strip())
    except HarnessError:
        return None
    hits = sorted(clang.parent.parent.glob("lib/clang/*/lib/darwin"))
    return hits[-1] if hits else None


def main_thread_checker_dylib() -> Path | None:
    dev = Path(xcode.developer_env()["DEVELOPER_DIR"])
    for cand in (dev / "Platforms/iPhoneSimulator.platform/Developer/usr/lib/libMainThreadChecker.dylib",
                 dev / "usr/lib/libMainThreadChecker.dylib"):
        if cand.exists():
            return cand
    return None


class SimApp:
    def __init__(self, udid: str, bundle_id: str, out_dir: Path, env: dict[str, str]):
        self.udid = udid
        self.bundle_id = bundle_id
        self.out_dir = out_dir
        self.env = env
        self.pid: int | None = None
        self.launches: list[dict] = []
        self.ended_by_us: set[int] = set()          # pids the harness itself killed, terminated or relaunched
        out_dir.mkdir(parents=True, exist_ok=True)

    def launch(self) -> int:
        n = len(self.launches) + 1
        out, err = self.out_dir / f"launch-{n:02d}.stdout", self.out_dir / f"launch-{n:02d}.stderr"
        child = {f"SIMCTL_CHILD_{k}": v for k, v in self.env.items()}
        if self.pid:
            self.ended_by_us.add(self.pid)
        cp = _xcrun(["simctl", "launch", "--terminate-running-process", f"--stdout={out}", f"--stderr={err}",
                     self.udid, self.bundle_id], env=child, timeout=120)
        m = re.search(r":\s*(\d+)\s*$", cp.stdout.strip())
        if not m:
            raise HarnessError(f"simctl launch printed no pid: {cp.stdout.strip()!r}")
        self.pid = int(m.group(1))
        self.launches.append({"n": n, "pid": self.pid, "t": dt.datetime.now().isoformat(timespec="seconds"),
                              "stdout": out.name, "stderr": err.name})
        log(f"launched {self.bundle_id} (pid {self.pid}, launch {n})")
        return self.pid

    def alive(self) -> bool:
        if not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # a zombie still answers kill(0); ps knows better
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(self.pid)], capture_output=True, text=True).stdout.strip()
        return bool(st) and not st.startswith("Z")

    def kill(self, sig: int = signal.SIGKILL) -> None:
        """What iOS does to a suspended app it reclaims: SIGKILL, no chance to save anything."""
        if self.pid and self.alive():
            self.ended_by_us.add(self.pid)
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        if self.pid:
            self.ended_by_us.add(self.pid)
        _xcrun(["simctl", "terminate", self.udid, self.bundle_id], check=False, quiet=True, timeout=60)

    def foreground(self) -> None:
        """Back to the front; a process that died meanwhile is relaunched with the environment."""
        if not self.alive():
            self.launch()
            return
        _xcrun(["simctl", "launch", self.udid, self.bundle_id], check=False, quiet=True, timeout=60)

    def died_unexpectedly(self) -> bool:
        return bool(self.pid) and self.pid not in self.ended_by_us and not self.alive()

    def sample(self, dest: Path, seconds: int = 3) -> Path | None:
        """macOS `sample` of the app process: every thread's stack, for a hang report."""
        if not self.alive():
            return None
        cp = subprocess.run(["sample", str(self.pid), str(seconds), "-file", str(dest)], capture_output=True,
                            text=True, timeout=seconds + 60)
        return dest if cp.returncode == 0 and dest.exists() else None

    def metrics(self) -> dict:
        """Resident memory (MB) and thread count of the app process, for spotting slow growth over a long run."""
        if not self.alive():
            return {}
        out: dict = {}
        rss = subprocess.run(["ps", "-o", "rss=", "-p", str(self.pid)], capture_output=True, text=True).stdout.strip()
        if rss.isdigit():
            out["rss_mb"] = int(rss) // 1024
        # macOS: `ps -M` prints one line per thread under a header
        lines = subprocess.run(["ps", "-M", "-p", str(self.pid)], capture_output=True, text=True).stdout.splitlines()
        if len(lines) > 1:
            out["threads"] = len(lines) - 1
        return out

    def stderr_files(self) -> list[Path]:
        return sorted(self.out_dir.glob("launch-*.stderr"))


class LogStream:
    """`log stream` inside the simulator, filtered to TandemKit: its debug messages go to OSLog only."""

    def __init__(self, udid: str, dest: Path, subsystem: str = TANDEMKIT_SUBSYSTEM):
        self.udid, self.dest, self.subsystem = udid, dest, subsystem
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        cmd = ["xcrun", "simctl", "spawn", self.udid, "log", "stream", "--level", "debug", "--style", "compact",
               "--predicate", f'subsystem == "{self.subsystem}"']
        self._fh = open(self.dest, "ab")
        self.proc = subprocess.Popen(cmd, stdout=self._fh, stderr=subprocess.STDOUT,
                                     env={**os.environ, **xcode.developer_env()}, start_new_session=True)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        if getattr(self, "_fh", None):
            self._fh.close()
