#!/usr/bin/env python3
"""Oca RQ5 hit rate: bugs with at least one true catch (an invariant that
HELD in Phase A and was FALSIFIED in Phase B, see rq5_check.compute_rq5)
over the bugs whose Oca run is complete.

Usage:
    python3 check_oca_hitrate.py [--last N] [--csv bugs_last10.csv,bugs.csv]

--last N restricts each project to its N highest bug ids in the CSV, the
same bug set as `check_daikon_catches.py --last N`.
"""
import argparse
import json
from pathlib import Path

from check_daikon_catches import bugs_by_project
from rq5_check import RunIncompleteError, compute_rq5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=None)
    ap.add_argument('--csv', default='bugs_last10.csv,bugs.csv')
    args = ap.parse_args()
    by_project = bugs_by_project(args.csv, args.last)

    in_scope = checked = hit = catches = 0
    for p in sorted(by_project):
        p_checked = p_hit = 0
        for bid in by_project[p]:
            in_scope += 1
            bug_dir = Path('outputs_usefulness') / f'{p}_{bid}'
            try:
                r = compute_rq5(bug_dir)
            except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError) as e:
                print(f'{p}_{bid}: SKIP ({type(e).__name__})')
                continue
            checked += 1
            p_checked += 1
            n = len(r['true_catches'])
            if n:
                hit += 1
                p_hit += 1
                catches += n
                print(f'{p}_{bid}: {n} true catch(es)')
                for k in r['true_catches']:
                    print(f'    {k}')
        if p_checked:
            print(f'  -- {p}: {p_hit}/{p_checked} bugs caught')

    print()
    print('==== TOTAL' + (f' (latest {args.last} bugs per project)' if args.last else '') + ' ====')
    print(f'bugs in scope:                   {in_scope}')
    print(f'bugs checked (Oca run complete): {checked}')
    print(f'bugs with >=1 true catch:        {hit}')
    print(f'hit rate:                        {hit / checked:.1%}' if checked else 'hit rate:                        n/a')
    print(f'total true catches:              {catches}')


if __name__ == '__main__':
    main()
