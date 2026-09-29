#!/usr/bin/env python3
"""List the Daikon bugs with at least one FALSIFIED invariant (RQ5).

Usage:
    python3 check_daikon_catches.py [--last N] [--csv bugs_last10.csv]

--last N restricts each project to its N highest bug ids in the CSV (e.g.
--last 5 for the latest 5 bugs); without it every bug in the CSV is used.
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def bugs_by_project(csv_path, last):
    by_project = defaultdict(list)
    with open(csv_path, newline='') as f:
        for row in csv.DictReader(f):
            by_project[row['project'].strip()].append(row['bug_id'].strip())
    if last:
        for p in by_project:
            by_project[p] = sorted(by_project[p], key=int, reverse=True)[:last]
    return by_project


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=None)
    ap.add_argument('--csv', default='bugs_last10.csv')
    args = ap.parse_args()
    by_project = bugs_by_project(args.csv, args.last)

    total_bugs_checked = 0
    total_with_catch = 0
    total_falsified = 0

    for p in sorted(by_project):
        for bid in by_project[p]:
            out_file = Path('outputs_usefulness') / f'{p}_{bid}' / 'daikon_outcomes.jsonl'
            if not out_file.exists():
                continue
            total_bugs_checked += 1
            falsified = []
            with open(out_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec['verdict'] == 'FALSIFIED':
                        falsified.append(rec)
            if falsified:
                total_with_catch += 1
                total_falsified += len(falsified)
                print(f'{p}_{bid}: {len(falsified)} FALSIFIED invariant(s)')
                for rec in falsified:
                    print(f"    ppt={rec['ppt']}  inv={rec['invariant']}")

    print()
    print('==== TOTAL' + (f' (latest {args.last} bugs per project)' if args.last else '') + ' ====')
    print(f'bugs in scope:                                {sum(len(v) for v in by_project.values())}')
    print(f'bugs checked (daikon_outcomes.jsonl present): {total_bugs_checked}')
    print(f'bugs with at least one FALSIFIED invariant:   {total_with_catch}')
    print(f'total FALSIFIED invariant records:            {total_falsified}')


if __name__ == '__main__':
    main()
