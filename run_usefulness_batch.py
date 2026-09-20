#!/usr/bin/env python3
"""Batch driver for run_usefulness_bug.py over a CSV of (project, bug_id) rows.

CSV format (header required):
    project,bug_id
    Lang,1
    Lang,3
    Math,12
    ...

Usage:
    python3 run_usefulness_batch.py bugs.csv [--skip-existing]
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.cwd()
THIS_DIR = Path(__file__).resolve().parent
BUG_SCRIPT = THIS_DIR / "run_usefulness_bug.py"
LOG_ROOT = ROOT / "outputs_usefulness" / "batch_logs"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_ROOT / "batch_summary.log", "a") as f:
        f.write(line + "\n")


def results_exist(project: str, bug_id: str) -> bool:
    out_dir = ROOT / "outputs_usefulness" / f"{project}_{bug_id}"
    return (out_dir / "with_test.log").exists() and (out_dir / "without_test.log").exists()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    LOG_ROOT.mkdir(parents=True, exist_ok=True)

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")
    if not BUG_SCRIPT.exists():
        sys.exit(f"ERROR: missing {BUG_SCRIPT}")

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    log(f"===== STARTING BATCH RUN ({len(rows)} bugs) =====")

    for i, row in enumerate(rows, 1):
        project = row["project"].strip()
        bug_id = row["bug_id"].strip()
        log("-" * 60)
        log(f"[{i}/{len(rows)}] {project}-{bug_id}")

        if args.skip_existing and results_exist(project, bug_id):
            log(f"SKIP (results exist): {project}-{bug_id}")
            continue

        log_file = LOG_ROOT / f"{project}_{bug_id}.log"
        with open(log_file, "w") as lf:
            proc = subprocess.Popen(
                [sys.executable, str(BUG_SCRIPT), project, bug_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                print(line, end="")
                lf.write(line)
            proc.wait()

        if proc.returncode == 0:
            log(f"SUCCESS: {project}-{bug_id}")
        else:
            log(f"FAILURE: {project}-{bug_id} (exit={proc.returncode})")

    log("===== BATCH RUN COMPLETE =====")


if __name__ == "__main__":
    main()
