#!/usr/bin/env python3
"""Batch driver for run_usefulness_bug.py over a CSV of (project, bug_id) rows.

Runs every row SEQUENTIALLY in this one process -- intended to be the unit
of work for one SLURM task in a per-PROJECT array (see submit_by_project.sh),
so that different bug IDs of the SAME project never run concurrently against
the same shared LLM cassette dir or the same project's build artifacts,
while different PROJECTS still run in true parallel as separate array tasks.

CSV format (header required):
    project,bug_id
    Lang,1
    Lang,3
    Math,12
    ...

Usage:
    python3 run_usefulness_batch.py bugs.csv [--skip-existing] [--project NAME] [--shard I/N]

--project NAME restricts the run to just that project's rows in csv_file,
so one shared bugs_all.csv can drive every project's per-task batch run
without needing a separate CSV file per project.

--shard I/N (0 <= I < N) further restricts to a contiguous 1/N slice of
those rows (in csv_file order) -- e.g. --shard 0/2 for the first half,
--shard 1/2 for the second -- so a project with too many bugs to run
sequentially in one job's walltime can be split across N separate SLURM
array tasks. Splitting a project this way means its shards DO run
concurrently and DO write to the same shared cassette dir -- daikonplusplus's
own Cassette.write() writes each entry as its own separate file keyed by a
prompt hash (see llm/Cassette.java), so two DIFFERENT prompts never
collide, but it is a plain (non-atomic) file write, so two shards
resolving the EXACT SAME prompt (plausible here, since cassette sharing
relies on many prompts being identical across a project's bug versions)
at the same moment could race on that one file. Narrower and rarer than
the checkout/build races a shared DPP_DIR caused, so accepted as a
tradeoff rather than engineered around.
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
    # RUN_COMPLETE is only written once both phases AND the in-process RQ5
    # analysis have genuinely finished (see run_usefulness_bug.py) -- a
    # weaker check like "with_test.log exists" can be true for a bug that
    # started a phase and never finished it.
    out_dir = ROOT / "outputs_usefulness" / f"{project}_{bug_id}"
    return (out_dir / "RUN_COMPLETE").exists()


def main():
    # This script's own stdout is redirected to a file under SLURM, not a
    # TTY, so Python defaults to fully buffered stdout -- see the identical
    # fix and full explanation in run_usefulness_bug.py's main().
    sys.stdout.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--project", default=None, help="restrict to this project's rows only")
    ap.add_argument("--shard", default=None, help="I/N: run only the I-th of N contiguous slices")
    args = ap.parse_args()

    shard_index = shard_count = None
    if args.shard:
        try:
            shard_index_s, shard_count_s = args.shard.split("/")
            shard_index, shard_count = int(shard_index_s), int(shard_count_s)
            assert 0 <= shard_index < shard_count
        except (ValueError, AssertionError):
            sys.exit(f"ERROR: --shard must be I/N with 0 <= I < N, got {args.shard!r}")

    LOG_ROOT.mkdir(parents=True, exist_ok=True)

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")
    if not BUG_SCRIPT.exists():
        sys.exit(f"ERROR: missing {BUG_SCRIPT}")

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    if args.project:
        rows = [r for r in rows if r["project"].strip() == args.project]
        if not rows:
            sys.exit(f"ERROR: no rows for project={args.project!r} in {csv_path}")

    if shard_count:
        total = len(rows)
        chunk = -(-total // shard_count)  # ceil division
        start = shard_index * chunk
        end = min(start + chunk, total)
        rows = rows[start:end]
        if not rows:
            sys.exit(f"ERROR: shard {args.shard} of {total} rows is empty")

    log(f"===== STARTING BATCH RUN ({len(rows)} bugs"
        f"{f', project={args.project}' if args.project else ''}"
        f"{f', shard={args.shard}' if args.shard else ''}) =====")

    for i, row in enumerate(rows, 1):
        project = row["project"].strip()
        bug_id = row["bug_id"].strip()
        log("-" * 60)
        log(f"[{i}/{len(rows)}] {project}-{bug_id}")

        if args.skip_existing and results_exist(project, bug_id):
            log(f"SKIP (results exist): {project}-{bug_id}")
            continue

        log_file = LOG_ROOT / f"{project}_{bug_id}.log"
        with open(log_file, "w", buffering=1) as lf:
            proc = subprocess.Popen(
                [sys.executable, str(BUG_SCRIPT), project, bug_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                print(line, end="", flush=True)
                lf.write(line)
            proc.wait()

        if proc.returncode == 0:
            log(f"SUCCESS: {project}-{bug_id}")
        else:
            log(f"FAILURE: {project}-{bug_id} (exit={proc.returncode})")

    log("===== BATCH RUN COMPLETE =====")


if __name__ == "__main__":
    main()
