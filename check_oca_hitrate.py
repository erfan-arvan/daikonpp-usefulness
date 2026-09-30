#!/usr/bin/env python3
"""Oca RQ5 hit rate: bugs with at least one true catch (an invariant that
HELD in Phase A and was FALSIFIED in Phase B, see rq5_check.compute_rq5)
over the bugs whose Oca run is confirmed complete (oca_status.py).

Bugs that are unverified, failed, incomplete or not started are listed by
state and never counted, neither as hits nor as misses. The bug set is
bugs_last10.csv + bugs.csv without check_daikon_catches.DROPPED_PROJECTS;
by default one section for the latest 5 and one for the latest 10 bugs per
project.

Usage:
    python3 check_oca_hitrate.py [--last N] [--out-root outputs_usefulness] [--csv bugs_last10.csv,bugs.csv] [--no-invariants] [--include-dropped]
"""
import argparse
import json
from pathlib import Path

from check_daikon_catches import DROPPED_PROJECTS, bugs_by_project
from oca_status import oca_status
from rq5_check import RunIncompleteError, compute_rq5


def report(out_root: Path, by_project: dict, label: str, list_invariants: bool, exclude=DROPPED_PROJECTS):
    print(f'######## {out_root} -- {label} (excluded: {", ".join(exclude) or "none"}) ########')
    in_scope = checked = hit = catches = 0
    not_done = {}
    for p in sorted(by_project):
        p_checked = p_hit = 0
        for bid in by_project[p]:
            in_scope += 1
            bug_dir = out_root / f'{p}_{bid}'
            try:
                r = compute_rq5(bug_dir)
            except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError) as e:
                state, why = oca_status(bug_dir)
                if state == 'complete':
                    state, why = 'unreadable', f'{type(e).__name__}: {e}'
                not_done.setdefault(state, []).append(f'{p}_{bid}')
                continue
            checked += 1
            p_checked += 1
            n = len(r['true_catches'])
            if n:
                hit += 1
                p_hit += 1
                catches += n
                print(f'{p}_{bid}: {n} true catch(es)')
                if list_invariants:
                    for k in r['true_catches']:
                        print(f'    {k}')
        print(f'  -- {p}: {p_hit}/{p_checked} bugs caught ({len(by_project[p]) - p_checked} not complete)')

    print()
    print(f'==== TOTAL ({label}) ====')
    print(f'bugs in scope:                   {in_scope}')
    print(f'bugs checked (Oca run complete): {checked}')
    print(f'bugs with >=1 true catch:        {hit}')
    print(f'hit rate:                        {hit / checked:.1%}' if checked else 'hit rate:                        n/a')
    print(f'total true catches:              {catches}')
    for state in sorted(not_done):
        print(f'NOT COUNTED, {state} ({len(not_done[state])}): {" ".join(not_done[state])}')
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=None, help='only this scope (default: latest 5 and latest 10)')
    ap.add_argument('--out-root', default='outputs_usefulness')
    ap.add_argument('--csv', default='bugs_last10.csv,bugs.csv')
    ap.add_argument('--no-invariants', action='store_true', help='omit the caught invariants')
    ap.add_argument('--include-dropped', action='store_true', help=f'also include {", ".join(DROPPED_PROJECTS)}')
    args = ap.parse_args()
    for n in ([args.last] if args.last else [5, 10]):
        exclude = () if args.include_dropped else DROPPED_PROJECTS
        report(Path(args.out_root), bugs_by_project(args.csv, n, exclude), f'LATEST {n} BUGS PER PROJECT',
               not args.no_invariants, exclude)


if __name__ == '__main__':
    main()
