#!/usr/bin/env python3
"""RQ5 for Daikon on ONE Defects4J bug, with the Phase-A invariants CHECKED
against the Phase-B trace instead of re-inferred and text-diffed.

daikon_diff_invariants.py calls an invariant FALSIFIED when its printed text
is missing from a second, independent Daikon run. That does not show any
sample violated it (the second run can drop an invariant for many other
reasons). Here the invariants inferred from trace A are frozen and each
Phase-B sample is checked against them:

  Phase A: Chicory over every test EXCEPT the triggering test method(s)
    -> traceA.dtrace.gz; Daikon once -> invA.inv.gz; PrintInvariants ->
    invariantsA.txt (the candidates).
  Phase B: Chicory in a fresh JVM -> traceFull.dtrace.gz over the full,
    unmodified suite (normal), or traceNull.dtrace.gz over phase A's own
    specs again (--null: the same tests, so any "violation" is run-to-run
    noise, not the bug).
  Check: DaikonCandidateChecker (a minimal wrapper around Daikon's
    InvariantChecker classes; see that file for why the stock tool alone
    can't give evaluation counts) evaluates every invariant of invA on the
    Phase-B samples, without a second inference. The stock
    daikon.tools.InvariantChecker --verbose also runs on the same inputs,
    and its raw output is kept and cross-checked.
  Baseline: the same check of invA against traceA itself (the samples it
    was inferred from), in baseline_A/. Expected: no FALSIFIED; any are
    reported, not hidden.

Eligible samples are the ones Daikon's inference uses: numbered-EXIT
samples (also applied to the combined EXIT), the ENTER sample of every call
that returned, and those propagated through the ppt hierarchy to OBJECT /
CLASS. ENTER samples of calls that never returned (threw, or the trace
ended) are not used by Daikon's inference; they are checked as diagnostics
only (diag_* fields) and never affect a verdict.

Candidates: the invariants printed in invariantsA.txt for which
daikon_diff_invariants.is_overfit_prone_invariant() is False. Each is mapped
to the invariant object that PrintInvariants printed it from (same printed
text at the same ppt). Verdicts:

  FALSIFIED            >= 1 eligible sample violated it (counted once per
                       invariant); "falsified_by" says where (see below)
  HELD                 >= 1 eligible evaluation and no eligible violation
  UNEXERCISED          no eligible sample reached its ppt
  UNEVALUATED_MISSING  eligible samples reached its ppt, but on every one a
                       variable was missing (nonsensical/flow) or out of
                       bounds, so it was never evaluated
  UNCHECKABLE          could not be mapped to exactly one active invariant of
                       invA (e.g. implications, ambiguous text), the stock
                       checker and the wrapper disagree on its own path, or
                       reason "inconsistent_with_inference_trace": the
                       invariant is violated on traceA, the very samples it
                       was inferred from (see below)

Inference/check inconsistency. Daikon can print invariants that its own
check rejects on its training samples. Known case, confirmed with the stock
InvariantChecker: PptSliceEquality.createEqualityInvs regroups variables
split off an equality set in a HashMap keyed by the boxed value, and
Double.equals(NaN, NaN) is true, so variables that are NaN together in one
sample are put in one equality set; Daikon then prints "a == b", which
FloatEqual (fuzzy NaN != NaN) rejects on that same sample. The baseline
check (invA vs traceA) finds every such candidate, whatever the cause; they
get verdict UNCHECKABLE with reason inconsistent_with_inference_trace, in
the normal and in the null run. Their raw_verdict and all counts are kept
(violations_all_nan counts violations where every compared value is NaN).
NaN is never ignored and NaN equality is never forced to pass.

Eligible violation origins (falsified_by is the first nonzero):
  direct       a sample of this ppt (or EXITnn -> EXIT) -- what the stock
               checker sees
  propagated   reached an OBJECT/CLASS ppt through the hierarchy
Diagnostic only (diag_violations_*): unmatched_entry (an ENTER sample of a
call that never returned) and propagated_unmatched_entry (the same, reaching
OBJECT/CLASS).

In --null mode, phase B must run exactly the tests phase A ran, with the
same multiplicities; otherwise the run fails without a completion marker.

Outputs, in <out-root>/<normal|null>/<PROJECT>_<BUG>/ (nothing under
outputs_usefulness/ is touched): the traces, invA.inv.gz, invariantsA.txt,
checker_records.jsonl (every invariant of invA with counts),
checker_summary.json, stock_checker_verbose.txt,
daikon_checker_outcomes.jsonl, baseline_A/ (the same files for invA vs.
traceA), run_info.json, logs/, and CHECKER_COMPLETE, written last and only
after every step succeeded.

Usage:
    DAIKON_JAR=/path/to/daikon.jar python3 run_daikon_checker_bug.py <PROJECT> <BUG_ID> [--null]
To redo only the check on saved traces/invA, see recheck_daikon_checker.py.
"""
from __future__ import annotations

