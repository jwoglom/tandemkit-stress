"""One GitHub issue per distinct finding, in the repository running the tests: filed during `run --file-issues` by a
background thread as findings appear (Publisher), and by `tandemkit-stress file-issues RUN_DIR` for anything a run did
not file.

An issue carries its finding's fingerprint in a hidden marker. A later run that sees the same fingerprint, or one of
the finding's aliases (what its signature made before the signature changed), comments on the open issue instead of
opening another; a closed issue whose fingerprint comes back gets a new issue that links the old one, since a fix that
did not hold is news. issues.json in the run directory records what the run filed, so nothing is filed twice, and a
finding seen again later in the same run gets one closing comment with its count.

An issue is meant to be read without the run's logs. Besides the full report it carries what the lane captured when
the finding first appeared (lane.capture_context): round, launch and app memory, the chaos actions before it, the
messages faketandem saw, and TandemKit's log. Sections are cut to fit GitHub's 65536-character limit.

The finding's files (crash report, hang sample, the launch's stderr, screenshots) are attached. The REST API cannot
attach a file to an issue, so they are uploaded as assets of a monthly pre-release, stress-attachments-YYYY-MM, which
the issue links; that needs contents: write. A crash report loses the machine's crash-reporter identifiers, the home
path and the simulator's device id first, and keeps its image UUIDs, which symbolication needs.

The repository is --repo, else TKSTRESS_ISSUES_REPO, else GITHUB_REPOSITORY in CI, else this checkout's origin: a run
started by hand files through the local `gh` login into the same place CI does. TKSTRESS_ISSUES_TOKEN, when set, is
the token `gh` files with (CI: the workflow's GITHUB_TOKEN, while GH_TOKEN holds the App token the build needs).

Nothing is filed in TandemKit: a person (or Claude, on request) promotes a confirmed driver bug there.
Bodies are scrubbed of long hex/base64 strings, UUIDs and home-directory paths before they leave the machine; the runs
use only faketandem and the app's simulated CGM, but the long-term key the lane generates must never be posted.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from harness.util import HarnessError, log, run

from . import REPO_ROOT, setting

MARKER = "<!-- stress-fingerprint: {fp} -->"
LEDGER = "issues.json"
BODY_LIMIT = 60000          # GitHub refuses bodies over 65536 characters
REPORT_LIMIT = 20000
SECTION_LIMIT = 12000
ATTACH_MAX_BYTES = 50 * 1024 * 1024
IPS_IDENTIFIERS = ("crashReporterKey", "incident", "incident_id", "sessionID", "beta_identifier")
LABELS = {"stress": ("5319e7", "Found by tandemkit-stress (simulator + faketandem)")}
KIND_COLOR = "fbca04"


def scrub_paths(text: str) -> str:
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    text = re.sub(r"(CoreSimulator/Devices/)[0-9A-Fa-f-]{36}", r"\1<device>", text)
    return re.sub(r"(Containers/(?:Bundle|Data|Shared/AppGroup)/Application/)[0-9A-Fa-f-]{36}", r"\1<app>", text)


def scrub_ips(text: str) -> str:
    """A crash report fit to attach: no machine identifiers or home paths, image UUIDs kept for symbolication."""
    head, _, rest = text.partition("\n")
    try:
        header, body = json.loads(head), json.loads(rest)
    except ValueError:
        return scrub_paths(text)
    for part in (header, body):
        for k in IPS_IDENTIFIERS:
            part.pop(k, None)
    return scrub_paths(json.dumps(header) + "\n" + json.dumps(body, indent=2))


def scrub(text: str) -> str:
    text = scrub_paths(text)
    text = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<uuid>", text)
    text = re.sub(r"\b[0-9a-fA-F]{32,}\b", "<hex>", text)
    text = re.sub(r"\b[A-Za-z0-9+/]{40,}={0,2}", "<base64>", text)
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    return text


def repo_from_remote(url: str) -> str | None:
    """owner/repo from a GitHub remote URL (https or ssh), else None."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?$",
                 url.strip())
    return m.group(1) if m else None


