#!/usr/bin/env python3
"""List the bugs Daikon exposes but Oca does not (RQ5), as a bugs CSV.

Same rules as compare_catches.py, over the latest N bugs of each project
(default 10, including each project's latest bug). Only bugs both tools
finished are considered.

Usage:
    python3 gen_daikon_only_csv.py [--last 10] [--projects Cli,Codec,...] > bugs_daikon_only.csv
"""
import argparse
import sys
from pathlib import Path

from check_daikon_catches import bugs_by_project
from compare_catches import daikon_exposes, oca_exposes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=10)
    ap.add_argument('--csv', default='bugs_last10.csv,bugs.csv')
    ap.add_argument('--projects', default='', help='comma-separated projects to keep (default: all)')
    args = ap.parse_args()
    keep = {p.strip() for p in args.projects.split(',') if p.strip()}

    print('project,bug_id')
    n = 0
    for p, ids in sorted(bugs_by_project(args.csv, args.last).items()):
        if keep and p not in keep:
            continue
        for b in ids:
            d = Path('outputs_usefulness') / f'{p}_{b}'
            if daikon_exposes(d) is True and oca_exposes(d) is False:
                print(f'{p},{b}')
                n += 1
    print(f'{n} bugs', file=sys.stderr)


if __name__ == '__main__':
    main()
