#!/usr/bin/env python3
"""Redo ONLY the checker step of run_daikon_checker_bug.py on a finished (or
checker-failed) run directory: the saved traceA / trace B, invA.inv.gz and
invariantsA.txt are used as they are. No Chicory, no Daikon inference, no
PrintInvariants, no defects4j.

Steps, in <out-root>/<normal|null>/<PROJECT>_<BUG>/:
  1. Require traceA, the phase-B trace, their .tests.json, invA.inv.gz and
     invariantsA.txt; fail otherwise.
  2. Re-verify from the saved .tests.json: triggering tests absent from A,
     present in normal B / absent in null B, and (null) B's executed tests
     equal A's with the same multiplicities.
  3. Remove CHECKER_COMPLETE; move the previous checker outputs to
     superseded_<timestamp>/.
  4. Check invA against trace B and against traceA (baseline_A/), write
     run_info.json and, last, CHECKER_COMPLETE.
  5. Fail if any input file changed size or mtime during the run.

Usage:
    DAIKON_JAR=/path/to/daikon.jar python3 recheck_daikon_checker.py <PROJECT> <BUG_ID> [--null]
        [--out-root outputs_daikon_checker] [--max-violations 5]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_daikon_checker_bug import (  # noqa: E402
    COMPLETE,
    JAVA_XMX,
    DAIKON_CONFIG,
    InfraError,
    is_valid_gzip,
    run_checks,
    tests_file,
    verify_same_inventory,
    verify_tests,
    write_atomic,
)

OLD_OUTPUTS = ("checker_records.jsonl", "checker_summary.json", "stock_checker_verbose.txt",
               "daikon_checker_outcomes.jsonl", "run_info.json", "baseline_A")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("bug_id")
    ap.add_argument("--null", action="store_true")
    ap.add_argument("--out-root", default=None, help="default: $ROOT/outputs_daikon_checker")
    ap.add_argument("--max-violations", type=int, default=5)
    args = ap.parse_args()

    daikon_jar = os.environ.get("DAIKON_JAR")
    if not daikon_jar or not Path(daikon_jar).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at a built daikon.jar (got: {daikon_jar!r})")
    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    mode = "null" if args.null else "normal"
    out_root = Path(args.out_root).resolve() if args.out_root else root / "outputs_daikon_checker"
    out_dir = out_root / mode / f"{args.project}_{args.bug_id}"
    logs = out_dir / "logs"

    trace_a = out_dir / "traceA.dtrace.gz"
    trace_b = out_dir / ("traceFull.dtrace.gz" if mode == "normal" else "traceNull.dtrace.gz")
    inv_a = out_dir / "invA.inv.gz"
    invariants_a = out_dir / "invariantsA.txt"
    inputs = [trace_a, trace_b, tests_file(trace_a), tests_file(trace_b), inv_a, invariants_a]
    missing = [str(p) for p in inputs if not p.is_file()]
    if missing:
        raise InfraError(f"saved inputs missing (run run_daikon_checker_bug.py first): {missing}")
    for p in (trace_a, trace_b, inv_a):
        if not is_valid_gzip(p):
            raise InfraError(f"{p} is not a valid gzip stream")
    stamp_before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in inputs}

    prev_info = {}
    if (out_dir / "run_info.json").is_file():
        prev_info = json.loads((out_dir / "run_info.json").read_text())
    triggering_s = prev_info.get("triggering")
    if not triggering_s:
        raise InfraError(f"{out_dir / 'run_info.json'} has no triggering tests; cannot re-verify phases")
    triggering = [tuple(t.split("::", 1)) for t in triggering_s]

    info = {k: prev_info[k] for k in ("project", "bug_id", "mode", "pkg_pattern", "triggering", "specs_a", "specs_b")
            if k in prev_info}
    info.update({"project": args.project, "bug_id": args.bug_id, "mode": mode, "daikon_jar": daikon_jar,
                 "java_xmx": JAVA_XMX, "daikon_config": DAIKON_CONFIG,
                 "rechecked": datetime.datetime.now().isoformat(),
                 "recheck_note": "checker step only; traces, invA and invariantsA.txt reused unchanged"})

    marker = out_dir / COMPLETE
    marker.unlink(missing_ok=True)
    try:
        ran_a = json.loads(tests_file(trace_a).read_text())["ran"]
        ran_b = json.loads(tests_file(trace_b).read_text())["ran"]
        info["verify_a"] = verify_tests("phase A", ran_a, triggering, expect_present=False)
        info["verify_b"] = verify_tests(f"phase B ({mode})", ran_b, triggering, expect_present=(mode == "normal"))
        if mode == "null":
            verify_same_inventory(ran_a, ran_b, info["verify_b"].setdefault("null_inventory", {}))

        sup = out_dir / f"superseded_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        for name in OLD_OUTPUTS:
            if (out_dir / name).exists():
                sup.mkdir(exist_ok=True)
                shutil.move(str(out_dir / name), str(sup / name))
        if sup.exists():
            print(f"[INFO] previous checker outputs moved to {sup}")

        run_checks(daikon_jar, out_dir, inv_a, invariants_a, trace_a, trace_b, logs, info, args.max_violations)

        stamp_after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in inputs}
        changed = [p for p in stamp_before if stamp_before[p] != stamp_after[p]]
        if changed:
            marker.unlink(missing_ok=True)
            raise InfraError(f"input files changed during the recheck: {changed}")
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        write_atomic(out_dir / "run_info.json", json.dumps(info, indent=1))
        raise


if __name__ == "__main__":
    try:
        main()
    except (InfraError, subprocess.CalledProcessError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
