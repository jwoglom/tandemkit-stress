"""What counts as a failure in a stress run, and how two sightings of the same one are recognised.

Sources:
  * sanitizer reports (ThreadSanitizer, AddressSanitizer) the app writes to stderr: `simctl launch --stderr`
  * Main Thread Checker reports, also on stderr, when its dylib is injected
  * crash reports (.ips) macOS writes to ~/Library/Logs/DiagnosticReports for simulator processes
  * the recovery check (lane.py), which decides whether the driver came back after the faults stopped

Every finding carries a fingerprint built from function names only (no addresses, offsets or line numbers), so the
same race seen on different commits, or with the code shifted a few lines, files into the same issue.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Runtime and system modules: never the interesting frame of a report.
SYSTEM_MODULES = re.compile(
    r"^(libswift\w*|libdispatch|libsystem_\w+|libclang_rt\.\w+|libobjc\.A|libc\+\+\w*|libdyld|dyld\w*|Foundation\w*|"
    r"CoreFoundation|UIKitCore|UIKit|SwiftUI\w*|AttributeGraph|QuartzCore|CoreData|Combine|XCTest\w*|"
    r"libMainThreadChecker|libxpc|GraphicsServices|FrontBoardServices|BaseBoard|RunningBoardServices|"
    r"CoreBluetooth|Network|CFNetwork|libc\.so\.\d|libpthread\w*|ld-linux\S*)$")
DRIVER = re.compile(r"TandemKit|TandemCore")


def norm_module(raw: str) -> str:
    """`/path/libdispatch.dylib` / `libFoundation.so.6` / `Trio.debug.dylib` -> libdispatch / libFoundation / Trio."""
    name = raw.strip().rsplit("/", 1)[-1]
    name = re.sub(r"\.so(\.\d+)*$|\.dylib$|\.framework$", "", name)
    return re.sub(r"\.debug$", "", name)


@dataclass
class Frame:
    symbol: str
    module: str = ""
    location: str = ""                 # file:line[:col] when the report has it

    def __post_init__(self):
        self.module = norm_module(self.module)

    @property
    def interesting(self) -> bool:
        return bool(self.symbol) and not SYSTEM_MODULES.match(self.module) and not self.symbol.startswith(
            ("__tsan", "__asan", "_dispatch", "_pthread", "swift_", "__swift", "objc_", "<null>"))

    def short(self) -> str:
        return self.symbol if not self.module else f"{self.module}`{self.symbol}"


def component_of(frames: list[Frame]) -> str:
    """Which code a report implicates first: the driver, the host app, LoopKit, or something else."""
    for f in frames:
        if not f.interesting:
            continue
        text = f"{f.module} {f.symbol} {f.location}"
        if DRIVER.search(text):
            return "TandemKit"
        if re.search(r"\bLoopKit", text):
            return "LoopKit"
        if f.module == "Trio" or "/Trio/Sources/" in f.location:
            return "Trio"
    return "other"


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]


_DEMANGLED: dict[str, str] = {}


def demangle(symbol: str) -> str:
    """Readable Swift name for titles (fingerprints keep the raw symbol). Darwin TSan already demangles; Linux
    prints mangled names. Uses swift-demangle when it is around, else returns the symbol as given."""
    if not symbol.startswith(("$s", "_$s", "$S", "_T")):
        return symbol
    if symbol not in _DEMANGLED:
        import shutil
        import subprocess
        cmd = (["xcrun", "swift-demangle", "--simplified"] if shutil.which("xcrun") else
               ["swift", "demangle", "--simplified"] if shutil.which("swift") else None)
        out = symbol
        if cmd:
            try:
                printed = subprocess.run([*cmd, symbol], capture_output=True, text=True, timeout=10).stdout.strip()
                out = printed.rsplit(" ---> ", 1)[-1] or symbol      # "mangled ---> demangled"
            except (OSError, subprocess.SubprocessError):
                pass
        _DEMANGLED[symbol] = out
    return _DEMANGLED[symbol]


@dataclass
class Finding:
    kind: str                          # crash | tsan | asan | main-thread | no-recovery | hang | app-died | build | setup
    title: str
    signature: list[str]               # the frames (or facts) the fingerprint is built from, for the issue body
    component: str = "other"
    details: str = ""                  # report excerpt
    evidence: list[str] = field(default_factory=list)   # run-relative paths
    round: int | None = None
    time: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    occurrences: int = 1
    fp: str = ""

    def __post_init__(self):
        self.fp = self.fp or fingerprint(self.kind, *self.signature)


class Findings:
    """A run's findings, deduplicated by fingerprint (a repeat bumps `occurrences`), saved as findings.json."""

    def __init__(self, path: Path):
        self.path = path
        self.items: dict[str, Finding] = {}
        if path.exists():
            self.items = {f.fp: f for f in (Finding(**d) for d in json.loads(path.read_text() or "[]"))}

    def add(self, f: Finding) -> bool:
        """True when the fingerprint is new to this run."""
        new = f.fp not in self.items
        if new:
            self.items[f.fp] = f
        else:
            self.items[f.fp].occurrences += 1
        self.save()
        return new

    def save(self) -> None:
        self.path.write_text(json.dumps([asdict(f) for f in self.items.values()], indent=2))

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items.values())

    @classmethod
    def load(cls, path: Path) -> list[Finding]:
        return [Finding(**d) for d in json.loads(path.read_text())] if path.exists() else []


