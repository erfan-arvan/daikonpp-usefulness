#!/usr/bin/env python3
"""Batch driver for run_daikon_usefulness_bug.py over a CSV of (project, bug_id)
rows -- the Daikon-side twin of run_usefulness_batch.py. Uses the SAME csv
(bugs_last10.csv, matching the Oca run's actual scope) as the Oca batch
driver so both tools are compared on identical bugs.

Runs every row SEQUENTIALLY in this one process -- intended to be the unit
of work for one SLURM task in a per-PROJECT array (see submit_daikon.sh),
matching the Oca side's per-project batching (see run_usefulness_batch.py
for the full rationale on why per-project rather than per-bug).

Usage:
    DAIKON_JAR=/path/to/daikon.jar python3 run_daikon_usefulness_batch.py bugs.csv [--skip-existing] [--project NAME] [--shard I/N]

--project NAME restricts the run to just that project's rows in csv_file.

--shard I/N (0 <= I < N) further restricts to a contiguous 1/N slice of
those rows (in csv_file order), same semantics as run_usefulness_batch.py's
--shard. Each bug here does its own independent `defects4j checkout` into
its own bug-specific work dir, so unlike the Oca side there's no shared
build state or cassette dir for shards of the same project to race on.
"""
from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.cwd()
THIS_DIR = Path(__file__).resolve().parent
BUG_SCRIPT = THIS_DIR / "run_daikon_usefulness_bug.py"
LOG_ROOT = ROOT / "outputs_usefulness" / "batch_logs_daikon"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_ROOT / "batch_summary.log", "a") as f:
        f.write(line + "\n")


def results_exist(project: str, bug_id: str) -> bool:
    out_dir = ROOT / "outputs_usefulness" / f"{project}_{bug_id}"
    return (out_dir / "daikon_outcomes.jsonl").exists()


# Set to the currently-running bug subprocess, read by _handle_sigterm.
# submit_daikon.sh's `timeout --signal=TERM ... 71h` sends SIGTERM only to
# THIS process (its direct child), not to the grandchild bug subprocess --
# without forwarding it, this driver just dies and the bug subprocess is
# orphaned, left running with no chance to clean up after itself (see the
# matching SIGTERM handler in run_daikon_usefulness_bug.py).
_current_proc: subprocess.Popen | None = None


def _handle_sigterm(signum, frame):
    if _current_proc is not None and _current_proc.poll() is None:
        print("[INFO] caught SIGTERM -- forwarding to current bug subprocess for cleanup", flush=True)
        _current_proc.send_signal(signal.SIGTERM)
        try:
            _current_proc.wait(timeout=240)
        except subprocess.TimeoutExpired:
            print("[INFO] bug subprocess did not exit within 240s of SIGTERM -- killing it", flush=True)
            _current_proc.kill()
    sys.exit(143)


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

    if not os.environ.get("DAIKON_JAR"):
        sys.exit("ERROR: DAIKON_JAR must be set before running this batch driver")

    shard_index = shard_count = None
    if args.shard:
        try:
            shard_index_s, shard_count_s = args.shard.split("/")
            shard_index, shard_count = int(shard_index_s), int(shard_count_s)
            assert 0 <= shard_index < shard_count
        except (ValueError, AssertionError):
            sys.exit(f"ERROR: --shard must be I/N with 0 <= I < N, got {args.shard!r}")

    signal.signal(signal.SIGTERM, _handle_sigterm)

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

    log(f"===== STARTING DAIKON BATCH RUN ({len(rows)} bugs"
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
            global _current_proc
            proc = subprocess.Popen(
                [sys.executable, str(BUG_SCRIPT), project, bug_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            _current_proc = proc
            for line in proc.stdout:
                print(line, end="")
                lf.write(line)
            proc.wait()
            _current_proc = None

        if proc.returncode == 0:
            log(f"SUCCESS: {project}-{bug_id}")
        else:
            log(f"FAILURE: {project}-{bug_id} (exit={proc.returncode})")

    log("===== DAIKON BATCH RUN COMPLETE =====")


if __name__ == "__main__":
    main()