def issues_repo() -> str | None:
    if setting("TKSTRESS_ISSUES_REPO"):
        return setting("TKSTRESS_ISSUES_REPO")
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    cp = run(["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"], check=False, quiet=True)
    return repo_from_remote(cp.stdout or "") if cp.returncode == 0 else None


def check_gh(repo: str) -> None:
    """Fail now, not after hours of running, when `gh` cannot file into `repo`."""
    try:
        cp = _gh(["repo", "view", repo, "--json", "hasIssuesEnabled"], check=False)
    except OSError:
        raise HarnessError("gh is not installed (brew install gh), and --file-issues needs it") from None
    if cp.returncode != 0:
        raise HarnessError(f"gh cannot read {repo} (gh auth login?): {(cp.stderr or cp.stdout or '').strip()[:300]}")
    if not json.loads(cp.stdout or "{}").get("hasIssuesEnabled", True):
        raise HarnessError(f"{repo} has issues turned off")


def _gh(args: list[str], **kw):
    token = setting("TKSTRESS_ISSUES_TOKEN")
    return run(["gh", *args], quiet=True, env={"GH_TOKEN": token} if token else None, **kw)


def ensure_labels(repo: str, kinds: set[str]) -> None:
    for name, (color, desc) in {**LABELS, **{f"stress:{k}": (KIND_COLOR, f"tandemkit-stress finding: {k}") for k in kinds}}.items():
        _gh(["label", "create", name, "--repo", repo, "--color", color, "--description", desc, "--force"], check=False)


def find_issue(repo: str, fp: str, aliases: list[str] | tuple[str, ...] = ()) -> dict | None:
    """The issue carrying this fingerprint, or one the finding had before its signature changed: an open one if there
    is one, else the most recent closed one."""
    hits = []
    for key in (fp, *aliases):
        cp = _gh(["issue", "list", "--repo", repo, "--label", "stress", "--state", "all", "--search", f"{key} in:body",
                  "--json", "number,state,title,body,url", "--limit", "20"])
        hits += [i for i in json.loads(cp.stdout or "[]") if MARKER.format(fp=key) in (i.get("body") or "")]
    hits.sort(key=lambda i: (i["state"] != "OPEN", -i["number"]))
    return hits[0] if hits else None


def run_url() -> str | None:
    if os.environ.get("GITHUB_RUN_ID"):
        return (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}"
                f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    return None


def run_line(run_dir: Path) -> str:
    link = run_url()
    return f"Run: {link}" if link else f"Run: started by hand; `runs/{run_dir.name}` on the Mac that ran it (not uploaded)"


def _host(args: dict):
    from .hosts import get
    return get(args.get("host"))


def host_flags(args: dict) -> str:
    """The flags that build a run's host app again (runs from before Loop was a host built Trio)."""
    host = _host(args)
    return f"--trio {args.get('trio')}" if host.key == "trio" else f"--host {host.key} --{host.key} {args.get(host.key)}"


def environment_lines(meta: dict) -> list[str]:
    args = meta.get("args") or {}
    git = meta.get("git") or {}
    subs = git.get("submodules") or {}
    fk = meta.get("faketandem") or {}
    build = meta.get("build") or {}
    host = _host(args)
    return [f"- {host.name}: `{args.get(host.key)}` @ `{git.get('sha', '?')}`",
            f"- TandemKit: `{args.get('tandemkit')}` @ `{subs.get('TandemKit', '?')}`",
            f"- faketandem: `{fk.get('ref', '?')}` @ `{fk.get('sha', '?')}`",
            f"- seed `{meta.get('seed')}`, sanitizer `{args.get('sanitizer')}`, intensity `{args.get('intensity')}`"
            + (f", Xcode {build.get('xcode')}" if build.get("xcode") else "")]


def _block(title: str, text: str, limit: int, *, keep: str = "tail") -> list[str]:
    """A collapsed section; logs keep their end, reports their start."""
    text = text.strip("\n")
    if not text:
        return []
    if len(text) > limit:
        cut = len(text) - limit
        text = (f"[{cut} earlier characters cut]\n" + text[-limit:]) if keep == "tail" else \
            (text[:limit] + f"\n[{cut} more characters cut]")
    fence = "````" if "```" in text else "```"
    return [f"<details><summary>{title}</summary>", "", fence, text, fence, "", "</details>", ""]


