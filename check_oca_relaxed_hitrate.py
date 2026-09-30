#!/usr/bin/env python3
"""RQ5 hit rate of relaxed Oca (outputs_usefulness_relaxed) vs. the original
Oca run (outputs_usefulness) on the SAME bugs, for the latest 5 and latest 10
bugs of each covered project.

A bug is exposed if it has >=1 true catch (held in Phase A, falsified in
Phase B; rq5_check.compute_rq5). Only bugs whose relaxed run is complete are
counted; "newly caught" / "lost" list the bugs whose verdict changed.

Usage:
    python3 check_oca_relaxed_hitrate.py [--list]   # --list: print each exposed bug's catches
"""
import argparse
import json
from pathlib import Path

from check_daikon_catches import bugs_by_project
from check_oca_relaxed_progress import PROJECTS
from rq5_check import RunIncompleteError, compute_rq5


def catches(bug_dir):
    try:
        return compute_rq5(bug_dir)['true_catches']
    except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true', help="print each exposed bug's caught invariants")
    args = ap.parse_args()

    rows = {}
    for p in PROJECTS:
        for b in bugs_by_project('bugs_last10.csv,bugs.csv', 10).get(p, []):
            rel = catches(Path('outputs_usefulness_relaxed') / f'{p}_{b}')
            if rel is None:
                continue
            rows[(p, b)] = (rel, catches(Path('outputs_usefulness') / f'{p}_{b}'))

    for n in (5, 10):
        scope = bugs_by_project('bugs_last10.csv,bugs.csv', n)
        print(f'######## LATEST {n} BUGS PER PROJECT ########')
        print(f'{"project":<16}{"complete":>9}{"relaxed":>9}{"original":>10}  newly caught / lost')
        n_done = n_rel = n_orig = 0
        new_all, lost_all = [], []
        for p in PROJECTS:
            done = [b for b in scope.get(p, []) if (p, b) in rows]
            rel = [b for b in done if rows[(p, b)][0]]
            orig = [b for b in done if rows[(p, b)][1]]
            new = [b for b in rel if b not in orig]
            lost = [b for b in orig if b not in rel]
            new_all += [f'{p}-{b}' for b in new]
            lost_all += [f'{p}-{b}' for b in lost]
            n_done += len(done)
            n_rel += len(rel)
            n_orig += len(orig)
            print(f'{p:<16}{len(done):>9}{len(rel):>9}{len(orig):>10}  {",".join(new) or "-"} / {",".join(lost) or "-"}')
        rate = lambda k: f'{k}/{n_done} ({k / n_done:.0%})' if n_done else f'{k}/0'
        print(f'TOTAL: {n_done} complete | relaxed exposes {rate(n_rel)} | original exposes {rate(n_orig)}')
        print(f'  newly caught by relaxed: {" ".join(new_all) or "-"}')
        print(f'  lost by relaxed:         {" ".join(lost_all) or "-"}')
        print()

    if args.list:
        print('######## CAUGHT INVARIANTS (relaxed) ########')
        for (p, b), (rel, _) in sorted(rows.items()):
            if rel:
                print(f'{p}-{b}: {len(rel)} true catch(es)')
                for k in rel:
                    print(f'    {k}')


if __name__ == '__main__':
    main()
