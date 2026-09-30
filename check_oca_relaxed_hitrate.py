#!/usr/bin/env python3
"""RQ5 hit rate of relaxed Oca (outputs_usefulness_relaxed) vs. the original
Oca run (outputs_usefulness) on the SAME bugs, for the latest 5 and latest 10
bugs of every dataset project (bugs_last10.csv + bugs.csv, without
check_daikon_catches.DROPPED_PROJECTS).

A bug is exposed if it has >=1 true catch (held in Phase A, falsified in
Phase B; rq5_check.compute_rq5). A run counts only if oca_status.py confirms
it finished. The side-by-side rates use only bugs where BOTH runs are
complete; bugs with just one complete run, and runs that are unverified,
failed, incomplete or not started, are listed separately and never counted
as misses. "newly caught" / "lost" list the bugs whose verdict changed.

Usage:
    python3 check_oca_relaxed_hitrate.py [--list] [--include-dropped]   # --list: print each exposed bug's catches
"""
import argparse
import json
from pathlib import Path

from check_daikon_catches import DROPPED_PROJECTS, bugs_by_project
from oca_status import oca_status
from rq5_check import RunIncompleteError, compute_rq5

REL, ORIG = Path('outputs_usefulness_relaxed'), Path('outputs_usefulness')


def catches(bug_dir):
    """(true catches or None, state)."""
    try:
        return compute_rq5(bug_dir)['true_catches'], 'complete'
    except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError) as e:
        state = oca_status(bug_dir)[0]
        return None, (f'unreadable ({type(e).__name__})' if state == 'complete' else state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true', help="print each exposed bug's caught invariants")
    ap.add_argument('--csv', default='bugs_last10.csv,bugs.csv')
    ap.add_argument('--include-dropped', action='store_true', help=f'also include {", ".join(DROPPED_PROJECTS)}')
    args = ap.parse_args()
    exclude = () if args.include_dropped else DROPPED_PROJECTS

    rows = {}
    for p, ids in bugs_by_project(args.csv, 10, exclude).items():
        for b in ids:
            rows[(p, b)] = (catches(REL / f'{p}_{b}'), catches(ORIG / f'{p}_{b}'))

    for n in (5, 10):
        scope = bugs_by_project(args.csv, n, exclude)
        print(f'######## LATEST {n} BUGS PER PROJECT (excluded: {", ".join(exclude) or "none"}) ########')
        print(f'{"project":<16}{"both":>6}{"relaxed":>9}{"original":>10}  newly caught / lost   | not compared')
        n_both = n_rel = n_orig = 0
        new_all, lost_all, skipped = [], [], {}
        for p in sorted(scope):
            both, rel, orig, other = [], [], [], []
            for b in scope[p]:
                (rc, rs), (oc, os_) = rows[(p, b)]
                if rc is None or oc is None:
                    why = f'relaxed {rs}' if rc is None else f'original {os_}'
                    skipped.setdefault(why, []).append(f'{p}_{b}')
                    other.append(b)
                    continue
                both.append(b)
                if rc:
                    rel.append(b)
                if oc:
                    orig.append(b)
            new = [b for b in rel if b not in orig]
            lost = [b for b in orig if b not in rel]
            new_all += [f'{p}-{b}' for b in new]
            lost_all += [f'{p}-{b}' for b in lost]
            n_both += len(both)
            n_rel += len(rel)
            n_orig += len(orig)
            print(f'{p:<16}{len(both):>6}{len(rel):>9}{len(orig):>10}  {",".join(new) or "-"} / {",".join(lost) or "-"}'
                  f'   | {",".join(other) or "-"}')
        rate = lambda k: f'{k}/{n_both} ({k / n_both:.1%})' if n_both else f'{k}/0'
        print(f'TOTAL (both complete): {n_both} | relaxed exposes {rate(n_rel)} | original exposes {rate(n_orig)}')
        print(f'  newly caught by relaxed: {" ".join(new_all) or "-"}')
        print(f'  lost by relaxed:         {" ".join(lost_all) or "-"}')
        for why in sorted(skipped):
            print(f'  NOT COMPARED, {why} ({len(skipped[why])}): {" ".join(skipped[why])}')
        print()

    if args.list:
        print('######## CAUGHT INVARIANTS (relaxed, complete runs) ########')
        for (p, b), ((rc, _), _) in sorted(rows.items()):
            if rc:
                print(f'{p}-{b}: {len(rc)} true catch(es)')
                for k in rc:
                    print(f'    {k}')


if __name__ == '__main__':
    main()