# ---------------- sanitizers -----------------

# "    #3 <symbol> <file:line[:col]> (<module>+0x...) (BuildId: ...)"   Linux / Darwin TSan and ASan
# "    #3 0x10234 in <symbol> <file:line> (<module>:arm64+0x...)"     ASan
SAN_FRAME = re.compile(r"^\s*#\d+\s+(?:0x[0-9a-f]+\s+in\s+)?(?P<sym>.+?)"
                       r"(?:\s+(?P<loc>\S+:\d+(?::\d+)?|\S*/\S+\.(?:swift|mm?|c|cc|cpp|h)|\S+\.(?:swift|mm?|c|cc|cpp|h)"
                       r"|<null>|/?<compiler-generated>))?"
                       r"\s+\((?P<mod>[^()+:]+)(?::\w+)?\+0x[0-9a-f]+\)(?:\s+\(BuildId: [0-9a-f]+\))?\s*$")
SAN_START = re.compile(r"^(?:==\d+==)?\s*(?:WARNING|ERROR): (ThreadSanitizer|AddressSanitizer): (.+?)(?: \(pid=\d+\))?\s*$")
# Stacks whose frames identify a race: the access and the conflicting one. Allocation sites, thread creation and
# mutex acquisition stacks say where, not what, and change with unrelated refactors.
ACCESS_STACK = re.compile(r"^\s{2}(?:Previous |Atomic )*(?:[Aa]tomic )?(?:read|write|Read|Write)\b|"
                          r"^(?:READ|WRITE) of size|^freed by thread|^previously allocated by")


def parse_sanitizer_reports(text: str) -> list[dict]:
    """Every sanitizer report in `text`: {"tool", "kind", "summary", "stacks": [[Frame]], "text"}."""
    reports: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = SAN_START.match(line)
        if m:
            cur = {"tool": m.group(1), "kind": m.group(2).strip(), "summary": "", "stacks": [], "lines": [line],
                   "_take": True}
            reports.append(cur)
            continue
        if cur is None:
            continue
        cur["lines"].append(line)
        if line.startswith("SUMMARY: "):
            cur["summary"] = line[len("SUMMARY: "):].strip()
            cur = None
            continue
        fm = SAN_FRAME.match(line)
        if fm:
            if cur["_take"]:
                if not cur["stacks"] or cur.get("_new_stack"):
                    cur["stacks"].append([])
                    cur["_new_stack"] = False
                loc = fm.group("loc") or ""
                cur["stacks"][-1].append(Frame(fm.group("sym").strip(), fm.group("mod"),
                                               "" if loc.startswith(("<", "/<")) else loc))
            continue
        stripped = line.strip()
        if stripped:
            # a section header starts the next stack; only access stacks (or the first stack of a non-race report) count
            cur["_take"] = bool(ACCESS_STACK.match(line)) or (cur["tool"] == "AddressSanitizer" and not cur["stacks"])
            cur["_new_stack"] = True
    for r in reports:
        r["text"] = "\n".join(r.pop("lines"))
        r.pop("_take", None)
        r.pop("_new_stack", None)
    return reports


def sanitizer_finding(report: dict, *, top: int = 3) -> Finding:
    kind = "tsan" if report["tool"] == "ThreadSanitizer" else "asan"
    sig: list[str] = [report["kind"]]
    frames_all: list[Frame] = []
    for stack in report["stacks"][:2]:
        keep = [f for f in stack if f.interesting][:top] or stack[:top]
        sig += [f.short() for f in keep]
        frames_all += stack
    lead = next((f for f in frames_all if f.interesting), frames_all[0] if frames_all else None)
    title = f"{report['tool']}: {report['kind']}" + (f" in {demangle(lead.symbol)}" if lead else "")
    return Finding(kind=kind, title=title[:200], signature=sig, component=component_of(frames_all),
                   details=report["text"][:6000])


# ---------------- Main Thread Checker -----------------

MTC_START = re.compile(r"^(?:.*?\s)?Main Thread Checker: (.+)$")
MTC_FRAME = re.compile(r"^\s*\d+\s+(\S+)\s+0x[0-9a-f]+\s+(.+?)(?:\s+\+\s+\d+)?\s*$")


def parse_main_thread_checker(text: str) -> list[Finding]:
    out: list[Finding] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = MTC_START.match(line)
        if not m:
            continue
        frames: list[Frame] = []
        for nxt in lines[i + 1:i + 60]:
            if MTC_START.match(nxt):
                break
            fm = MTC_FRAME.match(nxt)
            if fm:
                frames.append(Frame(fm.group(2), fm.group(1)))
        keep = [f for f in frames if f.interesting][:3]
        api = re.sub(r"0x[0-9a-f]+", "", m.group(1)).strip()
        out.append(Finding(kind="main-thread", title=f"Main Thread Checker: {api}"[:200],
                           signature=[api, *[f.short() for f in keep]], component=component_of(frames),
                           details="\n".join(lines[i:i + 40])))
    return out


