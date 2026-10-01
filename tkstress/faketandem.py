"""faketandem, the Tandem pump emulator (https://github.com/jwoglom/faketandem), for the stress lane.

TandemKit built with TANDEMKIT_VIRTUAL_PUMP talks to faketandem over a TCP socket instead of CoreBluetooth
(TandemKit docs/integration-harness.md). This module builds the emulator, runs it on two free loopback ports
(the virtual GATT link and the HTTP/websocket API) and drives that API: pump state, pump-side actions, faults,
the request log and the pairing commands that exist only on the websocket.

    fk = Faketandem.prepare(ref="main")    # build (or FAKETANDEM_BIN) + the pumpX2 cliparser jar
    with fk.start(log_path) as pump:
        pump.client.arm_fault({"kind": "drop_response", "message": "CurrentBasalStatusRequest"})
        pump.control.set_long_term_key(os.urandom(32))
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from harness.config import CONFIG
from harness.util import HarnessError, log, run

from . import setting

DEFAULT_REPO = "jwoglom/faketandem"
DEFAULT_PAIRING_CODE = "123456"
# The state the integration tests reset to: discoverable for a paired central. The emulator starts NotDiscoverable,
# which the virtual CBCentralManager treats as a pump that is not advertising, so a restored driver would never connect.
CONNECTABLE_PAIRING_STATE = "PairStep2"


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def read_hello(host: str, port: int, timeout: float = 5.0) -> dict:
    """The `hello` frame the virtual peripheral sends on every connection (its peripheral_id, name, services).
    Closing without `attach` is what TandemKit's virtual scan does too, so it does not disturb the emulator."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        line = s.makefile("r", encoding="utf-8").readline()
    try:
        hello = json.loads(line)
    except ValueError as e:
        raise HarnessError(f"faketandem {host}:{port} sent no hello: {line[:200]!r}") from e
    if hello.get("type") != "hello" or not hello.get("peripheral_id"):
        raise HarnessError(f"faketandem {host}:{port}: unexpected first frame {line[:200]!r}")
    return hello


# ---------------- source, binary, jar -----------------

def _git(args: list[str], cwd: Path, **kw):
    """Anonymously first (faketandem is public, and a CI job's App token expires after an hour, after which it fails
    even on public repositories), then with the harness's credentials for a private fork."""
    from harness.build import git_env
    check = kw.pop("check", True)
    cp = run(["git", *args], cwd=cwd, env={"GIT_TERMINAL_PROMPT": "0"}, check=False, **kw)
    if cp.returncode == 0:
        return cp
    return run(["git", *args], cwd=cwd, env=git_env(), check=check, **kw)


def source_dir(ref: str | None = None) -> tuple[Path, str]:
    """A faketandem checkout at `ref` (FAKETANDEM_DIR as-is, else build/faketandem-src fetched from
    FAKETANDEM_REPO). Returns (dir, short sha)."""
    if setting("FAKETANDEM_DIR"):
        src = Path(setting("FAKETANDEM_DIR")).expanduser().resolve()
        if not (src / "go.mod").exists():
            raise HarnessError(f"FAKETANDEM_DIR={src} is not a faketandem checkout")
    else:
        src = CONFIG.build_dir / "faketandem-src"
        url = f"https://github.com/{setting('FAKETANDEM_REPO') or DEFAULT_REPO}.git"
        ref = ref or setting("FAKETANDEM_REF") or "main"
        if not (src / ".git").exists():
            src.mkdir(parents=True, exist_ok=True)
            _git(["init", "--quiet"], src)
        _git(["fetch", "--quiet", "--depth", "1", url, ref], src, timeout=900)
        _git(["checkout", "--quiet", "--force", "--detach", "FETCH_HEAD"], src)
    sha = _git(["rev-parse", "--short=10", "HEAD"], src, quiet=True, check=False).stdout.strip()
    return src, sha


def build_binary(src: Path) -> Path:
    out = CONFIG.build_dir / "faketandem-bin" / "faketandem"
    out.parent.mkdir(parents=True, exist_ok=True)
    log(f"building faketandem from {src}")
    run(["go", "build", "-o", str(out), "."], cwd=src, timeout=900)
    return out


