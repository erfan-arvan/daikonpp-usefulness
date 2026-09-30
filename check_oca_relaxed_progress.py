#!/usr/bin/env python3
"""Progress of the relaxed-Oca runs (submit_oca_relaxed.sh) over the latest
5 / latest 10 bugs of EVERY dataset project (bugs_last10.csv + bugs.csv,
without check_daikon_catches.DROPPED_PROJECTS) -- projects that were never
submitted show up as not started.

done = oca_status.py confirms the run finished (the marker
run_usefulness_bug.py writes itself, or its oca-relaxed.*.out job log);
unverified, failed and incomplete (running or killed) runs are listed
separately by bug id.

Usage:
    python3 check_oca_relaxed_progress.py [--csv bugs_last10.csv,bugs.csv] [--include-dropped]
"""
import argparse
from pathlib import Path

from check_daikon_catches import DROPPED_PROJECTS
from check_oca_progress import report

OUT = Path('outputs_usefulness_relaxed')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default='bugs_last10.csv,bugs.csv')
    ap.add_argument('--include-dropped', action='store_true', help=f'also include {", ".join(DROPPED_PROJECTS)}')
    args = ap.parse_args()
    report(OUT, args.csv, show_missing=True, exclude=() if args.include_dropped else DROPPED_PROJECTS)


if __name__ == '__main__':
    main()
