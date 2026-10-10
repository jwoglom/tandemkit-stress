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
    rounds: list[int] = field(default_factory=list)     # every round it was seen in
    context: dict[str, str] = field(default_factory=dict)  # captured when first seen, for the issue (lane.capture_context)
    aliases: list[str] = field(default_factory=list)    # fingerprints the same finding had before its signature changed

    def __post_init__(self):
        self.fp = self.fp or fingerprint(self.kind, *self.signature)
        self.aliases = [a for a in self.aliases if a != self.fp]


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
        held = self.items[f.fp]
        if f.round is not None and f.round not in held.rounds:
            held.rounds.append(f.round)
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
ACCESS_STACK = re.compile(r"^\s{2}(?:Previous |Atomic )*(?:[Aa]tomic )?(?:read|write|Read|Write|[Mm]odifying access)\b|"
                          r"^(?:READ|WRITE) of size|^freed by thread|^previously allocated by")
# ACCESS_STACK before it read Swift's "Modifying access" stacks; open issues still carry fingerprints made with it.
LEGACY_ACCESS_STACK = re.compile(r"^\s{2}(?:Previous |Atomic )*(?:[Aa]tomic )?(?:read|write|Read|Write)\b|"
                                 r"^(?:READ|WRITE) of size|^freed by thread|^previously allocated by")


def parse_sanitizer_reports(text: str) -> list[dict]:
    """Every sanitizer report in `text`: {"tool", "kind", "summary", "stacks": [[Frame]], "legacy": [bool], "text"}.
    `legacy[i]` is whether LEGACY_ACCESS_STACK would have taken `stacks[i]`."""
    reports: list[dict] = []
    cur: dict | None = None
    for i, line in enumerate(text.splitlines()):
        m = SAN_START.match(line)
        if m:
            cur = {"tool": m.group(1), "kind": m.group(2).strip(), "summary": "", "stacks": [], "legacy": [],
                   "lines": [line], "_take": True, "_legacy": True, "start": i, "complete": False}
            reports.append(cur)
            continue
        if cur is None:
            continue
        cur["lines"].append(line)
        if line.startswith("SUMMARY: "):
            cur["summary"] = line[len("SUMMARY: "):].strip()
            cur["complete"] = True
            cur = None
            continue
        fm = SAN_FRAME.match(line)
        if fm:
            if cur["_take"]:
                if not cur["stacks"] or cur.get("_new_stack"):
                    cur["stacks"].append([])
                    cur["legacy"].append(cur["_legacy"])
                    cur["_new_stack"] = False
                loc = fm.group("loc") or ""
                cur["stacks"][-1].append(Frame(fm.group("sym").strip(), fm.group("mod"),
                                               "" if loc.startswith(("<", "/<")) else loc))
            continue
        stripped = line.strip()
        if stripped:
            # a section header starts the next stack; only access stacks (or the first stack of a non-race report) count
            first_asan = cur["tool"] == "AddressSanitizer" and not cur["stacks"]
            cur["_take"] = bool(ACCESS_STACK.match(line)) or first_asan
            cur["_legacy"] = bool(LEGACY_ACCESS_STACK.match(line)) or first_asan
            cur["_new_stack"] = True
    for r in reports:
        r["text"] = "\n".join(r.pop("lines"))
        for k in ("_take", "_legacy", "_new_stack"):
            r.pop(k, None)
    return reports


def _stack_signature(stack: list[Frame], top: int) -> list[str]:
    return [f.short() for f in ([f for f in stack if f.interesting][:top] or stack[:top])]