def _evidence_and_context(f: dict, section_limit: int) -> list[str]:
    ctx = dict(f.get("context") or {})
    ctx.pop("When", None)
    lines = ["### Report", "", *_block(f"{f['kind']} report", f.get("details", ""), REPORT_LIMIT, keep="head")]
    if ctx:
        lines += ["### What led up to it", ""]
        for title, text in ctx.items():
            lines += _block(title, text, section_limit)
    return lines


def _fit(render) -> str:
    """render(section_limit) shrunk until the body fits GitHub's limit."""
    limit = SECTION_LIMIT
    body = render(limit)
    while len(body) > BODY_LIMIT and limit > 500:
        limit //= 2
        body = render(limit)
    return body[:BODY_LIMIT]


def _seen_line(f: dict) -> str:
    rounds = f.get("rounds") or ([f["round"]] if f.get("round") else [])
    where = f" (round{'s' if len(rounds) > 1 else ''} {', '.join(map(str, rounds))})" if rounds else ""
    return f"seen {f['occurrences']}× in this run so far{where}"


def _attachment_lines(f: dict, attachments: tuple[list[str], list[str]] | None) -> list[str]:
    if not f.get("evidence"):
        return []
    links, problems = attachments or ([], [])
    lines = ["### Attachments", "", *links]
    if problems:
        lines += ["", "Not attached, so in the run directory (the Actions artifact): " + "; ".join(problems)]
    elif not links:
        lines += [f"- `{e}` (in the run directory)" for e in f["evidence"]]
    return lines + [""]


def issue_body(f: dict, meta: dict, run_dir: Path, attachments: tuple[list[str], list[str]] | None = None) -> str:
    args = meta.get("args") or {}
    subs = (meta.get("git") or {}).get("submodules") or {}
    when = (f.get("context") or {}).get("When")

    def render(section_limit: int) -> str:
        lines = [MARKER.format(fp=f["fp"]), "", f"**{f['kind']}** in **{f['component']}**, {_seen_line(f)}.", ""]
        if when:
            lines += [f"When: {when}", ""]
        lines += ["### Signature", "", "```", *f["signature"], "```", ""]
        lines += _evidence_and_context(f, section_limit)
        lines += ["### Environment", "", *environment_lines(meta), ""]
        lines += _attachment_lines(f, attachments)
        lines += ["### Reproduce", "", "```",
                  f"./tandemkit-stress run {host_flags(args)} --tandemkit "
                  f"{subs.get('TandemKit') or args.get('tandemkit')} --seed {meta.get('seed')} --intensity "
                  f"{args.get('intensity')}", "```",
                  "The seed fixes the plan of actions, not thread timing: a race may take several runs to reappear.",
                  "", run_line(run_dir), "",
                  f"Simulated devices only (faketandem, {_host(args).cgm}); no real pump or health data.", "",
                  "---", "_Filed by tandemkit-stress_"]
        return scrub("\n".join(lines))
    return _fit(render)


def comment_body(f: dict, meta: dict, run_dir: Path, attachments: tuple[list[str], list[str]] | None = None) -> str:
    when = (f.get("context") or {}).get("When")

    def render(section_limit: int) -> str:
        lines = [f"Seen again in another run, {_seen_line(f)}.", ""]
        if when:
            lines += [f"When: {when}", ""]
        lines += _evidence_and_context(f, section_limit)
        lines += ["### Environment", "", *environment_lines(meta), ""]
        lines += _attachment_lines(f, attachments)
        lines += [run_line(run_dir), "", "---", "_Filed by tandemkit-stress_"]
        return scrub("\n".join(lines))
    return _fit(render)


def read_ledger(run_dir: Path) -> dict[str, dict]:
    p = run_dir / LEDGER
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def write_ledger(run_dir: Path, ledger: dict[str, dict]) -> None:
    tmp = run_dir / (LEDGER + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2))
    tmp.replace(run_dir / LEDGER)