import argparse
import collections
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
import lib_defects4j  # noqa: E402
from lib_defects4j import (  # noqa: E402
    capture,
    d4j_env,
    derive_package_pattern,
    kill_current_subprocess,
    list_test_classes,
    parse_triggering_tests,
)
from daikon_diff_invariants import is_overfit_prone_invariant, parse_daikon_invariants  # noqa: E402
from run_daikon_usefulness_bug import (  # noqa: E402
    DAIKON_EXTRA_CONFIG,
    JAVA_XMX,
    build_full_omit_pattern,
    checkout,
    find_junit4_jar,
    is_valid_gzip,
)

THIS_DIR = Path(__file__).resolve().parent
RUNNER_SRC = THIS_DIR / "DaikonCheckerTestRunner.java"
CHECKER_SRC = THIS_DIR / "DaikonCandidateChecker.java"
RUN_LINE = re.compile(r"^\[DaikonCheckerTestRunner\] RUN (\S+)::(\S+)\s*$")
TOTAL_LINE = re.compile(r"^\[DaikonCheckerTestRunner\] total=(\d+) failures=(\d+)")
STOCK_LINE = re.compile(r"^At ppt (.*?), Invariant '(.*)' invalidated by sample (.*)at line (\d+) in file (.*)$")

# Same Daikon options as run_daikon_usefulness_bug.run_daikon.
DAIKON_CONFIG = ["daikon.split.PptSplitter.disable_splitting=true", *DAIKON_EXTRA_CONFIG]

COMPLETE = "CHECKER_COMPLETE"
VERDICTS = ("FALSIFIED", "HELD", "UNEXERCISED", "UNEVALUATED_MISSING", "UNCHECKABLE")
ORIGINS = ("direct", "propagated")  # eligible: decide verdicts
DIAG_ORIGINS = ("unmatched_entry", "propagated_unmatched_entry")  # diagnostic only


class InfraError(RuntimeError):
    """A step did not produce what the experiment needs (never a result)."""


_work_dir_for_cleanup: Path | None = None


def _handle_sigterm(signum, frame):
    kill_current_subprocess()
    if _work_dir_for_cleanup is not None:
        print(f"[INFO] caught SIGTERM -- removing {_work_dir_for_cleanup}", flush=True)
        shutil.rmtree(_work_dir_for_cleanup, ignore_errors=True)
    sys.exit(143)


def run_logged(cmd, log: Path, cwd=None, env=None) -> list[str]:
    """Runs cmd, appending its combined output to `log` (and echoing it).
    Returns the output lines; raises CalledProcessError on a nonzero exit."""
    log.parent.mkdir(parents=True, exist_ok=True)
    print("+", " ".join(str(c) for c in cmd), flush=True)
    lines: list[str] = []
    with open(log, "a", errors="replace") as lf:
        lf.write(f"\n===== {datetime.datetime.now().isoformat()} cwd={cwd}\n+ {' '.join(map(str, cmd))}\n")
        lf.flush()
        proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
        )
        lib_defects4j._current_subprocess = proc
        try:
            for line in proc.stdout:
                lf.write(line)
                sys.stdout.write(line)
                lines.append(line.rstrip("\n"))
            rc = proc.wait()
        finally:
            lib_defects4j._current_subprocess = None
        lf.write(f"===== exit {rc}\n")
    sys.stdout.flush()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)
    return lines


def write_atomic(path: Path, text: str):
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def norm_method(m: str) -> str:
    """JUnit4 parameterized runs name methods "m[0]"; Defects4J names "m"."""
    return re.sub(r"\[.*\]$", "", m)


