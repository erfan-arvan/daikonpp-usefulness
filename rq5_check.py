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
import sys
from pathlib import Path


class RunIncompleteError(Exception):
    """Raised when bug_dir has no RUN_COMPLETE marker yet.

    out_dir is reused across re-runs of the same bug -- run_usefulness_bug.py
    never clears its old registry/outcomes/log files before starting a fresh
    attempt, it just overwrites them in place. A snapshot taken mid-overwrite
    can still parse as complete, valid-looking JSON even though it's a mix
    of the previous run's data and a handful of freshly-appended records
    from the new attempt in progress -- confirmed directly on Lang-65, where
    this produced a clean "0 true catches" result while a brand new
    checkout/compile for that same bug was already under way. Only trust a
    bug_dir once run_usefulness_bug.py has written RUN_COMPLETE, which it
    does last, after both phases and its own RQ5 write have finished.
    """


def _key(record: dict) -> str:
    return record["kind"] + "|" + record["element"] + "|" + " ".join(record["expr"].split())


def _load_verdicts(reg_path: Path, out_path: Path) -> dict[str, str]:
    """Returns {(kind|element|expr) key: verdict} for one phase."""
    id_to_key: dict[str, str] = {}
    with open(reg_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            id_to_key[r["id"]] = _key(r)

    key_to_verdict: dict[str, str] = {}
    with open(out_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            key = id_to_key.get(o["id"])
            if key:
                key_to_verdict[key] = o["verdict"]
    return key_to_verdict


def compute_rq5(bug_dir: Path, require_complete: bool = True) -> dict:
    """Computes the RQ5 held-A -> falsified-B metric for one bug's output dir.

    Raises FileNotFoundError if the four required jsonl files aren't there
    yet (e.g. called before Phase B has finished), and RunIncompleteError
    if bug_dir has no RUN_COMPLETE marker (see RunIncompleteError) unless
    require_complete=False.
    """
    if require_complete and not (bug_dir / "RUN_COMPLETE").exists():
        raise RunIncompleteError(f"{bug_dir}/RUN_COMPLETE not found")

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
        bug_dirs = sorted(p for p in outputs_dir.iterdir() if p.is_dir())
    else:
        bug_dirs = [Path(args[0])]

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
            continue
        print(format_summary(result))
        write_summary(bug_dir, result)
        if allow_incomplete and not (bug_dir / "RUN_COMPLETE").exists():
            print("  *** WARNING: RUN_COMPLETE marker missing -- this bug is still "
                  "running or was re-run; the numbers above are NOT final. ***")
        print()


if __name__ == "__main__":
    main()