def sanitizer_finding(report: dict, *, top: int = 3) -> Finding:
    kind = "tsan" if report["tool"] == "ThreadSanitizer" else "asan"
    stacks = report["stacks"][:2]
    frames_all = [f for stack in stacks for f in stack]
    # TSan prints the access it caught first, and either side of one race can be the one caught
    sig = [report["kind"], *(s for part in sorted(_stack_signature(st, top) for st in stacks) for s in part)]
    legacy = [st for st, old in zip(report["stacks"], report.get("legacy", [])) if old][:2]
    legacy_sig = [s for st in legacy for s in _stack_signature(st, top)]
    lead = next((f for f in frames_all if f.interesting), frames_all[0] if frames_all else None)
    title = f"{report['tool']}: {report['kind']}" + (f" in {demangle(lead.symbol)}" if lead else "")
    # A frameless legacy signature lumped every such race into one issue; nothing should keep filing there.
    return Finding(kind=kind, title=title[:200], signature=sig, component=component_of(frames_all),
                   details=report["text"][:40000],
                   aliases=[fingerprint(kind, report["kind"], *legacy_sig)] if legacy_sig else [])


# ---------------- Main Thread Checker -----------------

MTC_START = re.compile(r"^(?:.*?\s)?Main Thread Checker: (.+)$")
MTC_FRAME = re.compile(r"^\s*\d+\s+(\S+)\s+0x[0-9a-f]+\s+(.+?)(?:\s+\+\s+\d+)?\s*$")


def parse_main_thread_checker(text: str) -> list[Finding]:
    return [f for _, f in _main_thread_checker_reports(text)]


