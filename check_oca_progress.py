#!/usr/bin/env python3
"""Per-project progress of the Oca (daikonplusplus) runs of the RQ5
usefulness experiment over the latest 5 and latest 10 bugs of each project
-- the Oca-side twin of check_daikon_progress.py.

The bug set is bugs_last10.csv + bugs.csv (bugs_last10.csv alone lacks each
project's newest bug), without check_daikon_catches.DROPPED_PROJECTS. A bug
counts as done only when oca_status.py confirms its run finished (the
marker run_usefulness_bug.py writes itself, or its job log); unverified,
failed and incomplete runs are counted separately.

Usage:
    python3 check_oca_progress.py [--out-root outputs_usefulness] [--csv bugs_last10.csv,bugs.csv] [--missing] [--include-dropped]

--missing also lists the bug ids that are not complete, by state.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from check_daikon_catches import DROPPED_PROJECTS, bugs_by_project
from oca_status import oca_status

STATES = ("complete", "unverified", "failed", "incomplete", "not_started")


def report(out_root: Path, csv_paths: str, show_missing: bool, exclude=DROPPED_PROJECTS):
    for n in (5, 10):
        bp = bugs_by_project(csv_paths, n, exclude)
        print(f"######## {out_root} -- LATEST {n} BUGS PER PROJECT (excluded: {', '.join(exclude) or 'none'}) ########")
        print(f'{"project":<16}{"complete":>10}{"unverified":>11}{"failed":>8}{"incomplete":>11}{"not started":>12}')
        tot = dict.fromkeys(STATES, 0)
        n_all = 0
        for p in sorted(bp):
            by_state = {s: [] for s in STATES}
            for b in bp[p]:
                by_state[oca_status(out_root / f"{p}_{b}")[0]].append(b)
            n_all += len(bp[p])
            for s in STATES:
                tot[s] += len(by_state[s])
            print(f'{p:<16}{len(by_state["complete"]):>6}/{len(bp[p]):<3}{len(by_state["unverified"]):>11}'
                  f'{len(by_state["failed"]):>8}{len(by_state["incomplete"]):>11}{len(by_state["not_started"]):>12}')
            if show_missing:
                for s in STATES[1:]:
                    if by_state[s]:
                        print(f'    {s}: {",".join(by_state[s])}')
        print(f'{"TOTAL":<16}{tot["complete"]:>6}/{n_all:<3}{tot["unverified"]:>11}{tot["failed"]:>8}'
              f'{tot["incomplete"]:>11}{tot["not_started"]:>12}')
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", default="outputs_usefulness")
    ap.add_argument("--csv", default="bugs_last10.csv,bugs.csv")
    ap.add_argument("--missing", action="store_true")
    ap.add_argument("--include-dropped", action="store_true", help=f"also include {', '.join(DROPPED_PROJECTS)}")
    args = ap.parse_args()
    report(Path(args.out_root), args.csv, args.missing, () if args.include_dropped else DROPPED_PROJECTS)


if __name__ == "__main__":
    main()
