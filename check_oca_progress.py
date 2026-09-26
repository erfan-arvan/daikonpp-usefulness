#!/usr/bin/env python3
"""Per-project progress check for the Oca (daikonplusplus) side of the RQ5
usefulness experiment -- the Oca-side twin of check_daikon_progress.py.

A bug counts as done once its outputs_usefulness/<PROJECT>_<BUG>/RUN_COMPLETE
marker exists (written by run_usefulness_bug.py on a successful run).

Usage:
    python3 check_oca_progress.py [bugs_last10.csv] [--missing]

--missing also lists the specific bug_ids still outstanding per project.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path.cwd()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_missing = "--missing" in sys.argv[1:]
    csv_path = Path(args[0]) if args else ROOT / "bugs_last10.csv"

    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")

    by_project: dict[str, list[str]] = defaultdict(list)
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            by_project[row["project"].strip()].append(row["bug_id"].strip())

    for project in sorted(by_project):
        bug_ids = by_project[project]
        missing = [
            bid for bid in bug_ids
            if not (ROOT / "outputs_usefulness" / f"{project}_{bid}" / "RUN_COMPLETE").exists()
        ]
        done = len(bug_ids) - len(missing)
        line = f"{project}: {done}/{len(bug_ids)}"
        if show_missing and missing:
            line += f"  MISSING: {missing}"
        print(line)


if __name__ == "__main__":
    main()
