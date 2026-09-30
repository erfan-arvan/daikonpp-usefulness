#!/usr/bin/env python3
"""Record the null-run trace of a bug for validate_daikon_fixed.py --null-trace,
without inferring a second invA (the null run only needs its trace).

Checks out <B>b and runs Chicory in a fresh JVM over phase A's saved specs
(the full suite minus the triggering tests, as the checker's --null mode
does). Fails, without a completion marker, unless the triggering tests are
absent and the executed tests equal those of the saved phase-A trace with
the same multiplicities (the checker's --null inventory rule).

Output: <out-root>/<P>_<B>/traceNull.dtrace.gz (+ .tests.json), run_info.json,
logs/, NULL_TRACE_COMPLETE (last, only on success).

Usage:
    DAIKON_JAR=... python3 make_null_trace.py <PROJECT> <BUG_ID> --checker-root R --out-root O
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_daikon_checker_bug as rc  # noqa: E402
from lib_defects4j import capture, d4j_env, kill_current_subprocess, list_test_classes  # noqa: E402
from run_daikon_usefulness_bug import build_full_omit_pattern, checkout, find_junit4_jar  # noqa: E402

COMPLETE = "NULL_TRACE_COMPLETE"
_work: Path | None = None


def _sigterm(signum, frame):
    kill_current_subprocess()
    if _work is not None:
        shutil.rmtree(_work, ignore_errors=True)
    sys.exit(143)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("bug_id")
    ap.add_argument("--checker-root", required=True)
    ap.add_argument("--out-root", required=True)
    args = ap.parse_args()
    jar = os.environ.get("DAIKON_JAR")
    if not jar or not Path(jar).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at daikon.jar (got {jar!r})")
    os.environ["D4J_DEBUG"] = "1"
    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    bug = f"{args.project}_{args.bug_id}"
    nd = Path(args.checker_root).resolve() / "normal" / bug
    out = Path(args.out_root).resolve() / bug
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    marker = out / COMPLETE
    if marker.exists():
        print(f"[INFO] {marker} exists -- already complete")
        return
    ninfo_p, tests_a = nd / "run_info.json", nd / "traceA.dtrace.gz.tests.json"
    if not (nd / rc.COMPLETE).is_file() or not ninfo_p.is_file() or not tests_a.is_file():
        raise rc.InfraError(f"normal-mode checker results missing under {nd}")
    ninfo = json.loads(ninfo_p.read_text())
    triggering = [tuple(t.split("::", 1)) for t in ninfo["triggering"]]
    ran_a = json.loads(tests_a.read_text())["ran"]
    info = {"project": args.project, "bug_id": args.bug_id, "daikon_jar": jar, "specs": ninfo["specs_a"],
            "started": datetime.datetime.now().isoformat()}

    d4j = d4j_env()
    work_root = Path(os.environ.get("DAIKON_WORK_ROOT", root / "defects4j"))
    work_root.mkdir(parents=True, exist_ok=True)
    work = work_root / f"{args.project}-{args.bug_id}b_nulltrace"
    shutil.rmtree(work, ignore_errors=True)
    global _work
    _work = work
    signal.signal(signal.SIGTERM, _sigterm)
    trace = out / "traceNull.dtrace.gz"
    try:
        ran = rc.load_ran(trace)
        if ran is None:
            checkout(args.project, f"{args.bug_id}b", work)
            rc.run_logged(["defects4j", "compile"], logs / "defects4j.log", cwd=work, env=d4j)
            bin_tests = work / capture(["defects4j", "export", "-p", "dir.bin.tests"], cwd=work, env=d4j).strip()
            cp_test = capture(["defects4j", "export", "-p", "cp.test"], cwd=work, env=d4j).strip()
            cp_runner = f"{cp_test}:{find_junit4_jar()}"
            runner = out / "runner-classes"
            runner.mkdir(exist_ok=True)
            rc.run_logged(["javac", "-cp", cp_runner, "-d", str(runner), str(rc.RUNNER_SRC)], logs / "javac_runner.log")
            omit = build_full_omit_pattern(list_test_classes(str(bin_tests)))
            ran = rc.run_chicory(jar, runner, cp_runner, ninfo["pkg_pattern"], omit, trace, work, ninfo["specs_a"],
                                 logs / "chicory_null.log")
        info["verify"] = rc.verify_tests("null trace", ran, triggering, expect_present=False)
        rc.verify_same_inventory(ran_a, ran, info["verify"].setdefault("null_inventory", {}))
        info["finished"] = datetime.datetime.now().isoformat()
        rc.write_atomic(out / "run_info.json", json.dumps(info, indent=1))
        rc.write_atomic(marker, json.dumps({"finished": info["finished"]}) + "\n")
        print(f">>> NULL TRACE {bug}: {trace}")
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        rc.write_atomic(out / "run_info.json", json.dumps(info, indent=1))
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except (rc.InfraError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