def run_chicory(daikon_jar, runner_classes, cp_runner, pkg_pattern, omit_pattern,
                out_dtrace: Path, work_dir: Path, specs: list[str], log: Path) -> list[str]:
    """Chicory over `specs` in a fresh JVM. Writes out_dtrace (atomically)
    and <out_dtrace>.tests.json with the tests the runner actually started.
    Returns those tests as "class::method"."""
    if not specs:
        raise ValueError("no test specs given to Chicory")
    # --dtrace-file must be a relative name (Chicory writes nothing for an
    # absolute path); run in work_dir and move the result afterwards.
    tmp_name = f".chicory-{out_dtrace.name}"
    (work_dir / tmp_name).unlink(missing_ok=True)
    cmd = [
        "java", f"-Xmx{JAVA_XMX}", "-cp", f"{runner_classes}:{cp_runner}:{daikon_jar}",
        "daikon.Chicory",
        f"--ppt-select-pattern={pkg_pattern}",
        f"--ppt-omit-pattern={omit_pattern}",
        f"--dtrace-file={tmp_name}",
        "DaikonCheckerTestRunner", *specs,
    ]
    lines = run_logged(cmd, log, cwd=work_dir)
    ran = [f"{m.group(1)}::{m.group(2)}" for m in map(RUN_LINE.match, lines) if m]
    totals = [TOTAL_LINE.match(l) for l in lines if TOTAL_LINE.match(l)]
    if not totals:
        raise InfraError(f"test runner never printed its summary line (see {log})")
    produced = work_dir / tmp_name
    if not produced.is_file():
        raise InfraError(f"Chicory did not produce {produced}")
    partial = out_dtrace.with_name(".partial-" + out_dtrace.name)
    partial.unlink(missing_ok=True)
    shutil.move(str(produced), str(partial))
    if not is_valid_gzip(partial):
        raise InfraError(f"Chicory wrote an invalid gzip stream: {partial}")
    write_atomic(tests_file(out_dtrace), json.dumps(
        {"specs": specs, "ran": ran, "total": int(totals[-1].group(1)), "failures": int(totals[-1].group(2))},
        indent=1))
    partial.replace(out_dtrace)
    return ran


def tests_file(trace: Path) -> Path:
    return trace.with_name(trace.name + ".tests.json")


def load_ran(trace: Path) -> list[str] | None:
    """Tests recorded for a finished trace, or None if it can't be reused."""
    tf = tests_file(trace)
    if not (trace.is_file() and tf.is_file()):
        return None
    if not is_valid_gzip(trace):
        print(f"[WARN] {trace} is not a valid gzip stream; regenerating it")
        return None
    return json.loads(tf.read_text())["ran"]


def verify_tests(label: str, ran: list[str], triggering: list[tuple[str, str]], expect_present: bool):
    ran_set = {(c, norm_method(m)) for c, m in (t.split("::", 1) for t in ran)}
    if not ran_set:
        raise InfraError(f"{label}: the test runner started no tests")
    trig = {(c, m) for c, m in triggering}
    present = sorted(f"{c}::{m}" for c, m in trig & ran_set)
    missing = sorted(f"{c}::{m}" for c, m in trig - ran_set)
    if expect_present and missing:
        raise InfraError(f"{label}: triggering test(s) did not run: {missing}")
    if not expect_present and present:
        raise InfraError(f"{label}: triggering test(s) ran but must be excluded: {present}")
    print(f"[INFO] {label}: {len(ran_set)} tests ran; triggering tests "
          f"{'all present' if expect_present else 'all absent'} (verified)")
    return {"tests_ran": len(ran_set), "triggering_present": present, "triggering_missing": missing}


def verify_same_inventory(ran_a: list[str], ran_b: list[str], record: dict):
    """--null: phase B must execute the same tests as phase A, each the
    same number of times. The comparison is stored in `record` first, so
    run_info.json shows it even when this raises."""
    ca, cb = collections.Counter(ran_a), collections.Counter(ran_b)
    diff = {t: {"A": ca.get(t, 0), "B": cb.get(t, 0)} for t in sorted(set(ca) | set(cb)) if ca.get(t) != cb.get(t)}
    record.update({"tests_executed_a": sum(ca.values()), "tests_executed_b": sum(cb.values()),
                   "inventory_diff": diff})
    if diff:
        shown = ", ".join(f"{t} (A={d['A']}, B={d['B']})" for t, d in list(diff.items())[:10])
        raise InfraError(f"null mode: phase B's executed tests differ from phase A's in {len(diff)} "
                         f"test(s): {shown}{' ...' if len(diff) > 10 else ''}")
    print(f"[INFO] null mode: phase B executed the same {sum(cb.values())} tests as phase A (verified)")