def _main_thread_checker_reports(text: str) -> list[tuple[int, Finding]]:
    out: list[tuple[int, Finding]] = []
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
        out.append((i, Finding(kind="main-thread", title=f"Main Thread Checker: {api}"[:200],
                               signature=[api, *[f.short() for f in keep]], component=component_of(frames),
                               details="\n".join(lines[i:i + 40]))))
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
    def frames_of(raw: list) -> list[Frame]:
        out = []
        for f in raw:
            idx = f.get("imageIndex", -1)
            mod = images[idx].get("name", "") if 0 <= idx < len(images) else ""
            loc = f"{f['sourceFile']}:{f.get('sourceLine', 0)}" if f.get("sourceFile") else ""
            out.append(Frame(f.get("symbol", "") or f"{mod}+{f.get('imageOffset', 0)}", mod, loc))
        return out
    frames = frames_of(thread.get("frames", []))
    others = [{"index": i, "name": t.get("name") or t.get("queue") or "", "frames": frames_of(t.get("frames", []))}
              for i, t in enumerate(threads) if t is not thread]
    return {"proc": body.get("procName") or header.get("app_name", ""), "pid": body.get("pid"),
            "last_exception": frames_of(body.get("lastExceptionBacktrace") or []), "threads": others,
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
    term = crash["termination"]
    if term:
        lines.append(f"termination: {term.get('namespace', '')} {term.get('code', '')} {term.get('indicator', '')}".rstrip())
    lines += ["", "crashed thread:"] + [f"  {i:2d} {f.short()}" + (f"  {f.location}" if f.location else "")
                                         for i, f in enumerate(crash["frames"][:60])]
    if crash.get("last_exception"):
        lines += ["", "last exception backtrace:"] + [f"  {i:2d} {f.short()}" + (f"  {f.location}" if f.location else "")
                                                       for i, f in enumerate(crash["last_exception"][:40])]
    if crash.get("threads"):
        lines += ["", "other threads (top app frames):"]
        for t in crash["threads"][:60]:
            top = [f for f in t["frames"] if f.interesting][:3] or t["frames"][:3]
            lines.append(f"  #{t['index']} {t['name']}: " + " < ".join(f.short() for f in top))
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


def stderr_reports(text: str) -> list[tuple[int, bool, Finding]]:
    """(first line, complete, finding) for every report in a stderr that may still be growing. A sanitizer report is
    complete at its SUMMARY line; a Main Thread Checker report has no end marker, so it waits for the lines its parse
    reads."""
    n = len(text.splitlines())
    return ([(r["start"], r["complete"], sanitizer_finding(r)) for r in parse_sanitizer_reports(text)]
            + [(i, i + 60 <= n, f) for i, f in _main_thread_checker_reports(text)])


# "    2580 Thread_1234   DispatchQueue_1: com.apple.main-thread  (serial)"
SAMPLE_THREAD = re.compile(r"^\s*(\d+)\s+(Thread_\d+.*?)\s*$")
# "    +   2580 main  (in Trio.debug.dylib) + 100  [0x100002000]": the count's column is the depth in the call tree
SAMPLE_FRAME = re.compile(r"^([\s+!:|]*)(\d+)\s+(.+?)\s+\(in ([^)]+)\)")


@dataclass
class SampleThread:
    name: str
    samples: int
    lines: list[str]
    frames: list[tuple[int, int, Frame]]    # (depth, samples, frame), in the order sample prints them: root first

    def path_to(self, i: int) -> list[Frame]:
        """frames[i] and its callers, root first."""
        depth, path = self.frames[i][0], [self.frames[i][2]]
        for d, _, f in reversed(self.frames[:i]):
            if d < depth:
                depth = d
                path.append(f)
        return path[::-1]


def sample_threads(text: str) -> list[SampleThread]:
    """Every thread in the call graph of macOS `sample` output."""
    threads: list[SampleThread] = []
    for line in text.partition("Call graph:")[2].splitlines():
        if line.startswith(("Total number in stack", "Binary Images")):
            break
        if m := SAMPLE_THREAD.match(line):
            threads.append(SampleThread(m.group(2), int(m.group(1)), [line.strip()], []))
        elif threads and line.strip():
            threads[-1].lines.append(line)
            if m := SAMPLE_FRAME.match(line):
                threads[-1].frames.append((len(m.group(1)), int(m.group(2)),
                                           Frame(m.group(3).strip(), m.group(4).strip())))
    return threads


def _stuck_race_report(threads: list[SampleThread]) -> tuple[SampleThread, Frame] | None:
    """A thread inside ThreadSanitizer's race report in every sample, and the access it was reporting. The runtime
    symbolizes a report while it holds its own locks: a thread that holds a lock the symbolizer then needs (dyld's,
    libc's locale lock), and waits on the runtime, deadlocks the process before the report prints."""
    for t in threads:
        for i, (_, n, f) in enumerate(t.frames):
            if f.symbol.startswith("__tsan::ReportRace") and n == t.samples:
                access = next((c for c in reversed(t.path_to(i)) if c.interesting), None)
                if access:
                    return t, access
    return None


def sample_finding(sample_text: str, *, what: str) -> Finding:
    """A hang from macOS `sample` output: the main thread's stack, reduced to its deepest app frames. When a thread is
    stuck in ThreadSanitizer's race report it is named by the reported access instead, since either side of the race
    can be the one caught and the other threads only wait on the runtime."""
    threads = sample_threads(sample_text)
    main = next((t for t in threads if "DispatchQueue_1: com.apple.main-thread" in t.name), None)
    if stuck := _stuck_race_report(threads):
        t, access = stuck
        note = f"{t.name.split()[0]} was inside ThreadSanitizer's race report in all {t.samples} samples; it never printed."
        blocks = [note, "\n".join(t.lines)] + (["\n".join(main.lines)] if main and main is not t else [])
        return Finding(kind="hang", title=f"ThreadSanitizer deadlocked reporting a race in {access.symbol} ({what})"[:200],
                       signature=["ThreadSanitizer race report", access.short()], component=component_of([access]),
                       details="\n\n".join(blocks)[:30000])
    frames = [f for _, _, f in main.frames] if main else []
    # sample prints the call tree root-first; the deepest frames are the ones that identify the hang
    deepest = [f for f in reversed(frames) if f.interesting][:3]
    return Finding(kind="hang", title=f"Main thread unresponsive ({what})" + (f" in {deepest[0].symbol}" if deepest else ""),
                   signature=[f.short() for f in deepest] or [what], component=component_of(list(reversed(frames))),
                   details=("\n".join(main.lines) if main else sample_text)[:30000])