def _gh_error(cp) -> str:
    out = ((cp.stderr or "") + (cp.stdout or "")).strip()
    return out.splitlines()[-1][:200] if out else f"gh exited {cp.returncode}"


def attach(repo: str, run_dir: Path, rel_paths: list[str], label: str) -> tuple[list[str], list[str]]:
    """Upload a finding's files as assets of this month's attachments release: (markdown links, problems)."""
    files = [(r, run_dir / r) for r in rel_paths if (run_dir / r).is_file()]
    if not files:
        return [], []
    tag = time.strftime("stress-attachments-%Y-%m")
    if _gh(["release", "view", tag, "--repo", repo], check=False).returncode != 0:
        cp = _gh(["release", "create", tag, "--repo", repo, "--prerelease", "--title", f"Stress attachments {tag[-7:]}",
                  "--notes", "Crash reports, samples, logs and screenshots that tandemkit-stress issues link to."],
                 check=False)
        if cp.returncode != 0 and _gh(["release", "view", tag, "--repo", repo], check=False).returncode != 0:
            return [], [f"no release to attach to ({_gh_error(cp)})"]
    links, problems = [], []
    with tempfile.TemporaryDirectory() as d:
        for rel, path in files:
            if path.stat().st_size > ATTACH_MAX_BYTES:
                problems.append(f"`{rel}` is over {ATTACH_MAX_BYTES // 2**20} MB")
                continue
            name = re.sub(r"[^\w.-]+", "-", f"{label}-{path.name}")
            out = Path(d) / name
            if path.suffix == ".ips":
                out.write_text(scrub_ips(path.read_text(errors="replace")))
            elif path.suffix in (".txt", ".stderr", ".log", ".json", ".jsonl"):
                out.write_text(scrub(path.read_text(errors="replace")))
            else:
                shutil.copy(path, out)
            cp = _gh(["release", "upload", tag, str(out), "--repo", repo, "--clobber"], check=False)
            if cp.returncode == 0:
                links.append(f"- [{path.name}](https://github.com/{repo}/releases/download/{tag}/{name}) (`{rel}`)")
            else:
                problems.append(f"`{rel}` ({_gh_error(cp)})")
    return links, problems