def run_daikon(daikon_jar, trace: Path, out_inv: Path, pkg_pattern: str, log: Path):
    # The temp name must end in ".gz" (Daikon decides compression by suffix).
    tmp_out = out_inv.with_name(out_inv.stem + ".tmp" + out_inv.suffix)
    tmp_out.unlink(missing_ok=True)
    cmd = ["java", f"-Xmx{JAVA_XMX}", "-cp", daikon_jar, "daikon.Daikon", "--no_show_progress",
           *[a for opt in DAIKON_CONFIG for a in ("--config_option", opt)],
           f"--ppt-select-pattern={pkg_pattern}", "-o", str(tmp_out), str(trace)]
    run_logged(cmd, log)
    tmp_out.replace(out_inv)


def inv_marker_text(pkg_pattern: str, trace: Path) -> str:
    return json.dumps({"pkg_pattern": pkg_pattern, "config": DAIKON_CONFIG,
                       "trace_size": trace.stat().st_size})


def print_invariants(daikon_jar, inv: Path, out_txt: Path, log: Path):
    tmp = out_txt.with_name("." + out_txt.name + ".tmp")
    cmd = ["java", f"-Xmx{JAVA_XMX}", "-cp", daikon_jar, "daikon.PrintInvariants", str(inv)]
    print("+", " ".join(cmd), flush=True)
    with open(tmp, "w") as out, open(log, "a") as lf:
        lf.write(f"\n===== {datetime.datetime.now().isoformat()}\n+ {' '.join(cmd)}\n")
        lf.flush()
        rc = subprocess.run(cmd, stdout=out, stderr=lf).returncode
        lf.write(f"===== exit {rc}\n")
    if rc != 0:
        raise InfraError(f"PrintInvariants failed (exit {rc}); see {log}")
    tmp.replace(out_txt)


def compile_checker(daikon_jar: str, classes: Path, log: Path) -> Path:
    classes.mkdir(parents=True, exist_ok=True)
    run_logged(["javac", "-nowarn", "-cp", daikon_jar, "-d", str(classes), str(CHECKER_SRC)], log)
    return classes


def run_wrapper(daikon_jar, checker_classes: Path, inv: Path, trace: Path, out_dir: Path,
                log: Path, max_violations: int):
    records, summary = out_dir / "checker_records.jsonl", out_dir / "checker_summary.json"
    cmd = ["java", f"-Xmx{JAVA_XMX}", "-cp", f"{checker_classes}:{daikon_jar}", "DaikonCandidateChecker",
           "--inv", str(inv), "--out", str(records), "--summary", str(summary),
           "--max-violations", str(max_violations),
           *[a for opt in DAIKON_CONFIG for a in ("--config_option", opt)], str(trace)]
    run_logged(cmd, log)
    return records, summary


def run_stock_checker(daikon_jar, inv: Path, trace: Path, out_dir: Path, log: Path) -> Path:
    """The unmodified daikon.tools.InvariantChecker, raw output kept."""
    verbose = out_dir / "stock_checker_verbose.txt"
    tmp = out_dir / ".stock_checker_verbose.txt.tmp"
    tmp.unlink(missing_ok=True)
    cmd = ["java", f"-Xmx{JAVA_XMX}", "-cp", daikon_jar, "daikon.tools.InvariantChecker",
           "--verbose", "--output", str(tmp),
           *[a for opt in DAIKON_CONFIG for a in ("--config_option", opt)], str(inv), str(trace)]
    run_logged(cmd, log)
    if not tmp.is_file():
        raise InfraError("stock InvariantChecker wrote no output file")
    tmp.replace(verbose)
    return verbose


def parse_stock(verbose: Path) -> tuple[set[tuple[str, str]], int]:
    """(ppt, inv.format()) of every invariant the stock checker reported
    invalidated, and the number of "At ppt" lines it could not parse."""
    failed, unparsed = set(), 0
    with open(verbose, errors="replace") as f:
        for line in f:
            if not line.startswith("At ppt "):
                continue
            m = STOCK_LINE.match(line.rstrip("\n"))
            if m:
                failed.add((m.group(1), m.group(2)))
            else:
                unparsed += 1
    return failed, unparsed