def ensure_jar(src: Path | None) -> Path:
    """pumpX2's cliparser jar: FAKETANDEM_JAR, else the checkout's third_party/ (`make jar` downloads it)."""
    if setting("FAKETANDEM_JAR"):
        jar = Path(setting("FAKETANDEM_JAR")).expanduser()
        if not jar.exists():
            raise HarnessError(f"FAKETANDEM_JAR={jar} does not exist")
        return jar
    if src is None:
        raise HarnessError("set FAKETANDEM_JAR (a pumpx2-cliparser jar) when FAKETANDEM_BIN is given")
    jars = sorted((src / "third_party").glob("pumpx2-cliparser-*.jar"))
    if not jars:
        run(["make", "jar"], cwd=src, timeout=600)
        jars = sorted((src / "third_party").glob("pumpx2-cliparser-*.jar"))
    if not jars:
        raise HarnessError(f"no pumpx2-cliparser jar in {src}/third_party after `make jar`")
    return jars[-1]


# ---------------- HTTP API -----------------

class Client:
    """faketandem's HTTP API (pkg/api + pkg/harness). Every call returns the decoded JSON."""

    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            r = self.session.request(method, self.base + path, json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise HarnessError(f"faketandem {method} {path}: {e}") from e
        if r.status_code >= 400:
            raise HarnessError(f"faketandem {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self._call("POST", path, body if body is not None else {})

    def put(self, path: str, body: Any) -> Any:
        return self._call("PUT", path, body)

    def delete(self, path: str) -> Any:
        return self._call("DELETE", path)

    # pairing / state
    def pairing_state(self) -> str:
        return self.get("/api/bluetooth/pairingstate")["pairingState"]

    def set_pairing_state(self, state: str) -> None:
        self.post("/api/bluetooth/pairingstate", {"pairingState": state})

    def snapshot(self) -> dict:
        return self.get("/api/state")

    def set_state(self, **fields) -> dict:
        return self.put("/api/state", fields)

    # pump-side actions (what a person does on the pump, or what the pump does on its own)
    def start_bolus(self, units: float, source: str = "pump", **extra) -> dict:
        return self.post("/api/state/bolus/start", {"units": units, "source": source, **extra})

    def abort_bolus(self) -> dict:
        return self.post("/api/state/bolus/abort")

    def start_temp_basal(self, percent: int, duration_minutes: int) -> dict:
        return self.post("/api/state/tempbasal/start", {"percent": percent, "duration_minutes": duration_minutes})

    def stop_temp_basal(self) -> dict:
        return self.post("/api/state/tempbasal/stop")

    def suspend(self, reason: str = "user") -> dict:
        return self.post("/api/state/suspend", {"reason": reason})

    def resume(self, clear_alarms: bool = True) -> dict:
        return self.post("/api/state/resume", {"clear_alarms": clear_alarms})

    def qualifying_event(self, bitmask: int) -> Any:
        return self.post("/api/state/qualifyingevent", {"bitmask": int(bitmask)})

    # clock, transport
    def clock(self) -> dict:
        return self.get("/api/clock")

    def set_clock(self, **fields) -> dict:
        return self.put("/api/clock", fields)

    def set_att_mtu(self, mtu: int) -> dict:
        return self.put("/api/transport", {"att_mtu": int(mtu)})

    # faults
    def faults(self) -> list[dict]:
        return self.get("/api/faults").get("faults", [])

    def arm_fault(self, spec: dict) -> dict:
        return self.post("/api/faults", spec)

    def clear_faults(self) -> None:
        self.delete("/api/faults")

    # request log and history
    def request_log(self, since: int | None = None) -> dict:
        return self.get("/api/log" + (f"?since={int(since)}" if since is not None else ""))

    def history_page(self, since: int | None = None, limit: int | None = None) -> dict:
        q = "&".join(f"{k}={v}" for k, v in (("since", since), ("limit", limit)) if v is not None)
        return self.get("/api/history" + (f"?{q}" if q else ""))


# ---------------- websocket control -----------------

class ControlSocket:
    """The websocket commands that have no HTTP route: pairing code, long-term key, pump-side disconnect."""

    def __init__(self, ws_url: str, timeout: float = 10.0):
        import websocket  # websocket-client
        self._ws = websocket.create_connection(ws_url, timeout=timeout)
        self.timeout = timeout

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not an error worth surfacing
            pass

    def send(self, command: str, **params) -> None:
        self._ws.send(json.dumps({"command": command, **params}))

    def _await(self, msg_type: str) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            msg = json.loads(self._ws.recv())
            if msg.get("type") == msg_type:
                return msg
        raise HarnessError(f"faketandem websocket: no {msg_type} message within {self.timeout}s")

    def set_pairing_code(self, code: str) -> dict:
        self.send("setPairingCode", pairingCode=code)
        return self._await("pairing_state")

    def set_long_term_key(self, key: bytes) -> dict:
        self.send("setLongTermKey", longTermKey=key.hex())
        status = self._await("pairing_state")
        if status.get("long_term_key") != key.hex():
            raise HarnessError("faketandem did not take the seeded long-term key")
        return status

    def disconnect_pump(self) -> None:
        self.send("disconnectPump")


# ---------------- the process -----------------

@dataclass
class Faketandem:
    binary: Path
    jar: Path
    info: dict = field(default_factory=dict)        # {"sha": ..., "source": ...} for run.json
    host: str = "127.0.0.1"
    extra_args: list[str] = field(default_factory=list)
    proc: subprocess.Popen | None = None
    gatt_port: int = 0
    api_port: int = 0
    _client: Client | None = None
    _control: ControlSocket | None = None

    @classmethod
    def prepare(cls, ref: str | None = None) -> "Faketandem":
        """FAKETANDEM_BIN (+ FAKETANDEM_JAR) when given, else build from source at `ref`."""
        if setting("FAKETANDEM_BIN"):
            binary = Path(setting("FAKETANDEM_BIN")).expanduser()
            src = Path(setting("FAKETANDEM_DIR")).expanduser() if setting("FAKETANDEM_DIR") else None
            return cls(binary, ensure_jar(src), {"source": str(binary)})
        src, sha = source_dir(ref)
        return cls(build_binary(src), ensure_jar(src), {"source": str(src), "sha": sha,
                                                         "ref": ref or setting("FAKETANDEM_REF") or "main"})

    @property
    def gatt_addr(self) -> str:
        return f"{self.host}:{self.gatt_port}"

    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.api_port}"

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self.api_url)
        return self._client

    @property
    def control(self) -> ControlSocket:
        if self._control is None:
            self._control = ControlSocket(f"ws://{self.host}:{self.api_port}/ws")
        return self._control

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, log_path: Path, startup_timeout: float = 90.0) -> "Faketandem":
        self.gatt_port, self.api_port = free_port(self.host), free_port(self.host)
        cmd = [str(self.binary), "-transport", "virtual", "-virtual-addr", self.gatt_addr,
               "-api-addr", f"{self.host}:{self.api_port}", "-pumpx2-jar-path", str(self.jar), *self.extra_args]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log(f"starting faketandem: GATT {self.gatt_addr}, API {self.api_url} (log: {log_path})")
        self._log_fh = open(log_path, "ab")
        # Its own process group, so stop() also takes the cliparser JVM it spawns.
        self.proc = subprocess.Popen(cmd, stdout=self._log_fh, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            if not self.alive():
                raise HarnessError(f"faketandem exited during startup (code {self.proc.returncode}); see {log_path}")
            try:
                self.client.pairing_state()
                return self
            except HarnessError:
                time.sleep(0.5)
        self.stop()
        raise HarnessError(f"faketandem API did not come up on {self.api_url} within {startup_timeout:.0f}s")

    def stop(self) -> None:
        if self._control:
            self._control.close()
            self._control = None
        if self.proc and self.proc.poll() is None:
            for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                try:
                    os.killpg(self.proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    self.proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if getattr(self, "_log_fh", None):
            self._log_fh.close()
            self._log_fh = None

    def __enter__(self) -> "Faketandem":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stage_paired_pump(self, key: bytes, pairing_code: str = DEFAULT_PAIRING_CODE) -> dict:
        """The pump side of a completed pairing: connectable, this pairing code, this long-term key. A driver whose
        state holds the same key authenticates through JPAKE rounds 3/4 alone (TandemKit's seededLongTermKey).
        Returns the `hello` frame, whose peripheral_id the driver's state needs."""
        self.client.set_pairing_state(CONNECTABLE_PAIRING_STATE)
        self.control.set_pairing_code(pairing_code)
        self.control.set_long_term_key(key)
        return read_hello(self.host, self.gatt_port)
