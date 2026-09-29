#!/usr/bin/env python3
"""Per-bug RQ5 results for Oca and Daikon: a table and summary for the latest
5 bugs of each project, then a table and summary for the latest 10.

For every bug prints its rank within its project (1 = highest bug id),
Daikon's status, whether Oca finished, and whether each tool exposed the
bug (same rules as compare_catches.py). Writes the same table to
rq5_bug_results.csv.

Daikon status, from the latest attempt of each bug:
  done              daikon_outcomes.jsonl exists
  running           a Daikon SLURM task for the bug is running or pending
  timeout           hit the 71h job limit (">>> TIMEOUT" in its SLURM .out)
                    or a step timed out (TimeoutExpired, e.g. PrintInvariants)
  error:<reason>    failed: oom, disk-quota, trace-corrupt, chicory,
                    daikon-internal, or other
  not-run           no attempt found (never run, or cancelled)

Usage:
    python3 bug_results_table.py [--csv bugs_last10.csv] [--out rq5_bug_results.csv]
"""
import argparse
import csv
import os
import re
import subprocess
from pathlib import Path

from check_daikon_catches import bugs_by_project
from compare_catches import daikon_exposes, oca_exposes

LOG_DIR = Path('outputs_usefulness/batch_logs_daikon')
BUG_RE = re.compile(r'project=(\S+) bug=(\S+)')

def head(path, n=64 * 1024):
    with open(path, 'rb') as f:
        return f.read(n).decode(errors='replace')


def tail(path, n=2 * 1024 * 1024):
    """Last n bytes only: Daikon logs include all test output and can be GBs."""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - n))
        return f.read().decode(errors='replace')


# First match wins.
ERROR_REASONS = [
    ('oom', re.compile(r'OutOfMemoryError')),
    ('disk-quota', re.compile(r'Disk quota exceeded|No space left on device')),
    ('daikon-internal', re.compile(r'at end of add_modified')),
    ('chicory', re.compile(r'VerifyError|Traversal pattern not initialized|Can\'t find ChicoryPremain')),
    ('trace-corrupt', re.compile(r'Bad modbit|Mismatch between declaration and trace|No declaration was provided'
                                 r"|Didn't find call with nonce|Not in GZIP format|ZLIB|ZipException|EOFException")),
]


def slurm_out_index():
    """{(project, bug): (mtime, path)} of the newest Daikon SLURM .out per bug."""
    idx = {}
    for f in Path('.').glob('usefulness-daikon-*.out'):
        try:
            m = BUG_RE.search(head(f))
            mt = f.stat().st_mtime
        except OSError:
            continue
        if not m:
            continue
        key = (m[1], m[2])
        if key not in idx or mt > idx[key][0]:
            idx[key] = (mt, f)
    return idx


def running_bugs(rows_by_index):
    """Bugs with a Daikon task running or pending right now."""
    out = subprocess.run(['squeue', '-u', os.environ.get('USER', ''), '-h', '-r', '-o', '%j|%F|%K|%T|%Z'],
                         capture_output=True, text=True).stdout
    running = set()
    for line in out.splitlines():
        j, F, K, T, Z = line.split('|')
        if not j.startswith('usefulness-daikon') or not K.isdigit():
            continue
        f = Path(Z) / f'{j}.{F}_{K}.out'
        m = BUG_RE.search(head(f)) if f.exists() else None
        if m:
            running.add((m[1], m[2]))
        elif int(K) in rows_by_index:  # pending: map the array index via the interleaved CSV
            running.add(rows_by_index[int(K)])
    return running


def daikon_status(p, b, done, running, outs):
    if done:
        return 'done'
    if (p, b) in running:
        return 'running'
    log = LOG_DIR / f'{p}_{b}.log'
    text = tail(log) if log.exists() else ''
    out_path = outs.get((p, b), (0, None))[1]
    out_text = tail(out_path, 64 * 1024) if out_path else ''
    if '>>> TIMEOUT' in out_text or 'TimeoutExpired' in text:
        return 'timeout'
    for reason, rx in ERROR_REASONS:
        if rx.search(text):
            return f'error:{reason}'
    if 'Traceback' in text or 'FAILURE' in out_text:
        return 'error:other'
    return 'not-run'


def yn(v):
    return '-' if v is None else ('yes' if v else 'no')


def print_table(rows):
    print(f'{"bug":<22}{"rank":>5}  {"daikon_status":<22}{"oca_done":<10}{"daikon_exposes":<16}{"oca_exposes":<12}')
    for r in rows:
        print(f'{r["project"] + "-" + r["bug"]:<22}{r["rank"]:>5}  {r["daikon_status"]:<22}'
              f'{yn(r["oca_exposes"] is not None):<10}{yn(r["daikon_exposes"]):<16}{yn(r["oca_exposes"]):<12}')
    print()


def summarize(rows, label):
    n = len(rows)
    d_done = [r for r in rows if r['daikon_exposes'] is not None]
    o_done = [r for r in rows if r['oca_exposes'] is not None]
    both = [r for r in rows if r['daikon_exposes'] is not None and r['oca_exposes'] is not None]
    count = lambda rs, f: sum(1 for r in rs if f(r))
    print(f'==== {label}: {n} bugs ====')
    print('Daikon status:')
    statuses = sorted({r['daikon_status'] for r in rows}, key=lambda s: (s != 'done', s))
    for s in statuses:
        print(f'  {s:<28} {count(rows, lambda r: r["daikon_status"] == s)}')
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
    ap.add_argument('--interleaved-csv', default='bugs_last10_interleaved.csv',
                    help='CSV the Daikon array indices refer to (to identify pending tasks)')
    args = ap.parse_args()
    by_project = bugs_by_project(args.csv, None)

    rows_by_index = {}
    if Path(args.interleaved_csv).exists():
        with open(args.interleaved_csv, newline='') as f:
            for i, row in enumerate(csv.DictReader(f)):
                rows_by_index[i] = (row['project'].strip(), row['bug_id'].strip())
    running = running_bugs(rows_by_index)
    outs = slurm_out_index()

    rows = []
    for p in sorted(by_project):
        for rank, bid in enumerate(sorted(by_project[p], key=int, reverse=True), 1):
            bug_dir = Path('outputs_usefulness') / f'{p}_{bid}'
            d = daikon_exposes(bug_dir)
            rows.append({
                'project': p, 'bug': bid, 'rank': rank,
                'daikon_status': daikon_status(p, bid, d is not None, running, outs),
                'oca_exposes': oca_exposes(bug_dir),
                'daikon_exposes': d,
            })

    for n in (5, 10):
        scope = [r for r in rows if r['rank'] <= n]
        print(f'######## LATEST {n} BUGS PER PROJECT ########')
        print_table(scope)
        summarize(scope, f'LATEST {n} per project')

    with open(args.out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['project', 'bug', 'rank', 'daikon_status', 'oca_complete', 'daikon_exposes', 'oca_exposes'])
        for r in rows:
            w.writerow([r['project'], r['bug'], r['rank'], r['daikon_status'],
                        yn(r['oca_exposes'] is not None), yn(r['daikon_exposes']), yn(r['oca_exposes'])])
    print(f'table written to {args.out}')


if __name__ == '__main__':
    main()
