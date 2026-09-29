#!/usr/bin/env python3
"""Per-bug RQ5 results for Oca and Daikon: a table and summary for the latest
5 bugs of each project, then a table and summary for the latest 10.

For every bug in the CSV prints: its rank within its project (1 = highest
bug id), whether Daikon and Oca finished, and whether each exposed the bug
(same rules as compare_catches.py). Writes the same table to
rq5_bug_results.csv.

Usage:
    python3 bug_results_table.py [--csv bugs_last10.csv] [--out rq5_bug_results.csv]
"""
import argparse
import csv
from pathlib import Path

from check_daikon_catches import bugs_by_project
from compare_catches import daikon_exposes, oca_exposes


def yn(v):
    return '-' if v is None else ('yes' if v else 'no')


def print_table(rows):
    print(f'{"bug":<22}{"rank":>5}  {"daikon_done":<12}{"oca_done":<10}{"daikon_exposes":<16}{"oca_exposes":<12}')
    for r in rows:
        print(f'{r["project"] + "-" + r["bug"]:<22}{r["rank"]:>5}  '
              f'{yn(r["daikon_exposes"] is not None):<12}{yn(r["oca_exposes"] is not None):<10}'
              f'{yn(r["daikon_exposes"]):<16}{yn(r["oca_exposes"]):<12}')
    print()


def summarize(rows, label):
    n = len(rows)
    d_done = [r for r in rows if r['daikon_exposes'] is not None]
    o_done = [r for r in rows if r['oca_exposes'] is not None]
    both = [r for r in rows if r['daikon_exposes'] is not None and r['oca_exposes'] is not None]
    count = lambda rs, f: sum(1 for r in rs if f(r))
    print(f'==== {label}: {n} bugs ====')
    print(f'Daikon complete:                 {len(d_done)}/{n}')
    print(f'Oca complete:                    {len(o_done)}/{n}')
    print(f'Daikon exposes (of its complete): {count(d_done, lambda r: r["daikon_exposes"])}/{len(d_done)}')
    print(f'Oca exposes (of its complete):    {count(o_done, lambda r: r["oca_exposes"])}/{len(o_done)}')
    print(f'Both complete:                   {len(both)}')
    if both:
        print(f'  Oca exposes:                   {count(both, lambda r: r["oca_exposes"])}/{len(both)}')
        print(f'  Daikon exposes:                {count(both, lambda r: r["daikon_exposes"])}/{len(both)}')
        print(f'  both expose:                   {count(both, lambda r: r["oca_exposes"] and r["daikon_exposes"])}')
        print(f'  Oca only:                      {count(both, lambda r: r["oca_exposes"] and not r["daikon_exposes"])}')
        print(f'  Daikon only:                   {count(both, lambda r: r["daikon_exposes"] and not r["oca_exposes"])}')
        print(f'  neither:                       {count(both, lambda r: not r["oca_exposes"] and not r["daikon_exposes"])}')
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default='bugs_last10.csv')
    ap.add_argument('--out', default='rq5_bug_results.csv')
    args = ap.parse_args()
    by_project = bugs_by_project(args.csv, None)

    rows = []
    for p in sorted(by_project):
        for rank, bid in enumerate(sorted(by_project[p], key=int, reverse=True), 1):
            bug_dir = Path('outputs_usefulness') / f'{p}_{bid}'
            rows.append({
                'project': p, 'bug': bid, 'rank': rank,
                'oca_exposes': oca_exposes(bug_dir),
                'daikon_exposes': daikon_exposes(bug_dir),
            })

    for n in (5, 10):
        scope = [r for r in rows if r['rank'] <= n]
        print(f'######## LATEST {n} BUGS PER PROJECT ########')
        print_table(scope)
        summarize(scope, f'LATEST {n} per project')

    with open(args.out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['project', 'bug', 'rank', 'daikon_complete', 'oca_complete', 'daikon_exposes', 'oca_exposes'])
        for r in rows:
            w.writerow([r['project'], r['bug'], r['rank'],
                        yn(r['daikon_exposes'] is not None), yn(r['oca_exposes'] is not None),
                        yn(r['daikon_exposes']), yn(r['oca_exposes'])])
    print(f'table written to {args.out}')


if __name__ == '__main__':
    main()