# ---------------- crash reports (.ips) -----------------

def parse_ips(path: Path) -> dict | None:
    """A crash report: {"proc", "bundle", "time", "exception", "termination", "asi", "frames": [Frame], "path"}.
    Modern .ips files are a one-line JSON header followed by a JSON body; None for anything else (e.g. hangs)."""
    try:
        head, _, rest = path.read_text(errors="replace").partition("\n")
        header, body = json.loads(head), json.loads(rest)
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or "threads" not in body:
        return None
    images = body.get("usedImages") or []
    threads = body.get("threads") or []
    ft = body.get("faultingThread", 0)
    thread = threads[ft] if 0 <= ft < len(threads) else (threads[0] if threads else {})
    frames = []
    for f in thread.get("frames", []):
        idx = f.get("imageIndex", -1)
        mod = images[idx].get("name", "") if 0 <= idx < len(images) else ""
        loc = f"{f['sourceFile']}:{f.get('sourceLine', 0)}" if f.get("sourceFile") else ""
        frames.append(Frame(f.get("symbol", "") or f"{mod}+{f.get('imageOffset', 0)}", mod, loc))
    return {"proc": body.get("procName") or header.get("app_name", ""), "pid": body.get("pid"),
            "bundle": header.get("bundleID") or (body.get("bundleInfo") or {}).get("CFBundleIdentifier", ""),
            "time": header.get("timestamp") or body.get("captureTime", ""),
            "exception": body.get("exception") or {}, "termination": body.get("termination") or {},
            "asi": body.get("asi") or {}, "queue": thread.get("queue", ""), "frames": frames, "path": str(path)}


def crash_finding(crash: dict, *, top: int = 4) -> Finding:
    exc = crash["exception"]
    what = " ".join(x for x in (exc.get("type", ""), exc.get("signal", "")) if x) or \
        crash["termination"].get("indicator", "") or "crash"
    keep = [f for f in crash["frames"] if f.interesting][:top] or crash["frames"][:top]
    asi = "; ".join(m for msgs in crash["asi"].values() for m in (msgs if isinstance(msgs, list) else [msgs]))
    lead = demangle(keep[0].symbol) if keep else "unknown frame"
    lines = [f"process: {crash['proc']} ({crash['bundle']})", f"exception: {what}", f"queue: {crash['queue']}"]
    if asi:
        lines.append(f"application specific: {asi[:1500]}")
    lines += ["", "crashed thread:"] + [f"  {i:2d} {f.short()}" + (f"  {f.location}" if f.location else "")
                                         for i, f in enumerate(crash["frames"][:40])]
    return Finding(kind="crash", title=f"Crash ({what}) in {lead}"[:200], signature=[what, *[f.short() for f in keep]],
                   component=component_of(crash["frames"]), details="\n".join(lines))


def find_crash_reports(dirs: list[Path], *, since: float, proc: str = "Trio") -> list[Path]:
    """.ips files for `proc` written after `since` (epoch seconds)."""
    out = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.glob(f"{proc}*.ips"):
            try:
                if p.stat().st_mtime >= since:
                    out.append(p)
            except OSError:
                continue
    return sorted(out, key=lambda p: p.stat().st_mtime)


def default_crash_dirs() -> list[Path]:
    return [Path.home() / "Library/Logs/DiagnosticReports", Path.home() / "Library/Logs/DiagnosticReports/Retired"]


def scan_stderr(text: str) -> list[Finding]:
    """Every sanitizer and Main Thread Checker report in one launch's stderr."""
    return [sanitizer_finding(r) for r in parse_sanitizer_reports(text)] + parse_main_thread_checker(text)


def sample_finding(sample_text: str, *, what: str) -> Finding:
    """A hang: the main thread's stack from macOS `sample` output, reduced to its first app frames."""
    # "    2580 Thread_1234   DispatchQueue_1: com.apple.main-thread  (serial)" up to the next thread's header
    main = re.search(r"Thread_\d+\s+DispatchQueue_1: com\.apple\.main-thread.*?"
                     r"(?=\n\s*\d*\s*Thread_\d+|\n\s*Total number|\n\nBinary Images|\Z)", sample_text, re.S)
    frames: list[Frame] = []
    for line in (main.group(0) if main else "").splitlines():
        m = re.search(r"\d+\s+(.+?)\s+\(in ([^)]+)\)", line)
        if m:
            frames.append(Frame(m.group(1).strip(), m.group(2).strip()))
    # sample prints the call tree root-first; the deepest frames are the ones that identify the hang
    deepest = [f for f in reversed(frames) if f.interesting][:3]
    return Finding(kind="hang", title=f"Main thread unresponsive ({what})" + (f" in {deepest[0].symbol}" if deepest else ""),
                   signature=[f.short() for f in deepest] or [what], component=component_of(list(reversed(frames))),
                   details=(main.group(0) if main else sample_text)[:6000])
