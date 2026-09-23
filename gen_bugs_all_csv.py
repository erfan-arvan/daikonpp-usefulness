#!/usr/bin/env python3
"""Generates a bugs CSV listing EVERY valid Defects4J bug ID for a set of
projects, instead of the one bug per project in bugs.csv.

Uses `defects4j bids -p <project>` (the same call lib_defects4j.resolve_bug_ids
makes for the "latest" spec) rather than assuming a contiguous 1..N range --
Defects4J deactivates some bug IDs, so bids is the only authoritative list.

Usage:
    python3 gen_bugs_all_csv.py <project> [<project> ...] > bugs_all.csv

    # e.g. the 8 projects whose one-bug-per-project run already completed:
    python3 gen_bugs_all_csv.py Cli Compress Csv Gson JacksonCore Jsoup JxPath Math > bugs_all.csv

By default this EXCLUDES a project's bug ID that's already in bugs.csv (in
the same directory), since that one has already been run and its results
are already saved -- pass --include-existing to add it back in anyway.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_defects4j import d4j_env  # noqa: E402


def _already_run_ids(bugs_csv: Path) -> dict[str, str]:
    if not bugs_csv.is_file():
        return {}
    out = {}
    with open(bugs_csv) as f:
        next(f, None)  # header
        for line in f:
            line = line.strip()
            if not line:
                continue
            project, bug_id = line.split(",")
            out[project] = bug_id
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("projects", nargs="+")
    ap.add_argument("--include-existing", action="store_true")
    ap.add_argument("--bugs-csv", default="bugs.csv")
    args = ap.parse_args()

    already_run = _already_run_ids(Path(args.bugs_csv))
    env = d4j_env()

    print("project,bug_id")
    for project in args.projects:
        out = subprocess.check_output(["defects4j", "bids", "-p", project], env=env, text=True)
        ids = sorted((l.strip() for l in out.splitlines() if l.strip()), key=int)
        skip = None if args.include_existing else already_run.get(project)
        for bug_id in ids:
            if bug_id == skip:
                continue
            print(f"{project},{bug_id}")


if __name__ == "__main__":
    main()
