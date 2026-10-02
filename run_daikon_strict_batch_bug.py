#!/usr/bin/env python3
"""Run one bug through the whole Daikon detection pipeline used in the pilot,
resuming from completed stages, and clean its traces when everything is done.

Stages (each skips itself if its completion marker exists):
  1. checker    run_daikon_checker_bug.py (normal mode): traceA, invA (the
                frozen candidates), invariantsA.txt, traceFull, verdicts,
                baseline (invA vs traceA)                 -> CHECKER_COMPLETE
  2. nulltrace  make_null_trace.py: phase A's specs again in a fresh JVM,
                same executed tests and multiplicities    -> NULL_TRACE_COMPLETE
  3. fixed      validate_daikon_fixed.py --null-trace: baseline / null /
                buggy / fixed-full-suite checks           -> VALIDATION_COMPLETE
  4. strict     validate_daikon_strict.py: marked buggy and fixed full-suite
                runs, triggering-test attribution         -> STRICT_COMPLETE
Stage outputs go under --work-root (e.g. /scratch). A bug is strictly
detected iff stage 4 reports >= 1 strict hit.

Before any work, free space is checked on the work root, DAIKON_WORK_ROOT and
the results root; if it is below the bug's requirement the bug is DEFERRED
(status recorded, exit code 75) and nothing is started.

With --cleanup-traces, after all four stages completed successfully: every
generated trace of the bug (*.dtrace.gz, including mapped and window-only
copies, and partial files) is deleted, then the remaining results (invA.inv.gz,
candidate lists, verdicts, evidence, logs, markers) are copied to
--results-root (same layout). Failed or incomplete runs keep their traces.
Completed and cleaned bugs are recognized from their status and markers and
never re-run.

Status: <results-root>/status/<P>_<B>.json, state in
{running, deferred, failed, completed}.

--three-step: the paper's procedure, three Chicory traces per bug instead of
six: (1) the buggy suite without the triggering tests (traceA -> invA),
(2) the triggering tests only on the buggy version (traceTrig): candidates
they falsify, (3) the fixed version's full suite: those candidates must hold
there. Stages: checker (--trigger-only), fixed (--no-null); no null trace, no
strict stage. A bug is detected iff >= 1 candidate is validated in stage 3.
--seed-root: a 6-step work root whose finished traceA / invA of the same bug
are hard-linked (copied if linking fails) into this run instead of re-traced.

Usage:
    DAIKON_JAR=... python3 run_daikon_strict_batch_bug.py <PROJECT> <BUG_ID> --work-root W --results-root R --cleanup-traces
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

THIS = Path(__file__).resolve().parent
DEFERRED_RC = 75
STAGES = ("checker", "nulltrace", "fixed", "strict")
STAGES_3STEP = ("checker", "fixed")
# Finished phase-A outputs a 3-step run can take over from a 6-step run.
SEED_FILES = ("traceA.dtrace.gz", "traceA.dtrace.gz.tests.json", "invA.inv.gz", "invA.inv.gz.info", "invariantsA.txt")
MARKERS = {"checker": "CHECKER_COMPLETE", "nulltrace": "NULL_TRACE_COMPLETE",
           "fixed": "VALIDATION_COMPLETE", "strict": "STRICT_COMPLETE"}
# Free GB required on the trace filesystems before a bug starts (all of its
# traces can coexist until cleanup). STRICT_MIN_FREE_GB overrides.
MIN_FREE_GB = {"Cli": 20, "Math": 40, "Codec": 40, "Gson": 150, "Csv": 250, "Collections": 300,
               "JacksonDatabind": 500, "JacksonXml": 500}
DEFAULT_MIN_FREE_GB = 300
RESULTS_MIN_FREE_GB = 5
# Same Daikon configuration as the project's original runs.
EXTRA_CONFIG = {"Gson": "daikon.inv.ternary.threeScalar.LinearTernary.enabled=false,"
                        "daikon.inv.ternary.threeScalar.LinearTernaryFloat.enabled=false"}


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def stage_dirs(root: Path, bug: str) -> dict[str, Path]:
    return {"checker": root / "checker" / "normal" / bug, "nulltrace": root / "nulltrace" / bug,
            "fixed": root / "fixed_validation" / bug, "strict": root / "strict" / bug}


def write_status(results: Path, bug: str, **kw):
    p = results / "status" / f"{bug}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(p.read_text()) if p.is_file() else {}
    old.update(kw, bug=bug, updated=now())
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(json.dumps(old, indent=1))
    tmp.replace(p)
    return old


def free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 1e9


def checker_new_format(d: Path) -> bool:
    try:
        return "requires_investigation" in json.loads((d / "run_info.json").read_text()).get("baseline_A", {})
    except (OSError, ValueError):
        return False


def counts(dirs: dict[str, Path], root: Path, bug: str) -> dict:
    out = {}
    old = root / "outputs_usefulness" / bug / "daikon_outcomes.jsonl"
    if old.is_file():
        out["old_falsified"] = sum('"FALSIFIED"' in l for l in old.read_text().splitlines())
    try:
        ci = json.loads((dirs["checker"] / "run_info.json").read_text())
        out["checker_falsified"] = ci["counts"]["FALSIFIED"]
        out["checker_candidates"] = ci["candidates"]
        out["baseline_falsified"] = ci["baseline_A"]["counts"]["FALSIFIED"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        vi = json.loads((dirs["fixed"] / "run_info.json").read_text())
        out["fixed_validated"] = vi["validated_hits"]
        out["fixed_checker_errors"] = vi.get("checker_errors") or {}
    except (OSError, ValueError, KeyError):
        pass
    try:
        si = json.loads((dirs["strict"] / "run_info.json").read_text())
        out["strict"] = si["strict_hits"]
        out["strict_categories"] = si.get("categories", {})
        out["strict_checker_errors"] = si.get("checker_errors") or {}
        out["triggering_without_window"] = {v: si[f"windows_{v}"]["tests_without_ok_window"] for v in ("Buggy", "Fixed")}
    except (OSError, ValueError, KeyError):
        pass
    return out


def seed_phase_a(src: Path, dst: Path) -> list[str]:
    """Links src's finished traceA (trace + tests list) and, if complete,
    invA (inv + marker + printed text) into dst. Never overwrites."""
    if not ((src / "traceA.dtrace.gz").is_file() and (src / "traceA.dtrace.gz.tests.json").is_file()):
        return []
    names = list(SEED_FILES[:2])
    if all((src / n).is_file() for n in SEED_FILES[2:]):
        names += SEED_FILES[2:]
    dst.mkdir(parents=True, exist_ok=True)
    done = []
    for n in names:
        if (dst / n).exists():
            continue
        try:
            os.link(src / n, dst / n)
        except OSError:
            shutil.copy2(src / n, dst / n)
        done.append(n)
    return done


def cleanup_traces(dirs: dict[str, Path]) -> dict:
    removed, freed = [], 0
    for d in dirs.values():
        for p in sorted(d.rglob("*")):
            if p.is_file() and (p.name.endswith(".dtrace.gz") or p.name.startswith((".partial-", ".chicory-"))):
                freed += p.stat().st_size
                p.unlink()
                removed.append(str(p))
    for d in dirs.values():
        (d / "TRACES_CLEANED").write_text(json.dumps({"when": now(), "removed": [r for r in removed if r.startswith(str(d))]}, indent=1))
    return {"removed": len(removed), "freed_gb": round(freed / 1e9, 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("bug_id")
    ap.add_argument("--work-root", required=True, help="stage outputs and traces (e.g. on /scratch)")
    ap.add_argument("--results-root", required=True, help="where completed, cleaned results are kept (e.g. on /project)")
    ap.add_argument("--cleanup-traces", action="store_true", help="delete the bug's traces after all stages succeed")
    ap.add_argument("--min-free-gb", type=float, default=None)
    ap.add_argument("--three-step", action="store_true", help="the paper's 3-step procedure (see module doc)")
    ap.add_argument("--seed-root", default=None, help="6-step work root to take a finished traceA/invA from")
    args = ap.parse_args()
    stages = STAGES_3STEP if args.three_step else STAGES

    if not os.environ.get("DAIKON_JAR") or not Path(os.environ["DAIKON_JAR"]).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at daikon.jar (got {os.environ.get('DAIKON_JAR')!r})")
    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    work, results = Path(args.work_root).resolve(), Path(args.results_root).resolve()
    bug = f"{args.project}_{args.bug_id}"
    wd, rd = stage_dirs(work, bug), stage_dirs(results, bug)

    st = results / "status" / f"{bug}.json"
    prev = json.loads(st.read_text()) if st.is_file() else {}
    if prev.get("state") == "completed" and all((rd[s] / MARKERS[s]).is_file() for s in stages):
        print(f"[INFO] {bug}: completed{' and cleaned' if prev.get('traces_cleaned') else ''} -- nothing to do")
        return 0

    done = {s: (wd[s] / MARKERS[s]).is_file() for s in stages}
    env = dict(os.environ)
    if args.project in EXTRA_CONFIG and not env.get("DAIKON_EXTRA_CONFIG"):
        env["DAIKON_EXTRA_CONFIG"] = EXTRA_CONFIG[args.project]

    if not all(done.values()):
        need = args.min_free_gb or float(os.environ.get("STRICT_MIN_FREE_GB") or MIN_FREE_GB.get(args.project, DEFAULT_MIN_FREE_GB))
        trace_fs = [work] + ([Path(os.environ["DAIKON_WORK_ROOT"])] if os.environ.get("DAIKON_WORK_ROOT") else [])
        free = {str(p): round(free_gb(p), 1) for p in trace_fs}
        res_free = round(free_gb(results), 1)
        if min(free.values()) < need or res_free < RESULTS_MIN_FREE_GB:
            write_status(results, bug, state="deferred", reason="insufficient disk space",
                         free_gb=free, results_free_gb=res_free, required_gb=need, stages_done=done)
            print(f">>> DEFERRED {bug}: free {free} GB (results {res_free} GB), need {need} GB")
            return DEFERRED_RC
        print(f"[INFO] {bug}: free {free} GB >= {need} GB required; stages done: {done}")

    if args.three_step and args.seed_root and not done["checker"]:
        seeded = seed_phase_a(stage_dirs(Path(args.seed_root).resolve(), bug)["checker"], wd["checker"])
        if seeded:
            print(f"[INFO] {bug}: reusing finished phase-A files from {args.seed_root}: {seeded}")

    write_status(results, bug, state="running", started=prev.get("started") or now(), stages_done=done,
                 pipeline="3step" if args.three_step else "6step",
                 slurm_job=os.environ.get("SLURM_ARRAY_JOB_ID", "") + "_" + os.environ.get("SLURM_ARRAY_TASK_ID", ""),
                 daikon_jar=os.environ["DAIKON_JAR"], extra_config=env.get("DAIKON_EXTRA_CONFIG", ""))
    py = sys.executable
    ck, nt, fv, sv = work / "checker", work / "nulltrace", work / "fixed_validation", work / "strict"
    cmds = {
        "checker": [py, THIS / "run_daikon_checker_bug.py", args.project, args.bug_id, "--out-root", ck,
                    *(["--trigger-only"] if args.three_step else [])],
        "nulltrace": [py, THIS / "make_null_trace.py", args.project, args.bug_id, "--checker-root", ck, "--out-root", nt],
        "fixed": [py, THIS / "validate_daikon_fixed.py", args.project, args.bug_id, "--checker-root", ck, "--out-root", fv,
                  *(["--no-null"] if args.three_step else ["--null-trace", nt / bug / "traceNull.dtrace.gz"])],
        "strict": [py, THIS / "validate_daikon_strict.py", args.project, args.bug_id, "--checker-root", ck,
                   "--validation-root", fv, "--out-root", sv],
    }
    for s in stages:
        if (wd[s] / MARKERS[s]).is_file():
            if s == "checker" and not checker_new_format(wd[s]):
                cmd = [py, THIS / "recheck_daikon_checker.py", args.project, args.bug_id, "--out-root", ck]
            else:
                print(f"[INFO] {bug}: stage {s} already complete")
                continue
        else:
            cmd = cmds[s]
        print(f">>> {bug}: stage {s}: {' '.join(map(str, cmd))}", flush=True)
        rc = subprocess.run([str(c) for c in cmd], env=env).returncode
        if rc != 0 or not (wd[s] / MARKERS[s]).is_file():
            err = None
            try:
                err = json.loads((wd[s] / "run_info.json").read_text()).get("error")
            except (OSError, ValueError):
                pass
            write_status(results, bug, state="failed", failed_stage=s, exit_code=rc, error=err,
                         stages_done={x: (wd[x] / MARKERS[x]).is_file() for x in stages})
            print(f">>> FAILED {bug}: stage {s} (rc={rc}); traces kept for resuming")
            return rc or 1
        write_status(results, bug, stages_done={x: (wd[x] / MARKERS[x]).is_file() for x in stages})

    c = counts(wd, root, bug)
    if args.three_step:
        c["detected"] = (c.get("fixed_validated") or 0) > 0
    cleaned = None
    if args.cleanup_traces:
        cleaned = cleanup_traces({s: wd[s] for s in stages})
        print(f"[INFO] {bug}: traces deleted: {cleaned}")
        if work != results:
            for s in stages:
                shutil.copytree(wd[s], rd[s], dirs_exist_ok=True)
                if not (rd[s] / MARKERS[s]).is_file():
                    write_status(results, bug, state="failed", failed_stage="archive", error=f"copy of {s} incomplete")
                    return 1
            for s in stages:
                shutil.rmtree(wd[s], ignore_errors=True)
    elif work != results:
        print(f"[INFO] {bug}: traces kept (no --cleanup-traces); results stay under {work}")
    write_status(results, bug, state="completed", finished=now(), traces_cleaned=bool(cleaned), cleanup=cleaned,
                 results_dir=str(results if (cleaned and work != results) else work), **c)
    print(f">>> COMPLETED {bug}: strict={c.get('strict')} fixed_validated={c.get('fixed_validated')} "
          f"checker={c.get('checker_falsified')} old={c.get('old_falsified')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
