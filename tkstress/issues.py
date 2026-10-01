"""One GitHub issue per distinct finding, in the repository running the tests: filed during `run --file-issues` by a
background thread as findings appear (Publisher), and by `tandemkit-stress file-issues RUN_DIR` for anything a run did
not file.

An issue carries its finding's fingerprint in a hidden marker. A later run that sees the same fingerprint comments on
the open issue instead of opening another; a closed issue whose fingerprint comes back gets a new issue that links the
old one, since a fix that did not hold is news. issues.json in the run directory records what the run filed, so
nothing is filed twice, and a finding seen again later in the same run gets one closing comment with its count.

An issue is meant to be read without the run's logs. Besides the full report it carries what the lane captured when
the finding first appeared (lane.capture_context): round, launch and app memory, the chaos actions before it, the
messages faketandem saw, and TandemKit's log. Sections are cut to fit GitHub's 65536-character limit.

The repository is --repo, else TKSTRESS_ISSUES_REPO, else GITHUB_REPOSITORY in CI, else this checkout's origin: a run
started by hand files through the local `gh` login into the same place CI does. TKSTRESS_ISSUES_TOKEN, when set, is
the token `gh` files with (CI: the workflow's GITHUB_TOKEN, while GH_TOKEN holds the App token the build needs).

Nothing is filed in TandemKit: a person (or Claude, on request) promotes a confirmed driver bug there.
Bodies are scrubbed of long hex/base64 strings, UUIDs and home-directory paths before they leave the machine; the runs
use only faketandem and the Glucose Simulator, but the long-term key the lane generates must never be posted.
"""
from __future__ import annotations

import json
import os
import queue
import re
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
LABELS = {"stress": ("5319e7", "Found by tandemkit-stress (simulator + faketandem)")}
KIND_COLOR = "fbca04"


def scrub(text: str) -> str:
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


def find_issue(repo: str, fp: str) -> dict | None:
    """The issue carrying this fingerprint: an open one if there is one, else the most recent closed one."""
    cp = _gh(["issue", "list", "--repo", repo, "--label", "stress", "--state", "all", "--search", f"{fp} in:body",
              "--json", "number,state,title,body,url", "--limit", "20"])
    hits = [i for i in json.loads(cp.stdout or "[]") if MARKER.format(fp=fp) in (i.get("body") or "")]
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


def environment_lines(meta: dict) -> list[str]:
    args = meta.get("args") or {}
    git = meta.get("git") or {}
    subs = git.get("submodules") or {}
    fk = meta.get("faketandem") or {}
    build = meta.get("build") or {}
    return [f"- Trio: `{args.get('trio')}` @ `{git.get('sha', '?')}`",
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


def issue_body(f: dict, meta: dict, run_dir: Path) -> str:
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
        if f.get("evidence"):
            lines += ["### Files (in the run directory)", "", *[f"- `{e}`" for e in f["evidence"]], ""]
        lines += ["### Reproduce", "", "```",
                  f"./tandemkit-stress run --trio {args.get('trio')} --tandemkit "
                  f"{subs.get('TandemKit') or args.get('tandemkit')} --seed {meta.get('seed')} --intensity "
                  f"{args.get('intensity')}", "```",
                  "The seed fixes the plan of actions, not thread timing: a race may take several runs to reappear.",
                  "", run_line(run_dir), "",
                  "Simulated devices only (faketandem, Glucose Simulator); no real pump or health data.", "",
                  "---", "_Filed by tandemkit-stress_"]
        return scrub("\n".join(lines))
    return _fit(render)


def comment_body(f: dict, meta: dict, run_dir: Path) -> str:
    when = (f.get("context") or {}).get("When")

    def render(section_limit: int) -> str:
        lines = [f"Seen again in another run, {_seen_line(f)}.", ""]
        if when:
            lines += [f"When: {when}", ""]
        lines += _evidence_and_context(f, section_limit)
        lines += ["### Environment", "", *environment_lines(meta), "", run_line(run_dir), "", "---",
                  "_Filed by tandemkit-stress_"]
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


def publish(f: dict, repo: str, meta: dict, run_dir: Path) -> dict:
    """File one finding: a comment on its open issue, else a new issue. Returns its issues.json entry."""
    entry = {"fp": f["fp"], "kind": f["kind"], "occurrences": f["occurrences"],
             "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    existing = find_issue(repo, f["fp"])
    if existing and existing["state"] == "OPEN":
        _gh(["issue", "comment", str(existing["number"]), "--repo", repo, "--body", comment_body(f, meta, run_dir)])
        return {**entry, "action": "commented", "url": existing["url"], "number": existing["number"]}
    body = issue_body(f, meta, run_dir)
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
