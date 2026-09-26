#!/usr/bin/env python3
"""Reorder a bugs CSV so array-index N maps to "the N-th highest-priority
bug across ALL projects", not "all of project A, then all of project B, ...".

submit_daikon.sh maps SLURM_ARRAY_TASK_ID directly to a row of the CSV (one
bug per array task), so with a concurrency cap (`sbatch --array=0-144%K`),
whichever K bugs occupy the FIRST K rows are exactly what runs first. A CSV
ordered "all of project A's bugs, then all of project B's bugs, ..." would
front-load an entire single project (e.g. all 10 of Closure's bugs, among
the largest/slowest in the set) into that first wave instead of spreading
the highest-priority bug of EVERY project across it.

This produces the opposite: rank 1 (the highest bug_id -- "last" bug) of
every project first, in project-name order, then every project's rank 2
bug, and so on, until every project's bugs are exhausted (a project with
fewer bugs than others, e.g. JacksonXml's 5, simply drops out of later
ranks).

Usage:
    python3 gen_bugs_interleaved.py bugs_last10.csv bugs_last10_interleaved.csv
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} <in.csv> <out.csv>")
    in_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])

    by_project: dict[str, list[str]] = defaultdict(list)
    with open(in_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            by_project[row["project"].strip()].append(row["bug_id"].strip())

    # Rank 1 = highest bug_id ("last" bug) within each project.
    for bug_ids in by_project.values():
        bug_ids.sort(key=int, reverse=True)

    projects = sorted(by_project)
    max_rank = max(len(ids) for ids in by_project.values())

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        n = 0
        for rank in range(max_rank):
            for project in projects:
                ids = by_project[project]
                if rank < len(ids):
                    writer.writerow({"project": project, "bug_id": ids[rank]})
                    n += 1

    print(f"wrote {n} rows ({len(projects)} projects, max_rank={max_rank}) -> {out_path}")


if __name__ == "__main__":
    main()
