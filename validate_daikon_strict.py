#!/usr/bin/env python3
"""Strict validation of a bug's Daikon detections, with automatic attribution
of samples to tests via test markers in BOTH full-suite runs.

Prerequisites (read only, never rewritten): the checker's normal-mode results
(outputs_daikon_checker/normal/<P>_<B>/: invA, invariantsA.txt, traceA,
traceFull, FALSIFIED verdicts, baseline exclusions) and the existing
fixed-version validation (outputs_daikon_fixed_validation/<P>_<B>/:
validation.jsonl, i.e. the baseline / null / fixed-full-suite checks).

Steps (no invariant is inferred):
  1. Check out <B>b and <B>f; assert the triggering tests pass on <B>f.
  2. On each version, Chicory over the same full suite (saved specs_b) with
     DaikonMarkedTestRunner, which calls DaikonTestMarker.begin/end around
     every test (traced via "|^DaikonTestMarker\\." in --ppt-select-pattern) and
     prints "MARK id class::method" (saved as <trace>.marks.json).
  3. daikon_test_windows: keep only samples inside UNAMBIGUOUS windows of the
     triggering tests (well-formed begin/end, no nesting, no thread started
     during the test). Ambiguous windows and samples outside windows never
     count.
  4. Check normal mode's frozen invA (existing checker wrapper, baseline
     exclusions) against the buggy triggering-window trace, and against the
     fixed triggering-window trace mapped onto the buggy program points
     (daikon_trace_map; shifted EXITnn -> UNEXERCISED, changed/missing ->
     UNCHECKABLE, never HELD).

Per buggy-FALSIFIED candidate, category:
  strict                        existing validated hit, AND violated during a
                                buggy triggering-test window, AND evaluated
                                >= 1 time and HELD during the fixed
                                triggering-test windows
  not_evaluated_by_fixed_trigger existing validated, violated during the buggy
                                triggering test, but 0 evaluations in the
                                fixed triggering windows (not reached, only
                                missing values, or a shifted EXITnn). Reported
                                separately; NOT a strict hit, and no claim is
                                made about why (e.g. that the fix removed a call)
  violated_by_fixed_trigger     ... but violated during the fixed triggering test
  fixed_trigger_uncheckable     ... but its ppt cannot be mapped / checked on fixed
  not_violated_during_buggy_trigger  existing validated, but no violation
                                inside an unambiguous buggy triggering window
  not_existing_validated        failed the existing baseline/null/fixed checks
  attribution_unavailable_{buggy,fixed}  no triggering test has an unambiguous
                                window in that run; nothing is attributed
Existing and strict counts are kept separate.

Outputs in <out-root>/<P>_<B>/ (default outputs_daikon_strict_validation):
trace{Buggy,Fixed}Marked.dtrace.gz (+ tests/fails json), window traces and
summaries, the fixed mapping, buggy_trigger_check/, fixed_trigger_check/,
strict.jsonl, run_info.json, logs/, STRICT_COMPLETE (last, only on success).

Usage:
    DAIKON_JAR=... python3 validate_daikon_strict.py <PROJECT> <BUG_ID>
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_daikon_checker_bug as rc  # noqa: E402
from daikon_test_windows import extract, norm_test, parse_marks  # noqa: E402
from daikon_trace_map import build_map, ppt_status, write_mapped  # noqa: E402
from lib_defects4j import capture, d4j_env, kill_current_subprocess, list_test_classes  # noqa: E402
from run_daikon_usefulness_bug import JAVA_XMX, build_full_omit_pattern, checkout, find_junit4_jar, is_valid_gzip  # noqa: E402
from validate_daikon_fixed import FAILING_TESTS, extract_samples, read_jsonl, run_check  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
RUNNER_SRC = THIS_DIR / "DaikonMarkedTestRunner.java"
MARKER_SRC = THIS_DIR / "DaikonTestMarker.java"
RUN_LINE = re.compile(r"^\[DaikonMarkedTestRunner\] RUN (\S+)::(\S+)\s*$")
TOTAL_LINE = re.compile(r"^\[DaikonMarkedTestRunner\] total=(\d+) failures=(\d+)")
FAIL_LINE = re.compile(r"^\[DaikonMarkedTestRunner\] FAIL (\S+)::(\S+?):")
COMPLETE = "STRICT_COMPLETE"
N_SAMPLES = 3

_work_dirs: list[Path] = []


def _sigterm(signum, frame):
    kill_current_subprocess()
    for d in _work_dirs:
        rc.remove_work(d)
    sys.exit(143)


def run_marked_chicory(jar, runner, cp_runner, select, omit, out_dtrace: Path, work: Path, specs, log: Path):
    """As run_daikon_checker_bug.run_chicory, with the marked runner. Writes
    out_dtrace atomically plus <out>.tests.json and <out>.fails.json."""
    tmp_name = f".chicory-{out_dtrace.name}"
    (work / tmp_name).unlink(missing_ok=True)
    cmd = ["java", f"-Xmx{JAVA_XMX}", "-cp", f"{runner}:{cp_runner}:{jar}", "daikon.Chicory",
           f"--ppt-select-pattern={select}", f"--ppt-omit-pattern={omit}", f"--dtrace-file={tmp_name}",
           "DaikonMarkedTestRunner", *specs]
    lines = rc.run_logged(cmd, log, cwd=work)
    ran = [f"{m.group(1)}::{m.group(2)}" for m in map(RUN_LINE.match, lines) if m]
    fails = sorted({f"{m.group(1)}::{rc.norm_method(m.group(2))}" for m in map(FAIL_LINE.match, lines) if m})
    if not any(TOTAL_LINE.match(l) for l in lines):
        raise rc.InfraError(f"marked test runner never printed its summary line (see {log})")
    produced = work / tmp_name
    if not produced.is_file():
        raise rc.InfraError(f"Chicory did not produce {produced}")
    partial = out_dtrace.with_name(".partial-" + out_dtrace.name)
    partial.unlink(missing_ok=True)
    shutil.move(str(produced), str(partial))
    if not is_valid_gzip(partial):
        raise rc.InfraError(f"Chicory wrote an invalid gzip stream: {partial}")
    rc.write_atomic(rc.tests_file(out_dtrace), json.dumps({"specs": specs, "ran": ran}, indent=1))
    rc.write_atomic(out_dtrace.with_name(out_dtrace.name + ".marks.json"), json.dumps(parse_marks(lines), indent=1))
    rc.write_atomic(out_dtrace.with_name(out_dtrace.name + ".fails.json"), json.dumps(fails))
    partial.replace(out_dtrace)
    return ran, fails


def prepare(project, version, work, d4j, logs, tag):
    """Checkout + compile; returns (bin_tests, cp_test)."""
    checkout(project, version, work)
    rc.run_logged(["defects4j", "compile"], logs / f"defects4j_{tag}.log", cwd=work, env=d4j)
    bin_tests = work / capture(["defects4j", "export", "-p", "dir.bin.tests"], cwd=work, env=d4j).strip()
    cp_test = capture(["defects4j", "export", "-p", "cp.test"], cwd=work, env=d4j).strip()
    return bin_tests, cp_test


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("bug_id")
    ap.add_argument("--checker-root", default=None, help="default: $ROOT/outputs_daikon_checker")
    ap.add_argument("--validation-root", default=None, help="default: $ROOT/outputs_daikon_fixed_validation")
    ap.add_argument("--out-root", default=None, help="default: $ROOT/outputs_daikon_strict_validation")
    ap.add_argument("--max-violations", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    jar = os.environ.get("DAIKON_JAR")
    if not jar or not Path(jar).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at daikon.jar (got {jar!r})")
    os.environ["D4J_DEBUG"] = "1"
    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    croot = Path(args.checker_root).resolve() if args.checker_root else root / "outputs_daikon_checker"
    vroot = Path(args.validation_root).resolve() if args.validation_root else root / "outputs_daikon_fixed_validation"
    oroot = Path(args.out_root).resolve() if args.out_root else root / "outputs_daikon_strict_validation"
    bug = f"{args.project}_{args.bug_id}"
    nd, vd, out = croot / "normal" / bug, vroot / bug, oroot / bug
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    marker = out / COMPLETE
    if marker.exists() and not args.force:
        print(f"[INFO] {marker} exists -- already complete")
        return
    marker.unlink(missing_ok=True)

    inv_a, invariants_a = nd / "invA.inv.gz", nd / "invariantsA.txt"
    trace_a, trace_full = nd / "traceA.dtrace.gz", nd / "traceFull.dtrace.gz"
    need = [nd / rc.COMPLETE, inv_a, invariants_a, trace_a, trace_full, nd / "daikon_checker_outcomes.jsonl",
            nd / "run_info.json", vd / "VALIDATION_COMPLETE", vd / "validation.jsonl"]
    missing = [str(p) for p in need if not p.is_file()]
    if missing:
        raise rc.InfraError(f"prerequisites missing (checker results and validate_daikon_fixed.py): {missing}")
    stamp = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in need}
    ninfo = json.loads((nd / "run_info.json").read_text())
    if "requires_investigation" not in ninfo.get("baseline_A", {}):
        raise rc.InfraError(f"{nd} is in an old checker format; run recheck_daikon_checker.py first")
    normal = {(o["ppt"], o["invariant"]): o for o in read_jsonl(nd / "daikon_checker_outcomes.jsonl")}
    existing = {(r["ppt"], r["invariant"]): r for r in read_jsonl(vd / "validation.jsonl")}
    baseline_bad = {(o["ppt"], o["invariant"]): o for o in ninfo["baseline_A"]["falsified"]}
    hits = sorted(k for k, o in normal.items() if o["verdict"] == "FALSIFIED")
    triggering = [tuple(t.split("::", 1)) for t in ninfo["triggering"]]
    trig_names = [f"{c}::{m}" for c, m in triggering]
    specs = ninfo["specs_b"]
    select = f"(?:{ninfo['pkg_pattern']})|^DaikonTestMarker\\."
    info = {"project": args.project, "bug_id": args.bug_id, "daikon_jar": jar, "triggering": trig_names,
            "select_pattern": select, "buggy_falsified": len(hits),
            "existing_validated": sum(1 for r in existing.values() if r["validated"]),
            "started": datetime.datetime.now().isoformat()}

    d4j = d4j_env()
    work_root = Path(os.environ.get("DAIKON_WORK_ROOT", root / "defects4j"))
    work_root.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        traces, window_traces = {}, {}
        for tag, version in (("Buggy", f"{args.bug_id}b"), ("Fixed", f"{args.bug_id}f")):
            work = work_root / f"{args.project}-{version}_strict"
            shutil.rmtree(work, ignore_errors=True)
            _work_dirs.append(work)
            bin_tests, cp_test = prepare(args.project, version, work, d4j, logs, tag)
            if tag == "Fixed":
                results = {}
                for c, m in triggering:
                    lines = rc.run_logged(["defects4j", "test", "-t", f"{c}::{m}"], logs / "defects4j_test.log",
                                          cwd=work, env=d4j)
                    found = [x for x in map(FAILING_TESTS.search, lines) if x]
                    results[f"{c}::{m}"] = int(found[-1].group(1)) if found else None
                info["fixed_triggering_failing"] = results
                if any(n != 0 for n in results.values()):
                    raise rc.InfraError(f"triggering tests do not pass on the fixed version: {results}")
            classes = list_test_classes(str(bin_tests))
            absent = sorted({s.split("::")[0] for s in specs} - set(classes))
            if absent:
                raise rc.InfraError(f"{tag}: test classes of the buggy full suite missing: {absent}")
            cp_runner = f"{cp_test}:{find_junit4_jar()}"
            runner = out / f"runner-classes-{tag}"
            runner.mkdir(exist_ok=True)
            rc.run_logged(["javac", "-g", "-cp", cp_runner, "-d", str(runner), str(MARKER_SRC), str(RUNNER_SRC)],
                          logs / f"javac_runner_{tag}.log")
            tr = out / f"trace{tag}Marked.dtrace.gz"
            ran = rc.load_ran(tr)
            if ran is None:
                ran, fails = run_marked_chicory(jar, runner, cp_runner, select, build_full_omit_pattern(classes), tr,
                                                work, specs, logs / f"chicory_{tag}Marked.log")
            fails = set(json.loads(tr.with_name(tr.name + ".fails.json").read_text()))
            info[f"verify_{tag}"] = rc.verify_tests(f"{tag} marked full suite", ran, triggering, expect_present=True)
            info[f"verify_{tag}"]["triggering_failed"] = sorted(t for t in trig_names if norm_test(t) in fails)
            if tag == "Fixed" and info["verify_Fixed"]["triggering_failed"]:
                raise rc.InfraError(f"triggering tests failed under Chicory on fixed: {info['verify_Fixed']['triggering_failed']}")
            rc.remove_work(work)
            traces[tag] = tr
            wt = out / f"trace{tag}TriggerWindows.dtrace.gz"
            marks = json.loads(tr.with_name(tr.name + ".marks.json").read_text())
            summary = extract(tr, wt, trig_names, marks)
            rc.write_atomic(out / f"windows_{tag}.json", json.dumps(summary, indent=1))
            info[f"windows_{tag}"] = {k: summary[k] for k in ("per_test", "tests_without_ok_window", "counts")}
            window_traces[tag] = wt
            print(f"[INFO] {tag}: triggering windows {summary['per_test']}; "
                  f"kept {summary['counts']['samples_kept']} samples")

        # fixed windows -> buggy program points
        tm = build_map([trace_full, trace_a], traces["Fixed"])
        mapped = out / "traceFixedTriggerWindows.mapped.dtrace.gz"
        info["map_counts"] = write_mapped(window_traces["Fixed"], mapped, tm)
        rc.write_atomic(out / "ppt_map_fixed.json", json.dumps({**tm.to_json(), "counts": info["map_counts"]}, indent=1))
        info["method_status"] = {s: sum(1 for v in tm.method_status.values() if v == s)
                                 for s in sorted(set(tm.method_status.values()))}

        cc = rc.compile_checker(jar, out / "checker-classes", logs / "javac_checker.log")
        text = invariants_a.read_text(errors="replace")
        res, errors = {}, {}
        for label, tr, dest in (("buggy_trigger", window_traces["Buggy"], out / "buggy_trigger_check"),
                                ("fixed_trigger", mapped, out / "fixed_trigger_check")):
            r, err = run_check(label, lambda tr=tr, dest=dest, label=label: rc.check_one(
                jar, cc, inv_a, text, tr, dest, logs, f"_{label}", args.max_violations, baseline_bad))
            if r is not None:
                res[label] = {(o["ppt"], o["invariant"]): o for o in r.pop("_outcomes")}
                info[f"check_{label}"] = r
            else:
                errors[label] = err
        info["checker_errors"] = errors
        if "buggy_trigger" in errors:
            raise rc.InfraError(f"checker failed on the buggy triggering windows: {errors['buggy_trigger']}")

        wanted = {}
        for k in hits:
            wanted.setdefault(k[0], [])
            for v in normal[k].get("vars", []):
                if v not in wanted[k[0]]:
                    wanted[k[0]].append(v)
        fixed_samples = extract_samples(mapped, wanted, N_SAMPLES)
        # attribution needs at least one unambiguous triggering-test window per version
        buggy_ok = len(info["windows_Buggy"]["tests_without_ok_window"]) < len(trig_names)
        fixed_ok = len(info["windows_Fixed"]["tests_without_ok_window"]) < len(trig_names)
        rows = []
        for k in hits:
            ppt, inv = k
            ex = existing.get(k, {})
            bt = res["buggy_trigger"].get(k, {})
            st, forced, why = ppt_status(tm, ppt)
            ft = res.get("fixed_trigger", {}).get(k)
            if "fixed_trigger" in errors:
                fverdict, freason, fevals = "UNCHECKABLE", f"checker failed on fixed windows: {errors['fixed_trigger']}", 0
            elif forced:
                fverdict, freason, fevals = forced, why, 0
            elif ft is None:
                fverdict, freason, fevals = "UNCHECKABLE", "no fixed-window outcome", 0
            else:
                fverdict, freason, fevals = ft["verdict"], ft.get("reason"), ft.get("evaluations", 0)
            bviol = bt.get("violations", 0) or 0
            if not ex.get("validated"):
                cat = "not_existing_validated"
            elif not buggy_ok:
                cat = "attribution_unavailable_buggy"
            elif bviol == 0:
                cat = "not_violated_during_buggy_trigger"
            elif not fixed_ok:
                cat = "attribution_unavailable_fixed"
            elif fverdict == "UNCHECKABLE":
                cat = "fixed_trigger_uncheckable"
            elif fverdict == "FALSIFIED":
                cat = "violated_by_fixed_trigger"
            elif fverdict == "HELD" and fevals > 0:
                cat = "strict"
            else:
                cat = "not_evaluated_by_fixed_trigger"
            vars_ = normal[k].get("vars", [])
            rows.append({
                "ppt": ppt, "invariant": inv, "category": cat, "strict": cat == "strict",
                "existing_validated": bool(ex.get("validated")), "existing_failed_checks": ex.get("failed_checks"),
                "buggy_trigger": {"verdict": bt.get("verdict"), "raw_verdict": bt.get("raw_verdict"),
                                  "evaluations": bt.get("evaluations"), "violations": bviol,
                                  "violating_samples": bt.get("violating_samples", [])},
                "fixed_trigger": {"verdict": fverdict, "reason": freason, "evaluations": fevals,
                                  "violations": (ft or {}).get("violations"), "ppt_map_status": st,
                                  "violating_samples": (ft or {}).get("violating_samples", []),
                                  "samples": [{v: s.get(v) for v in vars_} for s in fixed_samples.get(ppt, [])]},
            })
        rc.write_atomic(out / "strict.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
        changed = [p for p in stamp if (Path(p).stat().st_size, Path(p).stat().st_mtime_ns) != stamp[p]]
        if changed:
            raise rc.InfraError(f"prerequisite files changed during the run: {changed}")
        cats = {}
        for r in rows:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        info.update({"categories": cats, "strict_hits": cats.get("strict", 0),
                     "finished": datetime.datetime.now().isoformat()})
        rc.write_atomic(out / "run_info.json", json.dumps(info, indent=1))
        rc.write_atomic(marker, json.dumps({"strict_hits": info["strict_hits"],
                                            "existing_validated": info["existing_validated"]}) + "\n")
        print("=" * 60)
        print(f">>> STRICT {bug}: {info['strict_hits']} strict hit(s); existing validated {info['existing_validated']} "
              f"of {len(hits)} buggy-FALSIFIED")
        print(f"    categories: {cats}")
        print(f"    buggy triggering windows: {info['windows_Buggy']['per_test']}  "
              f"fixed: {info['windows_Fixed']['per_test']}  checker errors: {errors or '-'}")
        print("=" * 60)
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        rc.write_atomic(out / "run_info.json", json.dumps(info, indent=1))
        raise
    finally:
        for d in _work_dirs:
            rc.remove_work(d)


if __name__ == "__main__":
    try:
        main()
    except (rc.InfraError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
