"""`tandemkit-stress file-issues RUN_DIR`: one GitHub issue per distinct finding, in the repository running the tests.

An issue carries its finding's fingerprint in a hidden marker. A later run that sees the same fingerprint comments on
the open issue (commits, seed, link to the run) instead of opening another; a closed issue whose fingerprint comes
back gets a new issue that links the old one, since a fix that did not hold is news.

The repository is --repo, else TKSTRESS_ISSUES_REPO, else GITHUB_REPOSITORY in CI, else this checkout's origin: a run
started by hand files through the local `gh` login into the same place CI does.

Nothing is filed in TandemKit: a person (or Claude, on request) promotes a confirmed driver bug there.
Bodies are scrubbed of long hex/base64 strings and home-directory paths before they leave the machine; the runs use
only faketandem and the Glucose Simulator, but the long-term key the lane generates must never be posted.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from harness.util import HarnessError, log, run

from . import REPO_ROOT, setting

MARKER = "<!-- stress-fingerprint: {fp} -->"
LABELS = {"stress": ("5319e7", "Found by tandemkit-stress (simulator + faketandem)")}
KIND_COLOR = "fbca04"


def scrub(text: str) -> str:
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
    return run(["gh", *args], quiet=True, **kw)


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


def issue_body(f: dict, meta: dict, run_dir: Path) -> str:
    args = meta.get("args") or {}
    subs = (meta.get("git") or {}).get("submodules") or {}
    lines = [MARKER.format(fp=f["fp"]), "",
             f"**{f['kind']}** in **{f['component']}**, seen {f['occurrences']}× in one run"
             + (f" (round {f['round']})" if f.get("round") else "") + ".", "",
             "### Signature", "", "```", *f["signature"], "```", "",
             "### Report", "", "<details><summary>excerpt</summary>", "", "```", f["details"][:5000], "```", "",
             "</details>", "", "### Environment", "", *environment_lines(meta), ""]
    if f.get("evidence"):
        lines += ["### Evidence (in the run directory)", "", *[f"- `{e}`" for e in f["evidence"]], ""]
    lines += ["### Reproduce", "", "```",
              f"./tandemkit-stress run --trio {args.get('trio')} --tandemkit "
              f"{subs.get('TandemKit') or args.get('tandemkit')} --seed {meta.get('seed')} --intensity "
              f"{args.get('intensity')}", "```",
              "The seed fixes the plan of actions, not thread timing: a race may take several runs to reappear.", ""]
    lines += [run_line(run_dir), ""]
    lines += ["Simulated devices only (faketandem, Glucose Simulator); no real pump or health data.", "",
              "---", "_Filed by `tandemkit-stress file-issues`_"]
    return scrub("\n".join(lines))


def comment_body(f: dict, meta: dict, run_dir: Path) -> str:
    lines = [f"Seen again: {f['occurrences']}× in one run" + (f" (round {f['round']})" if f.get("round") else "") + ".",
             "", *environment_lines(meta), "", run_line(run_dir)]
    lines += ["", "---", "_Filed by `tandemkit-stress file-issues`_"]
    return scrub("\n".join(lines))


def file_issues(run_dir: Path, repo: str, *, dry_run: bool = False, kinds: set[str] | None = None) -> list[dict]:
    findings = json.loads((run_dir / "findings.json").read_text()) if (run_dir / "findings.json").exists() else []
    meta = json.loads((run_dir / "run.json").read_text())
    if kinds:
        findings = [f for f in findings if f["kind"] in kinds]
    if not findings:
        log("no findings to file")
        return []
    if not dry_run:
        ensure_labels(repo, {f["kind"] for f in findings})
    out = []
    for f in findings:
        title = scrub(f"[stress] {f['title']}")[:250]
        if dry_run:
            print(f"--- would file: {title}\n{issue_body(f, meta, run_dir)}\n")
            out.append({"fp": f["fp"], "action": "dry-run"})
            continue
        existing = find_issue(repo, f["fp"])
        if existing and existing["state"] == "OPEN":
            _gh(["issue", "comment", str(existing["number"]), "--repo", repo, "--body", comment_body(f, meta, run_dir)])
            out.append({"fp": f["fp"], "action": "commented", "url": existing["url"]})
            continue
        body = issue_body(f, meta, run_dir)
        if existing:
            body += f"\n\nThis fingerprint was previously tracked in {existing['url']} (closed); it came back."
        cp = _gh(["issue", "create", "--repo", repo, "--title", title, "--body", body,
                  "--label", "stress", "--label", f"stress:{f['kind']}"])
        out.append({"fp": f["fp"], "action": "created", "url": cp.stdout.strip().splitlines()[-1] if cp.stdout else ""})
    for o in out:
        log(f"{o['action']}: {o.get('url', o['fp'])}")
    return out


def cmd_file_issues(args) -> int:
    repo = args.repo or issues_repo()
    if not repo and not args.dry_run:
        raise HarnessError("no repository: pass --repo owner/repo or set TKSTRESS_ISSUES_REPO")
    kinds = set(args.kinds.split(",")) if args.kinds else None
    file_issues(Path(args.run_dir).expanduser().resolve(), repo or "", dry_run=args.dry_run, kinds=kinds)
    return 0
