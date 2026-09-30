#!/usr/bin/env python3
"""List the Daikon bugs with at least one FALSIFIED invariant (RQ5) in the
OLD text-diff results (outputs_usefulness/<bug>/daikon_outcomes.jsonl).
These are raw candidates, not validated detections: see
summarize_daikon_strict_batch.py for checker / fixed-validated / strict
counts. Projects in DROPPED_PROJECTS are left out.

Prints one section for the latest 5 bugs of each project and one for the
latest 10: each exposed bug with its number of FALSIFIED invariants and the
invariants themselves, then a per-bug count table and totals.

Usage:
    python3 check_daikon_catches.py [--last N] [--no-invariants] [--csv bugs_last10.csv,bugs.csv]

--last N prints only that scope; --no-invariants omits the invariant lines.
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


# Projects removed from the dataset: none of their latest-5 bugs can finish
# the Daikon checker/fixed/strict pipeline within 72 h. Every stats script
# leaves them out (pass exclude=() to include them).
DROPPED_PROJECTS = ('Csv', 'JacksonDatabind', 'JacksonXml')


def bugs_by_project(csv_paths, last, exclude=DROPPED_PROJECTS):
    """{project: [bug ids]} from one or more comma-separated CSV paths.

    bugs_last10.csv leaves out each project's LATEST bug (gen_bugs_all_csv.py
    skipped the one already in bugs.csv, the original one-bug-per-project
    run), so the default reads both files. Only projects in the first CSV
    are kept; missing files are skipped. With `last`, each project keeps its
    `last` highest bug ids. Projects in `exclude` are left out.
    """
    paths = [c.strip() for c in csv_paths.split(',') if c.strip()]
    by_project = defaultdict(set)
    projects = None
    for i, path in enumerate(paths):
        if not Path(path).exists():
            continue
        with open(path, newline='') as f:
            for row in csv.DictReader(f):
                p, b = row['project'].strip(), row['bug_id'].strip()
                if i > 0 and projects is not None and p not in projects:
                    continue
                if b.isdigit():
                    by_project[p].add(b)
        if i == 0:
            projects = set(by_project)
    result = {}
    for p, ids in by_project.items():
        if p in exclude:
            continue
        ids = sorted(ids, key=int, reverse=True)
        result[p] = ids[:last] if last else ids
    return result


def falsified_records(p, bid):
    """FALSIFIED records of one bug, or None if Daikon has no result."""
    out_file = Path('outputs_usefulness') / f'{p}_{bid}' / 'daikon_outcomes.jsonl'
    if not out_file.exists():
        return None
    recs = []
    with open(out_file) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                if rec['verdict'] == 'FALSIFIED':
                    recs.append(rec)
    return recs


def report(by_project, label, list_invariants):
    print(f'######## {label} (old text-diff; excluded projects: {", ".join(DROPPED_PROJECTS)}) ########')
    checked, exposed = 0, []
    for p in sorted(by_project):
        for bid in by_project[p]:
            recs = falsified_records(p, bid)
            if recs is None:
                continue
            checked += 1
            if recs:
                exposed.append((f'{p}_{bid}', len(recs)))
                print(f'{p}_{bid}: {len(recs)} FALSIFIED invariant(s)')
                if list_invariants:
                    for rec in recs:
                        print(f"    ppt={rec['ppt']}  inv={rec['invariant']}")
    print()
    print(f'---- falsified invariants per exposed bug ({label}) ----')
    for name, n in exposed:
        print(f'  {name:<24} {n}')
    print(f'==== TOTAL ({label}) ====')
    print(f'bugs in scope:                                {sum(len(v) for v in by_project.values())}')
    print(f'bugs checked (daikon_outcomes.jsonl present): {checked}')
    print(f'bugs with at least one FALSIFIED invariant:   {len(exposed)}')
    print(f'total FALSIFIED invariant records:            {sum(n for _, n in exposed)}')
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=None,
                    help='only this scope (default: a section for latest 5 and one for latest 10)')
    ap.add_argument('--csv', default='bugs_last10.csv,bugs.csv')
    ap.add_argument('--no-invariants', action='store_true',
                    help='print only the per-bug counts, not every falsified invariant')
    args = ap.parse_args()
    for n in ([args.last] if args.last else [5, 10]):
        report(bugs_by_project(args.csv, n), f'LATEST {n} BUGS PER PROJECT', not args.no_invariants)


if __name__ == '__main__':
    main()
