#!/usr/bin/env python3
"""RQ5 (usefulness) metric for one Defects4J bug run of run_usefulness_bug.py.

Reproduces the paper's section 4.5.3 procedure: a genuine "catch" is an
invariant that HELD in Phase A (without_test -- bug-revealing test disabled)
and was FALSIFIED in Phase B (with_test -- bug-revealing test enabled).

Invariant `id`s are randomly regenerated (UUID.randomUUID()) on every run --
see InvariantRegistry -- so they are NOT a valid key to match a Phase A
record to its Phase B counterpart. The stable identity across phases is
(kind, element, expr), the same triple InvariantRegistry.keyOf() itself
uses for dedup. Each phase's registry/outcomes are joined by `id` WITHIN that
phase first, then the two phases are joined by (kind, element, expr).

Usage:
    python3 rq5_check.py <bug_dir>              # e.g. outputs_usefulness/Cli_40
    python3 rq5_check.py --all [outputs_dir]    # every bug dir under outputs_usefulness/

Add --allow-incomplete (anywhere in the args) to report on a bug_dir even
without its RUN_COMPLETE marker -- only for manually inspecting a run still
in progress; never trust those numbers as final (see RunIncompleteError).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


class RunIncompleteError(Exception):
    """Raised when bug_dir has no RUN_COMPLETE marker and SLURM can't
    independently confirm the run is done either.

    out_dir is reused across re-runs of the same bug -- run_usefulness_bug.py
    never clears its old registry/outcomes/log files before starting a fresh
    attempt, it just overwrites them in place. A snapshot taken mid-overwrite
    can still parse as complete, valid-looking JSON even though it's a mix
    of the previous run's data and a handful of freshly-appended records
    from the new attempt in progress -- confirmed directly on Lang-65, where
    this produced a clean "0 true catches" result while a brand new
    checkout/compile for that same bug was already under way.
    """


def _bugs_csv_row_index(bugs_csv: Path, project: str, bug_id: str) -> int | None:
    with open(bugs_csv) as f:
        rows = [line.strip().split(",") for line in f if line.strip()]
    for i, row in enumerate(rows[1:]):  # skip header
        if len(row) >= 2 and row[0] == project and row[1] == bug_id:
            return i
    return None


def _slurm_task_state(bug_dir: Path) -> str | None:
    """Asks SLURM directly for this bug's array task's current status,
    instead of trusting the RUN_COMPLETE marker (which only exists for runs
    launched after that marker was added) or the mtimes/contents of files
    that a still-running or requeued task can be actively overwriting in
    place.

    Returns "RUNNING" (queued/running right now, any job id), the sacct
    State string of the most recently submitted job for that array index
    (e.g. "COMPLETED", "FAILED", "CANCELLED"), or None if this can't be
    determined at all (not on a SLURM node, sacct/squeue missing, bugs.csv
    not found, or no matching accounting record -- e.g. it aged out of
    sacct's history).
    """
    m = re.match(r"^(.+)_(\S+)$", bug_dir.name)
    if not m:
        return None
    project, bug_id = m.group(1), m.group(2)

    bugs_csv = bug_dir.parent.parent / "bugs.csv"
    if not bugs_csv.is_file():
        bugs_csv = Path("bugs.csv")
    if not bugs_csv.is_file():
        return None
    idx = _bugs_csv_row_index(bugs_csv, project, bug_id)
    if idx is None:
        return None

    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        return None
    try:
        squeue_out = subprocess.run(
            ["squeue", "-u", user, "-h", "-o", "%i", "--name=usefulness"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        sacct_out = subprocess.run(
            # sacct defaults to only today's jobs unless -S is given -- without
            # it, a job submitted on an earlier day (the normal case, since
            # this is checked well after a run finishes) silently returns
            # nothing, making every one of its bugs look "undeterminable"
            # even though sacct has the record. -S 1970-01-01 disables that
            # default window entirely.
            ["sacct", "--name=usefulness", "--format=JobID,State", "-X", "-n", "-P",
             "-S", "1970-01-01"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    # A currently-queued/running task for this exact array index (any job
    # id) means a fresh attempt is in flight right now, whatever sacct's
    # history says about an earlier attempt.
    running_indices = {line.split("_")[-1] for line in squeue_out.splitlines() if "_" in line}
    if str(idx) in running_indices:
        return "RUNNING"

    # Among sacct's history for this array index, only the most recently
    # submitted job (highest numeric job id) reflects the CURRENT contents
    # of out_dir -- an old COMPLETED record from a since-superseded
    # submission would otherwise look identical to a real completion.
    best_job_num = -1
    best_state = None
    for line in sacct_out.splitlines():
        parts = line.split("|")
        if len(parts) != 2:
            continue
        jobid, state = parts
        jm = re.match(r"^(\d+)_(\d+)$", jobid)
        if not jm or jm.group(2) != str(idx):
            continue
        job_num = int(jm.group(1))
        if job_num > best_job_num:
            best_job_num = job_num
            best_state = state
    return best_state


def _key(record: dict) -> str:
    return record["kind"] + "|" + record["element"] + "|" + " ".join(record["expr"].split())


_lenient_decoder = json.JSONDecoder(strict=False)


def _iter_json_objects(path: Path):
    """Yields each JSON object from a JSONL-ish file, tolerant of a
    daikonplusplus writer quirk where a source comment (javadoc opener,
    line comment) lands in a field -- e.g. its return-type descriptor
    picking up a trailing comment instead of an actual type -- and gets
    written back with that comment's raw, unescaped newline/control
    characters instead of JSON-escaping them (\\n, \\u0000, etc). That
    breaks both strict JSON parsing (a raw control character inside a
    string is illegal JSON) and naive line-by-line splitting (the literal
    embedded newline splits one record's bytes across two physical
    "lines"). Decoding the whole file positionally with a non-strict
    decoder sidesteps both: raw_decode finds the next complete JSON value
    from wherever the previous one ended regardless of embedded newlines,
    and non-strict mode accepts the otherwise-illegal raw control
    characters as literal string content -- recovering the real record
    daikonplusplus meant to write instead of discarding it.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, end = _lenient_decoder.raw_decode(text, idx)
        yield obj
        idx = end


def _load_verdicts(reg_path: Path, out_path: Path) -> dict[str, str]:
    """Returns {(kind|element|expr) key: verdict} for one phase."""
    id_to_key: dict[str, str] = {}
    for r in _iter_json_objects(reg_path):
        id_to_key[r["id"]] = _key(r)

    key_to_verdict: dict[str, str] = {}
    for o in _iter_json_objects(out_path):
        key = id_to_key.get(o["id"])
        if key:
            key_to_verdict[key] = o["verdict"]
    return key_to_verdict


def compute_rq5(bug_dir: Path, require_complete: bool = True) -> dict:
    """Computes the RQ5 held-A -> falsified-B metric for one bug's output dir.

    Raises FileNotFoundError if the four required jsonl files aren't there
    yet (e.g. called before Phase B has finished), and RunIncompleteError
    if the run can't be confirmed done (see RunIncompleteError) unless
    require_complete=False.

    "Confirmed done" is either the RUN_COMPLETE marker (written by runs
    launched after that marker was added) or, when it's absent -- e.g. a
    bug that finished before this check existed -- SLURM's own accounting
    record for that bug's array task (see _slurm_task_done). Only when
    NEITHER can confirm completion does this raise.
    """
    if require_complete and not (bug_dir / "RUN_COMPLETE").exists():
        state = _slurm_task_state(bug_dir)
        if state == "COMPLETED":
            # Confirmed independently via sacct/squeue -- backfill the
            # marker so future calls take the fast path without re-asking
            # SLURM every time.
            (bug_dir / "RUN_COMPLETE").write_text("backfilled from sacct\n")
        elif state == "RUNNING":
            raise RunIncompleteError(f"{bug_dir}: SLURM shows this bug's task is still running/requeued")
        elif state is not None:
            raise RunIncompleteError(f"{bug_dir}: SLURM shows this bug's task ended in state {state}, not COMPLETED")
        else:
            raise RunIncompleteError(
                f"{bug_dir}/RUN_COMPLETE not found and SLURM state couldn't be determined "
                f"(pass --allow-incomplete to inspect anyway)"
            )

    a = _load_verdicts(
        bug_dir / "daikonpp_registry_without_test.jsonl",
        bug_dir / "daikonpp_outcomes_without_test.jsonl",
    )
    b = _load_verdicts(
        bug_dir / "daikonpp_registry_with_test.jsonl",
        bug_dir / "daikonpp_outcomes_with_test.jsonl",
    )

    held_a = {k for k, v in a.items() if v == "HELD"}
    falsified_a = {k for k, v in a.items() if v == "FALSIFIED"}
    falsified_b = {k for k, v in b.items() if v == "FALSIFIED"}

    true_catches = held_a & falsified_b
    already_broken = falsified_a & falsified_b
    unaccounted = falsified_b - true_catches - already_broken

    return {
        "bug_dir": str(bug_dir),
        "held_in_a": len(held_a),
        "falsified_in_b_raw": len(falsified_b),
        "true_catches": sorted(true_catches),
        "already_broken": len(already_broken),
        "unaccounted": len(unaccounted),
    }


def format_summary(result: dict) -> str:
    lines = [
        f"{result['bug_dir']}:",
        f"  held in Phase A:                       {result['held_in_a']}",
        f"  falsified in Phase B (raw):             {result['falsified_in_b_raw']}",
        f"  TRUE bug-catches (held A -> falsified B): {len(result['true_catches'])}",
        f"  already falsified in A too (not a catch): {result['already_broken']}",
        f"  unaccounted (key mismatch/other):         {result['unaccounted']}",
    ]
    return "\n".join(lines)


def write_summary(bug_dir: Path, result: dict) -> Path:
    summary_path = bug_dir / "daikonpp_rq5_summary.json"
    summary_path.write_text(json.dumps(result, indent=2) + "\n")
    return summary_path


def main():
    args = sys.argv[1:]
    allow_incomplete = "--allow-incomplete" in args
    args = [a for a in args if a != "--allow-incomplete"]
    if not args:
        print(__doc__)
        raise SystemExit(1)

    if args[0] == "--all":
        outputs_dir = Path(args[1]) if len(args) > 1 else Path("outputs_usefulness")
        if not outputs_dir.is_dir():
            print(f"ERROR: {outputs_dir} is not a directory")
            raise SystemExit(1)
        # "_cassettes" (the shared per-project LLM cassette dir) and
        # "batch_logs" (run_usefulness_batch.py's own log directory) aren't
        # bug output dirs -- skip them here instead of letting them show up
        # as unparseable SKIP entries.
        non_bug_dirs = {"batch_logs"}
        bug_dirs = sorted(
            p
            for p in outputs_dir.iterdir()
            if p.is_dir() and not p.name.startswith("_") and p.name not in non_bug_dirs
        )
    else:
        bug_dirs = [Path(args[0])]

    total_bug_dirs = len(bug_dirs)
    finished = 0
    skipped = 0
    total_true_catches = 0

    for bug_dir in bug_dirs:
        try:
            result = compute_rq5(bug_dir, require_complete=not allow_incomplete)
        except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError) as e:
            # A still-running array task keeps appending to its
            # daikonpp_registry_*.jsonl / daikonpp_outcomes_*.jsonl files,
            # so reading them mid-write can catch a half-written last line
            # (JSONDecodeError) or, since out_dir is reused across re-runs
            # of the same bug, valid-but-stale-or-mixed data with no
            # RUN_COMPLETE marker yet (RunIncompleteError), in addition to
            # a file that doesn't exist at all yet (FileNotFoundError).
            # Treat all three as "not ready yet" rather than crashing the
            # whole batch or reporting a number that isn't final.
            print(f"{bug_dir}: SKIP ({e})")
            skipped += 1
            continue
        print(format_summary(result))
        write_summary(bug_dir, result)
        if allow_incomplete and not (bug_dir / "RUN_COMPLETE").exists():
            print("  *** WARNING: RUN_COMPLETE marker missing -- this bug is still "
                  "running or was re-run; the numbers above are NOT final. ***")
        else:
            finished += 1
            total_true_catches += len(result["true_catches"])
        print()

    print("==== TOTAL ====")
    print(f"bug dirs found:        {total_bug_dirs}")
    print(f"finished (counted):    {finished}")
    print(f"skipped (not ready):   {skipped}")
    print(f"total TRUE bug-catches across finished bugs: {total_true_catches}")


if __name__ == "__main__":
    main()
