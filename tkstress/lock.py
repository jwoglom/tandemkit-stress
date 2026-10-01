"""One stress run at a time per build cache, whether it was started by hand or by a CI job on the same Mac.

The lock is a flock on <cache>/lane.lock, so the OS drops it when its process exits and it is never stale. Its file
holds who has it, for the message the next run prints. The harness's own device lock does not cover this: it lives in
whichever harness checkout is in use, and a CI job checks out its own.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from harness.util import HarnessError, log


class LaneBusy(HarnessError):
    pass


def describe(path: Path) -> str:
    try:
        h = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        h = {}
    since = (h.get("since") or "?").replace("T", " ")[:16]
    return f"{h.get('what', 'another run')} (pid {h.get('pid', '?')}, since {since})"


def holder_description() -> str:
    """What a run started by hand or by CI calls itself in the lock file."""
    if os.environ.get("GITHUB_RUN_ID"):
        return f"GitHub Actions run {os.environ['GITHUB_RUN_ID']} ({os.environ.get('GITHUB_WORKFLOW', 'stress')})"
    return "a tandemkit-stress run started by hand"


@contextmanager
def hold(path: Path, what: str, *, wait: bool = False, give_up=lambda: False, poll: float = 15):
    """Hold the lane for the duration. While another process has it: raise LaneBusy, or with wait=True poll until it
    is free, raising LaneBusy once give_up() is true."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        announced = False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                other = describe(path)
                if not wait or give_up():
                    raise LaneBusy(f"the stress lane on this machine is in use by {other}") from None
                if not announced:
                    log(f"waiting for the stress lane: {other}")
                    announced = True
                time.sleep(poll)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"what": what, "pid": os.getpid(),
                             "since": dt.datetime.now().isoformat(timespec="seconds")}))
        fh.flush()
        yield
    finally:
        fh.close()