def publish(f: dict, repo: str, meta: dict, run_dir: Path) -> dict:
    """File one finding, with its files attached: a comment on its open issue, else a new issue. Returns its
    issues.json entry."""
    entry = {"fp": f["fp"], "kind": f["kind"], "occurrences": f["occurrences"],
             "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    attachments = attach(repo, run_dir, f.get("evidence") or [], f"{f['fp']}-{run_dir.name}")
    if attachments[0]:
        entry["attachments"] = [re.search(r"\((https://[^)]+)\)", link).group(1) for link in attachments[0]]
    existing = find_issue(repo, f["fp"], f.get("aliases") or ())
    if existing and existing["state"] == "OPEN":
        _gh(["issue", "comment", str(existing["number"]), "--repo", repo, "--body",
             comment_body(f, meta, run_dir, attachments)])
        return {**entry, "action": "commented", "url": existing["url"], "number": existing["number"]}
    body = issue_body(f, meta, run_dir, attachments)
    if existing:
        body = body[:BODY_LIMIT - 200] + f"\n\nThis fingerprint was previously tracked in {existing['url']} (closed); it came back."
    cp = _gh(["issue", "create", "--repo", repo, "--title", scrub(f"[stress] {f['title']}")[:250], "--body", body,
              "--label", "stress", "--label", f"stress:{f['kind']}"])
    url = cp.stdout.strip().splitlines()[-1] if cp.stdout and cp.stdout.strip() else ""
    number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url.rstrip("/").rsplit("/", 1)[-1].isdigit() else None
    return {**entry, "action": "created", "url": url, "number": number}


def file_issues(run_dir: Path, repo: str, *, dry_run: bool = False, kinds: set[str] | None = None) -> list[dict]:
    """File the findings issues.json has no entry for, then one closing comment per filed finding seen again since."""
    findings = json.loads((run_dir / "findings.json").read_text()) if (run_dir / "findings.json").exists() else []
    meta = json.loads((run_dir / "run.json").read_text())
    if kinds:
        findings = [f for f in findings if f["kind"] in kinds]
    ledger = read_ledger(run_dir)
    todo = [f for f in findings if f["fp"] not in ledger]
    if dry_run:
        for f in todo:
            print(f"--- would file: [stress] {f['title']}\n{issue_body(f, meta, run_dir)}\n")
        return [{"fp": f["fp"], "action": "dry-run"} for f in todo]
    if todo:
        ensure_labels(repo, {f["kind"] for f in todo})
    out = []
    for f in todo:
        entry = publish(f, repo, meta, run_dir)
        entry["reported_occurrences"] = f["occurrences"]
        ledger[f["fp"]] = entry
        write_ledger(run_dir, ledger)
        out.append(entry)
        log(f"issue {entry['action']}: {entry.get('url')}")
    for f in findings:
        e = ledger.get(f["fp"])
        if not e or not e.get("number") or f["occurrences"] <= e.get("reported_occurrences", e.get("occurrences", 1)):
            continue
        _gh(["issue", "comment", str(e["number"]), "--repo", repo, "--body", scrub(
            f"This run saw it {f['occurrences']}× in all" + (f", in rounds {', '.join(map(str, f['rounds']))}"
                                                            if f.get("rounds") else "")
            + f".\n\n{run_line(run_dir)}\n\n---\n_Filed by tandemkit-stress_")])
        e["reported_occurrences"] = f["occurrences"]
        write_ledger(run_dir, ledger)
    if not findings:
        log("no findings to file")
    return out


class Publisher:
    """Files each new finding on a background thread as the run reports it, so the issue exists minutes after the
    problem, not at the end of the run, or never if the job is killed first."""

    def __init__(self, repo: str, run_dir: Path):
        self.repo, self.run_dir = repo, run_dir
        self.queue: queue.Queue = queue.Queue()
        self.labelled: set[str] = set()
        self.thread = threading.Thread(target=self._work, name="issue-publisher", daemon=True)
        self.thread.start()

    def submit(self, finding: dict, meta: dict) -> None:
        self.queue.put((finding, meta))

    def _work(self) -> None:
        while (item := self.queue.get()) is not None:
            f, meta = item
            ledger = read_ledger(self.run_dir)
            if f["fp"] in ledger:
                continue
            try:
                if f["kind"] not in self.labelled:
                    ensure_labels(self.repo, {f["kind"]})
                    self.labelled.add(f["kind"])
                entry = publish(f, self.repo, meta, self.run_dir)
            except Exception as e:  # noqa: BLE001 - a failed filing must not stop the run; close() retries it
                log(f"filing {f['kind']} {f['fp']} failed, retried at the end of the run: {e}")
                continue
            ledger[f["fp"]] = entry
            write_ledger(self.run_dir, ledger)
            log(f"issue {entry['action']}: {entry.get('url')}")
            time.sleep(1)       # GitHub asks for a pause between requests that create content

    def close(self, timeout: float = 300) -> None:
        """Finish the queue, then file what was never submitted or failed, and close out repeats (file_issues)."""
        self.queue.put(None)
        self.thread.join(timeout)
        if self.thread.is_alive():
            log(f"issue filing still busy after {timeout:.0f}s; finish with `tandemkit-stress file-issues {self.run_dir}`")
            return
        try:
            file_issues(self.run_dir, self.repo)
        except HarnessError as e:
            log(f"filing issues failed: {e}\nretry with: ./tandemkit-stress file-issues {self.run_dir} --repo {self.repo}")


def cmd_file_issues(args) -> int:
    repo = args.repo or issues_repo()
    if not repo and not args.dry_run:
        raise HarnessError("no repository: pass --repo owner/repo or set TKSTRESS_ISSUES_REPO")
    kinds = set(args.kinds.split(",")) if args.kinds else None
    file_issues(Path(args.run_dir).expanduser().resolve(), repo or "", dry_run=args.dry_run, kinds=kinds)
    return 0
