"""The apps the lane runs TandemKit inside: Trio, and Loop built from LoopWorkspace. `--host` picks one. The driver,
faketandem and the chaos on the pump's side are the same in both; what differs is the build (trio-test-harness's
app profiles, harness/apps.py), the seeding, the UI flows and what the app's own code is called in a report.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Host:
    key: str                  # trio-test-harness's app (harness/apps.py) and the --host value
    name: str                 # the process, product and crash-report name
    repo: str                 # what `source` is a branch of
    default_source: str
    cgm: str                  # the simulated CGM the seed sets up
    log_files: tuple[str, ...]   # under the data container, copied when a run ends

    @property
    def flows(self):
        from harness import apps
        return apps.get(self.key).flows


TANDEM_LOGS = ("Documents/tandem/tandem_log.txt", "Documents/tandem/tandem_log_prev.txt")

TRIO = Host("trio", "Trio", "Trio", "nightscout/Trio:feat/tandem-closed-beta", "Glucose Simulator",
            (*TANDEM_LOGS, "Documents/logs/log.txt", "Documents/logs/log_prev.txt"))
LOOP = Host("loop", "Loop", "LoopWorkspace", "LoopKit/LoopWorkspace:feat/tandem-closed-beta", "CGM Simulator",
            TANDEM_LOGS)
HOSTS = {h.key: h for h in (TRIO, LOOP)}


def get(key: str | None) -> Host:
    return HOSTS[(key or "trio").lower()]


def source_of(args) -> str:
    """The branch spec the run builds: --loop for Loop, --trio for Trio."""
    host = get(getattr(args, "host", None))
    return getattr(args, host.key, None) or host.default_source
