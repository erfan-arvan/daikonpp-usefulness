#!/usr/bin/env python3
"""Filters a bugs CSV down to the last N (highest bug_id) rows per project.

Usage:
    python3 filter_last_n.py bugs_all.csv 10 > bugs_last10.csv

Output preserves ascending bug_id order within each project (so it still
plays nicely with submit_by_project.sh's shard math, which slices rows in
file order), it just restricts each project to its N highest bug IDs
instead of every bug.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    ap.add_argument("n", type=int)
    args = ap.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")

    by_project: dict[str, list[str]] = defaultdict(list)
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            by_project[row["project"].strip()].append(row["bug_id"].strip())

    print("project,bug_id")
    for project in sorted(by_project):
        ids = sorted(by_project[project], key=int)
        for bug_id in ids[-args.n:]:
            print(f"{project},{bug_id}")


if __name__ == "__main__":
    main()