def load_records(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def classify(invariants_a_text: str, records: list[dict], stock_failed: set | None,
             baseline_bad: dict | None = None) -> tuple[list[dict], dict]:
    """One outcome per candidate: every (ppt, printed line) of invariantsA.txt
    that is_overfit_prone_invariant() does not exclude. `baseline_bad` maps
    (ppt, invariant) of candidates FALSIFIED on traceA itself to that
    baseline outcome; those become UNCHECKABLE (inconsistent_with_inference_trace)."""
    baseline_bad = baseline_bad or {}
    by_printed: dict[tuple[str, str], list[dict]] = {}
    by_format: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        if r["printed"] is not None:
            by_printed.setdefault((r["ppt"], r["printed"]), []).append(r)
        if r["format"] is not None:
            by_format.setdefault((r["ppt"], r["format"]), []).append(r)

    # Stock-checker cross-check, both ways, on the stock checker's own path:
    # violations_direct excludes propagated samples and (diagnostic)
    # unmatched-ENTER samples, which the stock checker never checks.
    disagree: set[tuple[str, str]] = set()
    stock_only: set[tuple[str, str]] = set()
    wrapper_only: set[tuple[str, str]] = set()
    if stock_failed is not None:
        for key in stock_failed:
            if not any(r["violations_direct"] > 0 for r in by_format.get(key, [])):
                stock_only.add(key)
        for r in records:
            if r["violations_direct"] > 0 and (r["ppt"], r["format"]) not in stock_failed:
                wrapper_only.add((r["ppt"], r["format"]))
        disagree = stock_only | wrapper_only

    outcomes = []
    parsed = parse_daikon_invariants(invariants_a_text)
    for ppt in sorted(parsed):
        for text in sorted(parsed[ppt]):
            if is_overfit_prone_invariant(text):
                continue
            row = {"ppt": ppt, "invariant": text}
            recs = by_printed.get((ppt, text), [])
            if len(recs) > 1:
                kept = [r for r in recs if r["filter_keep"]]
                recs = kept if len(kept) == 1 else recs
            if not recs:
                outcomes.append({**row, "verdict": "UNCHECKABLE",
                                 "reason": "no invariant of invA prints this text (e.g. an implication)"})
                continue
            if len(recs) > 1:
                outcomes.append({**row, "verdict": "UNCHECKABLE",
                                 "reason": f"ambiguous: {len(recs)} invariants of invA print this text"})
                continue
            r = recs[0]
            info = {
                "daikon_class": r["class"], "vars": r["vars"],
                "evaluations": r["evaluations"], "violations": r["violations"],
                **{f"violations_{o}": r[f"violations_{o}"] for o in ORIGINS},
                "skipped_missing": r["skipped_missing"],
                "skipped_out_of_bounds": r["skipped_out_of_bounds"],
                "violations_all_nan": r.get("violations_all_nan"),
                "violating_samples": r["first_violations"],
                # diagnostics: never used for the verdict
                "diag_evaluations_unmatched_entry": r["diag_evaluations_unmatched_entry"],
                **{f"diag_violations_{o}": r[f"diag_violations_{o}"] for o in DIAG_ORIGINS},
                "diag_violating_samples": r["first_diag_violations"],
            }
            # the verdict the samples alone give
            if r["violations"] > 0:
                raw = {"verdict": "FALSIFIED", "falsified_by": next(o for o in ORIGINS if r[f"violations_{o}"] > 0)}
            elif r["evaluations"] > 0:
                raw = {"verdict": "HELD"}
            elif r["skipped_missing"] + r["skipped_out_of_bounds"] > 0:
                raw = {"verdict": "UNEVALUATED_MISSING"}
            else:
                raw = {"verdict": "UNEXERCISED"}
            info["raw_verdict"] = raw["verdict"]
            if not r["active"]:
                outcomes.append({**row, "verdict": "UNCHECKABLE", "reason": "invariant is not active", **info})
            elif (r["ppt"], r["format"]) in disagree:
                why = ("stock InvariantChecker reports a violation the wrapper does not"
                       if (r["ppt"], r["format"]) in stock_only else
                       "wrapper reports a direct violation the stock InvariantChecker does not")
                outcomes.append({**row, "verdict": "UNCHECKABLE", "reason": why, **info})
            elif (ppt, text) in baseline_bad:
                b = baseline_bad[(ppt, text)]
                outcomes.append({**row, "verdict": "UNCHECKABLE", "reason": "inconsistent_with_inference_trace",
                                 "baseline_violations": b["violations"], "baseline_evaluations": b["evaluations"],
                                 "baseline_violations_all_nan": b.get("violations_all_nan"),
                                 "baseline_violating_samples": b["violating_samples"][:2],
                                 **({"raw_falsified_by": raw["falsified_by"]} if "falsified_by" in raw else {}),
                                 **info})
            else:
                outcomes.append({**row, **raw, **info})

    cross = {
        "stock_checked": stock_failed is not None,
        "stock_failed": len(stock_failed) if stock_failed is not None else None,
        "stock_only_disagreements": sorted(f"{p} :: {i}" for p, i in stock_only),
        "wrapper_only_disagreements": sorted(f"{p} :: {i}" for p, i in wrapper_only),
    }
    return outcomes, cross


def write_outcomes(path: Path, outcomes: list[dict]):
    write_atomic(path, "".join(json.dumps(o) + "\n" for o in outcomes))


def tally(outcomes: list[dict]) -> dict:
    diag = [o for o in outcomes if any(o.get(f"diag_violations_{g}", 0) for g in DIAG_ORIGINS)]
    return {
        "candidates": len(outcomes),
        "counts": {v: sum(1 for o in outcomes if o["verdict"] == v) for v in VERDICTS},
        "falsified_by": {g: sum(1 for o in outcomes if o.get("falsified_by") == g) for g in ORIGINS},
        # candidates an unmatched ENTER sample would have violated, by their (eligible) verdict
        "diag_unmatched_entry_candidates": {v: sum(1 for o in diag if o["verdict"] == v) for v in VERDICTS},
        "inconsistent_with_inference_trace": sum(1 for o in outcomes
                                                 if o.get("reason") == "inconsistent_with_inference_trace"),
        "inconsistent_raw_verdicts": {v: sum(1 for o in outcomes if o.get("reason") == "inconsistent_with_inference_trace"
                                             and o.get("raw_verdict") == v) for v in VERDICTS},
    }


def check_one(daikon_jar, checker_classes: Path, inv: Path, invariants_a_text: str, trace: Path,
              dest: Path, logs: Path, tag: str, max_violations: int, baseline_bad: dict | None = None) -> dict:
    """invA against one trace: wrapper + stock checker + classification, all
    outputs in `dest`. Returns tally + cross-check + checker summary."""
    dest.mkdir(parents=True, exist_ok=True)
    records_path, summary_path = run_wrapper(daikon_jar, checker_classes, inv, trace, dest,
                                             logs / f"checker_wrapper{tag}.log", max_violations)
    stock_failed, unparsed = parse_stock(
        run_stock_checker(daikon_jar, inv, trace, dest, logs / f"stock_checker{tag}.log"))
    outcomes, cross = classify(invariants_a_text, load_records(records_path), stock_failed, baseline_bad)
    cross["stock_unparsed_lines"] = unparsed
    write_outcomes(dest / "daikon_checker_outcomes.jsonl", outcomes)
    return {**tally(outcomes), "cross_check": cross,
            "checker_summary": json.loads(summary_path.read_text()), "_outcomes": outcomes}


def run_checks(daikon_jar, out_dir: Path, inv_a: Path, invariants_a: Path, trace_a: Path, trace_b: Path,
               logs: Path, info: dict, max_violations: int):
    """Checks invA against traceA itself (baseline -> out_dir/baseline_A)
    first, then against trace B (-> out_dir) with every candidate the
    baseline FALSIFIED classified as inconsistent_with_inference_trace;
    updates `info`, then writes run_info.json and, last, the completion
    marker."""
    checker_classes = compile_checker(daikon_jar, out_dir / "checker-classes", logs / "javac_checker.log")
    text = invariants_a.read_text(errors="replace")
    base = check_one(daikon_jar, checker_classes, inv_a, text, trace_a, out_dir / "baseline_A", logs, "_baseline_A",
                     max_violations)
    base_outcomes = base.pop("_outcomes")
    base_fals = [{"ppt": o["ppt"], "invariant": o["invariant"], "falsified_by": o["falsified_by"],
                  "violations": o["violations"], "evaluations": o["evaluations"],
                  "violations_all_nan": o.get("violations_all_nan"), "daikon_class": o.get("daikon_class"),
                  "violating_samples": o["violating_samples"][:2]}
                 for o in base_outcomes if o["verdict"] == "FALSIFIED"]
    baseline_bad = {(o["ppt"], o["invariant"]): o for o in base_fals}
    res = check_one(daikon_jar, checker_classes, inv_a, text, trace_b, out_dir, logs, "", max_violations, baseline_bad)
    res.pop("_outcomes")
    info.update(res)
    info["baseline_A"] = {**base, "falsified": base_fals,
                          "falsified_all_nan_only": sum(1 for o in base_fals
                                                        if o["violations_all_nan"] == o["violations"]),
                          "requires_investigation": bool(base_fals)}
    write_atomic(out_dir / "run_info.json", json.dumps(info, indent=1))
    write_atomic(out_dir / COMPLETE, json.dumps({"finished": datetime.datetime.now().isoformat(),
                                                 **res["counts"]}) + "\n")
    print("=" * 60)
    print(f">>> DONE {info['project']}-{info['bug_id']} [{info['mode']}]: candidates={res['candidates']} {res['counts']}")
    print(f"    FALSIFIED by origin: {res['falsified_by']}")
    print(f"    diagnostic (unmatched ENTER, not in verdicts): candidates it would violate, by verdict: "
          f"{res['diag_unmatched_entry_candidates']}")
    print(f"    baseline invA vs traceA: {base['counts']['FALSIFIED']} FALSIFIED of {base['candidates']} candidates"
          f" ({info['baseline_A']['falsified_all_nan_only']} with only all-NaN violations)"
          f"{'  -- REQUIRES INVESTIGATION' if base_fals else ''}")
    print(f"    classified inconsistent_with_inference_trace: {res['inconsistent_with_inference_trace']} "
          f"(raw verdicts on trace B: {res['inconsistent_raw_verdicts']})")
    for o in base_fals[:10]:
        print(f"      BASELINE VIOLATION {o['ppt']} :: {o['invariant']} ({o['falsified_by']}, {o['violations']})")
    for label, r in (("check", res), ("baseline", base)):
        n = len(r["cross_check"]["stock_only_disagreements"]) + len(r["cross_check"]["wrapper_only_disagreements"])
        if n:
            print(f"[WARN] {label}: {n} stock-checker disagreement(s), marked UNCHECKABLE (see run_info.json)")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("bug_id")
    ap.add_argument("--null", action="store_true",
                    help="phase B reruns phase A's specs in a fresh JVM (null / noise run)")
    ap.add_argument("--out-root", default=None,
                    help="default: $ROOT/outputs_daikon_checker (normal/ and null/ below it)")
    ap.add_argument("--pkg-pattern", default=None)
    ap.add_argument("--max-violations", type=int, default=5,
                    help="violating samples kept per invariant")
    ap.add_argument("--force", action="store_true", help="rerun even if CHECKER_COMPLETE exists")
    args = ap.parse_args()

    daikon_jar = os.environ.get("DAIKON_JAR")
    if not daikon_jar or not Path(daikon_jar).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at a built daikon.jar (got: {daikon_jar!r})")
    os.environ["D4J_DEBUG"] = "1"

    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    mode = "null" if args.null else "normal"
    out_root = Path(args.out_root).resolve() if args.out_root else root / "outputs_daikon_checker"
    out_dir = out_root / mode / f"{args.project}_{args.bug_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    logs = out_dir / "logs"
    logs.mkdir(exist_ok=True)
    marker = out_dir / COMPLETE
    if marker.exists() and not args.force:
        print(f"[INFO] {marker} exists -- already complete, nothing to do")
        return
    marker.unlink(missing_ok=True)

    trace_a = out_dir / "traceA.dtrace.gz"
    trace_b = out_dir / ("traceFull.dtrace.gz" if mode == "normal" else "traceNull.dtrace.gz")
    inv_a = out_dir / "invA.inv.gz"
    inv_a_marker = inv_a.with_name(inv_a.name + ".info")
    invariants_a = out_dir / "invariantsA.txt"

    d4j = d4j_env()
    version = f"{args.bug_id}b"
    work_root = Path(os.environ.get("DAIKON_WORK_ROOT", root / "defects4j"))
    work_root.mkdir(parents=True, exist_ok=True)
    # Distinct from run_daikon_usefulness_bug.py's "<P>-<v>_daikon", which
    # that script deletes on start.
    work_dir = work_root / f"{args.project}-{version}_checker_{mode}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    global _work_dir_for_cleanup
    _work_dir_for_cleanup = work_dir
    signal.signal(signal.SIGTERM, _handle_sigterm)

    info: dict = {"project": args.project, "bug_id": args.bug_id, "mode": mode,
                  "daikon_jar": daikon_jar, "java_xmx": JAVA_XMX, "daikon_config": DAIKON_CONFIG}
    try:
        checkout(args.project, version, work_dir)
        run_logged(["defects4j", "compile"], logs / "defects4j.log", cwd=work_dir, env=d4j)
        main_src = work_dir / capture(["defects4j", "export", "-p", "dir.src.classes"], cwd=work_dir, env=d4j).strip()
        bin_tests = work_dir / capture(["defects4j", "export", "-p", "dir.bin.tests"], cwd=work_dir, env=d4j).strip()
        cp_test = capture(["defects4j", "export", "-p", "cp.test"], cwd=work_dir, env=d4j).strip()
        pkg_pattern = args.pkg_pattern or derive_package_pattern(str(main_src))
        triggering = parse_triggering_tests(
            capture(["defects4j", "info", "-p", args.project, "-b", args.bug_id], env=d4j))
        if not triggering:
            raise InfraError(f"no triggering tests found for {args.project}-{args.bug_id}")
        all_classes = list_test_classes(str(bin_tests))
        if not all_classes:
            raise InfraError(f"no compiled test classes under {bin_tests}")
        omit_pattern = build_full_omit_pattern(all_classes)

        trig_by_class: dict[str, list[str]] = {}
        for cls, meth in triggering:
            trig_by_class.setdefault(cls, []).append(meth)
        missing_cls = sorted(c for c in trig_by_class if c not in all_classes)
        if missing_cls:
            raise InfraError(f"triggering test class(es) not among compiled test classes: {missing_cls}")
        specs_a = [f"{c}::" + ",".join(f"!{m}" for m in trig_by_class[c]) if c in trig_by_class else c
                   for c in all_classes]
        specs_b = list(specs_a) if mode == "null" else list(all_classes)
        info.update({"pkg_pattern": pkg_pattern, "triggering": [f"{c}::{m}" for c, m in triggering],
                     "specs_a": specs_a, "specs_b": specs_b})
        print(f"[INFO] mode={mode} pkg_pattern={pkg_pattern} triggering={info['triggering']}")

        cp_runner = f"{cp_test}:{find_junit4_jar()}"
        runner_classes = out_dir / "runner-classes"
        runner_classes.mkdir(exist_ok=True)
        run_logged(["javac", "-cp", cp_runner, "-d", str(runner_classes), str(RUNNER_SRC)], logs / "javac_runner.log")

        # ---- Phase A: trace, invA (inferred once), invariantsA.txt
        ran_a = load_ran(trace_a)
        if ran_a is not None:
            print(f"[INFO] reusing finished {trace_a}")
        else:
            print(f">>> Chicory phase A (without triggering tests): {args.project}-{args.bug_id} [{mode}]")
            ran_a = run_chicory(daikon_jar, runner_classes, cp_runner, pkg_pattern, omit_pattern,
                                trace_a, work_dir, specs_a, logs / "chicory_A.log")
        info["verify_a"] = verify_tests("phase A", ran_a, triggering, expect_present=False)

        want = inv_marker_text(pkg_pattern, trace_a)
        if inv_a.is_file() and inv_a_marker.is_file() and inv_a_marker.read_text() == want and is_valid_gzip(inv_a):
            print(f"[INFO] reusing finished {inv_a}")
        else:
            inv_a.unlink(missing_ok=True)
            invariants_a.unlink(missing_ok=True)
            print(">>> Daikon on trace A")
            run_daikon(daikon_jar, trace_a, inv_a, pkg_pattern, logs / "daikon_A.log")
            write_atomic(inv_a_marker, want)
        if not invariants_a.is_file():
            print_invariants(daikon_jar, inv_a, invariants_a, logs / "printinvariants_A.log")

        # ---- Phase B: a fresh JVM (never traceA)
        ran_b = load_ran(trace_b)
        if ran_b is not None:
            print(f"[INFO] reusing finished {trace_b}")
        else:
            print(f">>> Chicory phase B ({'full suite' if mode == 'normal' else 'null: phase A specs again'})")
            ran_b = run_chicory(daikon_jar, runner_classes, cp_runner, pkg_pattern, omit_pattern,
                                trace_b, work_dir, specs_b, logs / "chicory_B.log")
        info["verify_b"] = verify_tests(f"phase B ({mode})", ran_b, triggering, expect_present=(mode == "normal"))
        if mode == "null":
            verify_same_inventory(ran_a, ran_b, info["verify_b"].setdefault("null_inventory", {}))

        # ---- Check invA (frozen) against trace B, and against traceA (baseline)
        run_checks(daikon_jar, out_dir, inv_a, invariants_a, trace_a, trace_b, logs, info, args.max_violations)
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        write_atomic(out_dir / "run_info.json", json.dumps(info, indent=1))
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except (InfraError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
