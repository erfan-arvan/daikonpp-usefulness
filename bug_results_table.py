#!/usr/bin/env python3
"""Per-bug RQ5 results for Oca and Daikon: a table and summary for the latest
5 bugs of each project, then a table and summary for the latest 10.

For every bug prints its rank within its project (1 = highest bug id),
Daikon's status, whether Oca finished, and whether each tool exposed the
bug (same rules as compare_catches.py). Writes the same table to
rq5_bug_results.csv.

Daikon status, from the latest attempt of each bug:
  done              daikon_outcomes.jsonl exists
  running / pending a Daikon SLURM task for the bug is running / queued (incl. held)
  timeout           hit the 71h job limit (">>> TIMEOUT" in its SLURM .out)
                    or a step timed out (TimeoutExpired, e.g. PrintInvariants)
  cancelled         stopped by scancel (SIGTERM without a timeout)
  error:<reason>    failed: oom, disk-quota, trace-corrupt, chicory,
                    daikon-internal, or other (matched on error lines only,
                    not on test output)
  error:chicory-hang  log stops inside Chicory (no error, no end marker)
  incomplete        has a log but ended without any of the above
  not-run           no attempt found

Usage:
    python3 bug_results_table.py [--csv bugs_last10.csv,bugs.csv] [--out rq5_bug_results.csv]
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
    # "No declaration was provided": Chicory's decl/data desync bug (Bug 4 in
    # DAIKON_CHICORY_BUGS_AND_FIXES.md), e.g. Jsoup.
    ('chicory', re.compile(r'VerifyError|Traversal pattern not initialized|Can\'t find ChicoryPremain'
                           r'|No declaration was provided')),
    ('trace-corrupt', re.compile(r'Bad modbit|Mismatch between declaration and trace'
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


def load_csv_rows(path):
    with open(path, newline='') as f:
        return {i: (r['project'].strip(), r['bug_id'].strip()) for i, r in enumerate(csv.DictReader(f))}


def queue_states(default_rows):
    """{(project, bug): 'running' | 'pending'} for Daikon tasks in the queue now.

    A pending task has no .out yet, so its bug comes from its array index in
    the CSV the job was submitted with: the CSV named in the job's SLURM
    comment (`scontrol update JobId=<id> Comment=bugs_latest.csv`), else
    bugs_last10_interleaved.csv.
    """
    out = subprocess.run(['squeue', '-u', os.environ.get('USER', ''), '-h', '-r', '-o', '%j|%F|%K|%T|%Z|%k'],
                         capture_output=True, text=True).stdout
    csv_cache = {}
    states = {}
    for line in out.splitlines():
        j, F, K, T, Z, comment = line.split('|', 5)
        if not j.startswith('usefulness-daikon') or not K.isdigit():
            continue
        f = Path(Z) / f'{j}.{F}_{K}.out'
        m = BUG_RE.search(head(f)) if f.exists() else None
        if m:
            key = (m[1], m[2])
        else:
            rows = default_rows
            c = comment.strip()
            if c.endswith('.csv'):
                path = Path(Z) / c
                if path not in csv_cache:
                    csv_cache[path] = load_csv_rows(path) if path.exists() else {}
                rows = csv_cache[path]
            if int(K) not in rows:
                continue
            key = rows[int(K)]
        state = 'running' if T == 'RUNNING' else 'pending'
        if states.get(key) != 'running':
            states[key] = state
    return states


def error_lines(text, n=300):
    """Last n log lines that can carry a tool error: test output
    ([DaikonTestRunner] ...), javac chatter and stack frames are dropped so
    that e.g. a test named ...EOFException... isn't read as a Daikon error."""
    keep = [l for l in text.splitlines()
            if not l.startswith(('[DaikonTestRunner]', '    [javac]', '\tat ', '    at '))]
    return '\n'.join(keep[-n:])


def slurm_state(out_path):
    """SLURM's final state of the task that wrote out_path
    (<name>.<jobid>_<task>.out), e.g. CANCELLED, TIMEOUT, COMPLETED."""
    m = re.search(r'\.(\d+_\d+)\.out$', str(out_path))
    if not m:
        return ''
    out = subprocess.run(['sacct', '-j', m[1], '-X', '-n', '-o', 'State%30'],
                         capture_output=True, text=True).stdout.split()
    return out[0] if out else ''


# Bugs we stopped on purpose, with the reason we report. Applied only when
# the bug is not done and not in the queue, so a later rerun still shows its
# real state.
#  - Jsoup: Chicory fails on this project (VerifyError, "Traversal pattern not
#    initialized", "No declaration was provided", hangs); its remaining bugs
#    were cancelled once that was established.
#  - Compress-46, JacksonCore-25: still in Chicory phase A after 52h with
#    400-550G traces; cancelled because they could not finish in the 71h
#    limit (and were filling /project).
STOPPED = {
    ('Jsoup', None): 'error:chicory',
    ('Compress', '46'): 'timeout',
    ('JacksonCore', '25'): 'timeout',
}


def daikon_status(p, b, done, queue, outs):
    if done:
        return 'done'
    if (p, b) in queue:
        return queue[(p, b)]
    stopped = STOPPED.get((p, b)) or STOPPED.get((p, None))
    if stopped:
        return stopped
    log = LOG_DIR / f'{p}_{b}.log'
    text = tail(log) if log.exists() else ''
    out_path = outs.get((p, b), (0, None))[1]
    out_text = tail(out_path, 64 * 1024) if out_path else ''
    if '>>> TIMEOUT' in out_text or 'TimeoutExpired' in text:
        return 'timeout'
    state = slurm_state(out_path) if out_path else ''
    if state == 'TIMEOUT':
        return 'timeout'
    if 'caught SIGTERM' in text or 'caught SIGTERM' in out_text or state.startswith('CANCELLED'):
        return 'cancelled'
    errs = error_lines(text)
    for reason, rx in ERROR_REASONS:
        if rx.search(errs):
            return f'error:{reason}'
    if 'Traceback' in text or 'FAILURE' in out_text:
        return 'error:other'
    if not log.exists():
        return 'not-run'
    # Log stops inside Chicory with no error or end marker: Chicory hung and
    # the task was stopped by hand (e.g. Jsoup-83..88).
    last = errs.strip().splitlines()[-1] if errs.strip() else ''
    return 'error:chicory-hang' if last.startswith('Chicory') else 'incomplete'


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
    ap.add_argument('--csv', default='bugs_last10.csv,bugs.csv')
    ap.add_argument('--out', default='rq5_bug_results.csv')
    ap.add_argument('--interleaved-csv', default='bugs_last10_interleaved.csv',
                    help='CSV the Daikon array indices refer to (to identify pending tasks)')
    args = ap.parse_args()
    by_project = bugs_by_project(args.csv, None)

    rows_by_index = load_csv_rows(args.interleaved_csv) if Path(args.interleaved_csv).exists() else {}
    queue = queue_states(rows_by_index)
    outs = slurm_out_index()

    rows = []
    for p in sorted(by_project):
        for rank, bid in enumerate(sorted(by_project[p], key=int, reverse=True), 1):
            bug_dir = Path('outputs_usefulness') / f'{p}_{bid}'
            d = daikon_exposes(bug_dir)
            rows.append({
                'project': p, 'bug': bid, 'rank': rank,
                'daikon_status': daikon_status(p, bid, d is not None, queue, outs),
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
