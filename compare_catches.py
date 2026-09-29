#!/usr/bin/env python3
"""Compare which bugs Oca and Daikon expose (RQ5).

A bug is exposed by Oca if it has at least one true catch (an invariant
HELD in Phase A and FALSIFIED in Phase B, rq5_check.compute_rq5), and by
Daikon if its daikon_outcomes.jsonl has at least one FALSIFIED invariant
(the same rule as check_daikon_catches.py). Only bugs where BOTH tools
finished are compared; bugs finished by only one tool are counted
separately.

Usage:
    python3 compare_catches.py [--last N] [--csv bugs_last10.csv,bugs.csv]
"""
import argparse
import json
from pathlib import Path

from check_daikon_catches import bugs_by_project
from rq5_check import RunIncompleteError, compute_rq5


def oca_exposes(bug_dir: Path):
    """True/False, or None if the Oca run isn't complete."""
    try:
        return bool(compute_rq5(bug_dir)['true_catches'])
    except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError):
        return None


def daikon_exposes(bug_dir: Path):
    """True/False, or None if Daikon has no result."""
    out = bug_dir / 'daikon_outcomes.jsonl'
    if not out.exists():
        return None
    with open(out) as f:
        return any(line.strip() and json.loads(line)['verdict'] == 'FALSIFIED' for line in f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=None)
    ap.add_argument('--csv', default='bugs_last10.csv,bugs.csv')
    args = ap.parse_args()
    by_project = bugs_by_project(args.csv, args.last)

    groups = {'both': [], 'oca_only': [], 'daikon_only': [], 'neither': []}
    only_oca_done, only_daikon_done, none_done = [], [], []

    for p in sorted(by_project):
        for bid in sorted(by_project[p], key=int, reverse=True):
            name = f'{p}-{bid}'
            bug_dir = Path('outputs_usefulness') / f'{p}_{bid}'
            o, d = oca_exposes(bug_dir), daikon_exposes(bug_dir)
            if o is None and d is None:
                none_done.append(name)
            elif d is None:
                only_oca_done.append(name)
            elif o is None:
                only_daikon_done.append(name)
            elif o and d:
                groups['both'].append(name)
            elif o:
                groups['oca_only'].append(name)
            elif d:
                groups['daikon_only'].append(name)
            else:
                groups['neither'].append(name)

    compared = sum(len(v) for v in groups.values())
    labels = {
        'both': 'exposed by BOTH',
        'oca_only': 'exposed by Oca ONLY',
        'daikon_only': 'exposed by Daikon ONLY',
        'neither': 'exposed by NEITHER',
    }
    for k, label in labels.items():
        print(f'{label} ({len(groups[k])}): {" ".join(groups[k])}')
    print()
    print('==== TOTAL' + (f' (latest {args.last} bugs per project)' if args.last else '') + ' ====')
    print(f'bugs in scope:                        {sum(len(v) for v in by_project.values())}')
    print(f'compared (both tools finished):       {compared}')
    if compared:
        oca = len(groups['both']) + len(groups['oca_only'])
        dk = len(groups['both']) + len(groups['daikon_only'])
        print(f'  Oca exposes:                        {oca}/{compared}')
        print(f'  Daikon exposes:                     {dk}/{compared}')
        print(f'  both:                               {len(groups["both"])}')
        print(f'  Oca only (Daikon misses):           {len(groups["oca_only"])}')
        print(f'  Daikon only (Oca misses):           {len(groups["daikon_only"])}')
        print(f'  neither:                            {len(groups["neither"])}')
    print(f'not compared - only Oca finished:     {len(only_oca_done)}')
    print(f'not compared - only Daikon finished:  {len(only_daikon_done)}')
    print(f'not compared - neither finished:      {len(none_done)}')


if __name__ == '__main__':
    main()
