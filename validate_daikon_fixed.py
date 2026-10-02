#!/usr/bin/env python3
"""Validate a bug's Daikon checker detections against the FIXED version.

Reuses, never rewrites, the checker pipeline's saved results for the bug
(run_daikon_checker_bug.py / recheck_daikon_checker.py, new format):
  normal/<P>_<B>/  invA.inv.gz, invariantsA.txt, traceA.dtrace.gz,
                   traceFull.dtrace.gz, daikon_checker_outcomes.jsonl (the
                   FALSIFIED verdicts), baseline_A/ outcomes, run_info.json
  null/<P>_<B>/    traceNull.dtrace.gz (only the trace: null mode's own,
                   separately inferred invA is NOT used)

Steps (no invariant is inferred here):
  1. Check out <B>f, compile, and assert with `defects4j test -t` that every
     triggering test passes on the fixed version.
  2. Chicory over the same full suite as the buggy phase B (the saved
     specs_b) -> traceFixed; and over only the triggering tests ->
     traceFixedTrigger. The runner's RUN/FAIL lines must show the triggering
     tests ran and did not fail.
  3. Map both fixed traces onto the buggy program points (daikon_trace_map):
     only identical declarations are kept; methods whose EXITnn line numbers
     shifted keep ENTER and the combined EXIT, but their EXITnn candidates
     are UNEXERCISED; changed / missing points are UNCHECKABLE. Never HELD.
  4. Check normal mode's frozen invA with the existing checker wrapper
     (DaikonCandidateChecker + stock InvariantChecker cross-check), with the
     saved baseline's inconsistent_with_inference_trace exclusions, against:
     the saved null trace, the mapped fixed trace, the mapped fixed
     triggering-only trace. A checker failure on a fixed trace is recorded
     and its candidates become UNCHECKABLE; it does not abort the others.

A candidate is a VALIDATED hit iff
  - FALSIFIED on the buggy full run (normal outcomes),
  - exercised and HELD on the saved phase-A baseline (traceA),
  - not violated on the saved null trace (0 eligible violations there),
  - exercised and HELD on the fixed full run (mapped, ppt status ok).
Also reported, not required: whether the triggering tests alone reach its
ppt on the fixed version (trigger_evaluations > 0) and it holds there.

Outputs in <out-root>/<P>_<B>/ (default outputs_daikon_fixed_validation):
traceFixed*.dtrace.gz (+ mapped), ppt_map.json, null_check/, fixed_check/,
fixed_trigger_check/, validation.jsonl (one row per buggy-FALSIFIED
candidate, with the violating values on buggy and samples on fixed),
run_info.json, logs/, and VALIDATION_COMPLETE (written last, only on
success).

Usage:
    DAIKON_JAR=... python3 validate_daikon_fixed.py <PROJECT> <BUG_ID> [--checker-root outputs_daikon_checker]
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
from daikon_trace_map import build_map, ppt_status, records, split, write_mapped  # noqa: E402
from lib_defects4j import capture, d4j_env, kill_current_subprocess, list_test_classes  # noqa: E402
from run_daikon_usefulness_bug import build_full_omit_pattern, checkout, find_junit4_jar, is_valid_gzip  # noqa: E402

COMPLETE = "VALIDATION_COMPLETE"
FAILING_TESTS = re.compile(r"Failing tests:\s*(\d+)")
FAIL_LINE = re.compile(r"^\[DaikonCheckerTestRunner\] FAIL (\S+)::(\S+?):")
N_SAMPLES = 3

_work_dir: Path | None = None


def _sigterm(signum, frame):
    kill_current_subprocess()
    rc.remove_work(_work_dir)
    sys.exit(143)


def ppt_class(ppt: str) -> str:
    """Top-level class of a Daikon ppt name, e.g. 'a.b.C$D.m(int):::EXIT7' -> 'a.b.C'."""
    name = ppt.split(":::")[0]
    if "(" in name:
        name = name[:name.index("(")].rsplit(".", 1)[0]
    return name.split("$")[0]


def narrow_pattern(ppts) -> str:
    """Chicory --ppt-select-pattern for just the classes (and their nested
    classes) of these ppts. Chicory includes a class or method when the
    pattern is found in its class, method or ppt name."""
    return "^(?:" + "|".join(sorted(re.escape(c) for c in {ppt_class(p) for p in ppts})) + r")\b"


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def last_run_lines(log: Path) -> list[str]:
    """Lines of the last invocation appended to a run_logged log."""
    text = log.read_text(errors="replace")
    return text.rsplit("\n===== ", 1)[-1].splitlines()


def extract_samples(trace: Path, wanted: dict[str, list[str]], k: int) -> dict[str, list[dict]]:
    """First k samples of each wanted ppt in a (mapped) trace, as {var: value}.
    ENTER / EXITnn: the record itself; combined EXIT: every EXITnn record of
    the method; orig(v) from the call's ENTER record. Other derived variables
    are marked "(derived)"; OBJECT/CLASS have no samples of their own."""
    out: dict[str, list[dict]] = {p: [] for p in wanted}
    by_method: dict[str, list[str]] = {}
    for p in wanted:
        by_method.setdefault(split(p)[0], []).append(p)
    enters: dict[tuple[str, str], dict] = {}

    def values(rec):
        nonce, body = (rec[2], rec[3:]) if len(rec) > 2 and rec[1] == "this_invocation_nonce" else (None, rec[1:])
        return nonce, {body[i]: body[i + 1] for i in range(0, len(body) - 2, 3)}

    for rec in records(trace):
        head = rec[0]
        if head.startswith(("ppt ", "decl-version", "var-comparability", "input-language", "#")):
            continue
        m, point = split(head)
        if m not in by_method:
            continue
        nonce, vals = values(rec)
        if point == "ENTER" and nonce is not None:
            enters[(m, nonce)] = vals
        for p in by_method[m]:
            pp = split(p)[1]
            hit = (pp == point) or (pp == "EXIT" and point.startswith("EXIT") and point != "EXIT")
            if not hit or len(out[p]) >= k:
                continue
            enter = enters.get((m, nonce), {}) if point != "ENTER" else {}
            row = {}
            for v in wanted[p]:
                mo = re.fullmatch(r"orig\((.*)\)", v)
                row[v] = vals[v] if v in vals else (enter.get(mo.group(1), "(missing)") if mo else "(derived)")
            out[p].append(row)
        if point != "ENTER" and nonce is not None:
            enters.pop((m, nonce), None)
    return out


def run_check(label, fn):
    """Runs a checker step; on failure returns (None, error) instead of raising."""
    try:
        return fn(), None
    except (subprocess.CalledProcessError, rc.InfraError, OSError, ValueError, KeyError) as e:
        print(f"[WARN] checker failed on {label}: {type(e).__name__}: {e}")
        return None, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("bug_id")
    ap.add_argument("--checker-root", default=None, help="default: $ROOT/outputs_daikon_checker")
    ap.add_argument("--out-root", default=None, help="default: $ROOT/outputs_daikon_fixed_validation")
    ap.add_argument("--max-violations", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--null-trace", default=None,
                    help="use this null trace (from make_null_trace.py; its directory must hold "
                         "NULL_TRACE_COMPLETE) instead of the checker's null/<P>_<B>/traceNull.dtrace.gz")
    ap.add_argument("--no-null", action="store_true",
                    help="3-step pipeline: no null trace and no null check")
    ap.add_argument("--narrow-trace", action="store_true",
                    help="trace the fixed version's full suite only in the classes of the buggy-FALSIFIED "
                         "candidates; with none, finish without tracing (3-step pipeline)")
    args = ap.parse_args()

    jar = os.environ.get("DAIKON_JAR")
    if not jar or not Path(jar).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at daikon.jar (got {jar!r})")
    os.environ["D4J_DEBUG"] = "1"
    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    croot = Path(args.checker_root).resolve() if args.checker_root else root / "outputs_daikon_checker"
    oroot = Path(args.out_root).resolve() if args.out_root else root / "outputs_daikon_fixed_validation"
    bug = f"{args.project}_{args.bug_id}"
    nd, ud, out = croot / "normal" / bug, croot / "null" / bug, oroot / bug
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    marker = out / COMPLETE
    if marker.exists() and not args.force:
        print(f"[INFO] {marker} exists -- already complete")
        return
    marker.unlink(missing_ok=True)

    # ---- saved inputs (read only)
    inv_a, invariants_a = nd / "invA.inv.gz", nd / "invariantsA.txt"
    ninfo0 = json.loads((nd / "run_info.json").read_text()) if (nd / "run_info.json").is_file() else {}
    # The buggy phase-B trace: the full suite, or (3-step) the triggering tests only.
    trace_a, trace_full, trace_null = nd / "traceA.dtrace.gz", nd / ninfo0.get("trace_b", "traceFull.dtrace.gz"), ud / "traceNull.dtrace.gz"
    null_done = ud / rc.COMPLETE
    if args.null_trace:
        trace_null = Path(args.null_trace).resolve()
        null_done = trace_null.parent / "NULL_TRACE_COMPLETE"
    baseline_checked = ninfo0.get("baseline_checked", True)
    need = [nd / rc.COMPLETE, inv_a, invariants_a, trace_a, trace_full,
            nd / "daikon_checker_outcomes.jsonl", nd / "run_info.json"]
    if baseline_checked:
        need.append(nd / "baseline_A" / "daikon_checker_outcomes.jsonl")
    if not args.no_null:
        need += [null_done, trace_null]
    missing = [str(p) for p in need if not p.is_file()]
    if missing:
        raise rc.InfraError(f"saved checker results missing (run/recheck the checker first): {missing}")
    ninfo = json.loads((nd / "run_info.json").read_text())
    if "requires_investigation" not in ninfo.get("baseline_A", {}):
        raise rc.InfraError(f"{nd} is in an old checker format; run recheck_daikon_checker.py first")
    stamp = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in need}

    normal = {(o["ppt"], o["invariant"]): o for o in read_jsonl(nd / "daikon_checker_outcomes.jsonl")}
    baseline = ({(o["ppt"], o["invariant"]): o for o in read_jsonl(nd / "baseline_A" / "daikon_checker_outcomes.jsonl")}
                if baseline_checked else {})
    baseline_bad = {(o["ppt"], o["invariant"]): o for o in ninfo["baseline_A"]["falsified"]}
    hits = sorted(k for k, o in normal.items() if o["verdict"] == "FALSIFIED")
    triggering = [tuple(t.split("::", 1)) for t in ninfo["triggering"]]
    info = {"project": args.project, "bug_id": args.bug_id, "daikon_jar": jar, "checker_normal_dir": str(nd),
            "checker_null_dir": None if args.no_null else str(ud), "null_trace": None if args.no_null else str(trace_null),
            "null_check": not args.no_null, "buggy_phase_b": ninfo.get("phase_b", "full"), "triggering": ninfo["triggering"], "buggy_falsified": len(hits),
            "baseline_checked": baseline_checked, "narrow_trace": args.narrow_trace,
            "started": datetime.datetime.now().isoformat()}
    if args.narrow_trace and not hits:
        # Nothing for the fixed version to confirm: no checkout, no traces.
        info.update({"validated_hits": 0, "validated_trigger_reached": 0, "skipped_fixed_run": "no buggy-FALSIFIED candidates",
                     "finished": datetime.datetime.now().isoformat()})
        rc.write_atomic(out / "validation.jsonl", "")
        rc.write_atomic(out / "run_info.json", json.dumps(info, indent=1))
        rc.write_atomic(marker, json.dumps({"validated_hits": 0, "buggy_falsified": 0}) + "\n")
        print(f">>> VALIDATED {bug}: 0 of 0 buggy-FALSIFIED candidates (nothing falsified; fixed run skipped)")
        return

    d4j = d4j_env()
    work_root = Path(os.environ.get("DAIKON_WORK_ROOT", root / "defects4j"))
    work_root.mkdir(parents=True, exist_ok=True)
    work = work_root / f"{args.project}-{args.bug_id}f_validate"
    shutil.rmtree(work, ignore_errors=True)
    global _work_dir
    _work_dir = work
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        # ---- 1. fixed version; triggering tests must pass
        checkout(args.project, f"{args.bug_id}f", work)
        rc.run_logged(["defects4j", "compile"], logs / "defects4j.log", cwd=work, env=d4j)
        test_results = {}
        for cls, meth in triggering:
            lines = rc.run_logged(["defects4j", "test", "-t", f"{cls}::{meth}"], logs / "defects4j_test.log",
                                  cwd=work, env=d4j)
            m = [FAILING_TESTS.search(l) for l in lines]
            m = [x for x in m if x]
            test_results[f"{cls}::{meth}"] = int(m[-1].group(1)) if m else None
        info["fixed_triggering_failing"] = test_results
        bad = {t: n for t, n in test_results.items() if n != 0}
        if bad:
            raise rc.InfraError(f"triggering tests do not pass on the fixed version: {bad}")
        print(f"[INFO] all {len(test_results)} triggering test(s) pass on {args.project}-{args.bug_id}f")

        bin_tests = work / capture(["defects4j", "export", "-p", "dir.bin.tests"], cwd=work, env=d4j).strip()
        cp_test = capture(["defects4j", "export", "-p", "cp.test"], cwd=work, env=d4j).strip()
        rc.ensure_test_classes(work, bin_tests, logs / "defects4j.log", d4j)
        classes = list_test_classes(str(bin_tests))
        # The fixed version always runs the full suite (paper step 3).
        specs = ninfo.get("full_suite") or ninfo["specs_b"]
        absent = sorted({s.split("::")[0] for s in specs} - set(classes))
        if absent:
            raise rc.InfraError(f"test classes of the buggy full suite missing on the fixed version: {absent}")
        pkg_pattern = narrow_pattern(k[0] for k in hits) if args.narrow_trace else ninfo["pkg_pattern"]
        info["fixed_ppt_select_pattern"] = pkg_pattern
        print(f"[INFO] fixed-version Chicory --ppt-select-pattern={pkg_pattern}")
        omit = build_full_omit_pattern(classes)
        cp_runner = f"{cp_test}:{find_junit4_jar()}"
        runner = out / "runner-classes"
        runner.mkdir(exist_ok=True)
        rc.run_logged(["javac", "-cp", cp_runner, "-d", str(runner), str(rc.RUNNER_SRC)], logs / "javac_runner.log")

        # ---- 2. fixed traces: same full suite, and triggering tests only
        fixed_traces = {}
        for tag, tspecs in (("Fixed", specs), ("FixedTrigger", [f"{c}::{m}" for c, m in triggering])):
            tr = out / f"trace{tag}.dtrace.gz"
            ran = rc.load_ran(tr)
            if ran is None:
                ran = rc.run_chicory(jar, runner, cp_runner, pkg_pattern, omit, tr, work, tspecs, logs / f"chicory_{tag}.log")
                fails = {f"{m.group(1)}::{rc.norm_method(m.group(2))}"
                         for m in map(FAIL_LINE.match, last_run_lines(logs / f"chicory_{tag}.log")) if m}
                (out / f"trace{tag}.fails.json").write_text(json.dumps(sorted(fails)))
            fails = set(json.loads((out / f"trace{tag}.fails.json").read_text()))
            info[f"verify_{tag}"] = rc.verify_tests(f"fixed {tag}", ran, triggering, expect_present=True)
            trig_failed = sorted(f"{c}::{m}" for c, m in triggering if f"{c}::{m}" in fails)
            info[f"verify_{tag}"]["triggering_failed_under_chicory"] = trig_failed
            if trig_failed:
                raise rc.InfraError(f"triggering tests failed under Chicory on the fixed version ({tag}): {trig_failed}")
            fixed_traces[tag] = tr

        # ---- 3. map fixed traces onto the buggy program points
        tm = build_map([trace_full, trace_a], fixed_traces["Fixed"])
        mapped = {}
        for tag, tr in fixed_traces.items():
            mt = out / f"trace{tag}.mapped.dtrace.gz"
            info[f"map_counts_{tag}"] = write_mapped(tr, mt, tm)
            mapped[tag] = mt
        rc.write_atomic(out / "ppt_map.json", json.dumps({**tm.to_json(), "counts": {t: info[f"map_counts_{t}"] for t in mapped}}, indent=1))
        info["method_status"] = {s: sum(1 for v in tm.method_status.values() if v == s) for s in sorted(set(tm.method_status.values()))}

        # ---- 4. frozen normal-mode invA against null, fixed, fixed-trigger traces
        cc = rc.compile_checker(jar, out / "checker-classes", logs / "javac_checker.log")
        text = invariants_a.read_text(errors="replace")
        results, errors = {}, {}
        targets = ([] if args.no_null else [("null", trace_null, out / "null_check")]) + [
            ("fixed", mapped["Fixed"], out / "fixed_check"),
            ("fixed_trigger", mapped["FixedTrigger"], out / "fixed_trigger_check")]
        for label, tr, dest in targets:
            res, err = run_check(label, lambda tr=tr, dest=dest, label=label: rc.check_one(
                jar, cc, inv_a, text, tr, dest, logs, f"_{label}", args.max_violations, baseline_bad))
            if res is not None:
                results[label] = {(o["ppt"], o["invariant"]): o for o in res.pop("_outcomes")}
                info[f"check_{label}"] = res
            else:
                errors[label] = err
        info["checker_errors"] = errors
        if "null" in errors:
            raise rc.InfraError(f"checker failed on the saved null trace: {errors['null']}")

        # ---- 5. evidence and verdicts per buggy-FALSIFIED candidate
        wanted = {}
        for k in hits:
            wanted.setdefault(k[0], [])
            for v in normal[k].get("vars", []):
                if v not in wanted[k[0]]:
                    wanted[k[0]].append(v)
        fixed_samples = {"trigger": extract_samples(mapped["FixedTrigger"], wanted, N_SAMPLES),
                         "full": extract_samples(mapped["Fixed"], wanted, N_SAMPLES)}
        rows = []
        for k in hits:
            ppt, inv = k
            b = normal[k]
            base = baseline.get(k, {})
            nul = {} if args.no_null else results["null"].get(k, {})
            st, forced, why = ppt_status(tm, ppt)
            fx = results.get("fixed", {}).get(k)
            if "fixed" in errors:
                fixed_verdict, fixed_reason = "UNCHECKABLE", f"checker failed on the fixed trace: {errors['fixed']}"
            elif forced:
                fixed_verdict, fixed_reason = forced, why
            elif fx is None:
                fixed_verdict, fixed_reason = "UNCHECKABLE", "no fixed-run outcome"
            else:
                fixed_verdict, fixed_reason = fx["verdict"], fx.get("reason")
            tg = results.get("fixed_trigger", {}).get(k, {}) if not forced else {}
            checks = {
                **({"baseline_held": base.get("verdict") == "HELD" and base.get("evaluations", 0) > 0}
                   if baseline_checked else {}),
                **({} if args.no_null else {"null_not_violated": bool(nul) and nul.get("violations", 1) == 0}),
                "buggy_falsified": b["verdict"] == "FALSIFIED",
                "fixed_held": fixed_verdict == "HELD" and (fx or {}).get("evaluations", 0) > 0,
            }
            vars_ = b.get("vars", [])
            rows.append({
                "ppt": ppt, "invariant": inv, "validated": all(checks.values()), **checks,
                "failed_checks": [c for c, ok in checks.items() if not ok],
                "ppt_map_status": st,
                "trigger_reached_on_fixed": tg.get("evaluations", 0) > 0,
                "trigger_held_on_fixed": tg.get("verdict") == "HELD",
                "baseline": {"verdict": base.get("verdict"), "evaluations": base.get("evaluations")},
                "null": {"verdict": nul.get("verdict"), "raw_verdict": nul.get("raw_verdict"),
                         "evaluations": nul.get("evaluations"), "violations": nul.get("violations"),
                         "reason": nul.get("reason")},
                "buggy": {"evaluations": b["evaluations"], "violations": b["violations"],
                          "falsified_by": b.get("falsified_by"), "violating_samples": b.get("violating_samples", [])},
                "fixed": {"verdict": fixed_verdict, "reason": fixed_reason,
                          "evaluations": (fx or {}).get("evaluations"), "violations": (fx or {}).get("violations"),
                          "violating_samples": (fx or {}).get("violating_samples", []),
                          "samples_from_triggering_tests": [{v: s.get(v) for v in vars_} for s in fixed_samples["trigger"].get(ppt, [])],
                          "samples_from_full_suite": [{v: s.get(v) for v in vars_} for s in fixed_samples["full"].get(ppt, [])]},
                "fixed_trigger": {"verdict": tg.get("verdict"), "evaluations": tg.get("evaluations"),
                                  "violations": tg.get("violations")},
            })
        rc.write_atomic(out / "validation.jsonl", "".join(json.dumps(r) + "\n" for r in rows))

        changed = [p for p in stamp if (Path(p).stat().st_size, Path(p).stat().st_mtime_ns) != stamp[p]]
        if changed:
            raise rc.InfraError(f"saved checker inputs changed during validation: {changed}")
        n_val = sum(r["validated"] for r in rows)
        info.update({"validated_hits": n_val, "validated_trigger_reached": sum(r["validated"] and r["trigger_reached_on_fixed"] for r in rows),
                     "failed_check_counts": {c: sum(c in r["failed_checks"] for r in rows)
                                             for c in (*(("baseline_held",) if baseline_checked else ()),
                                                       *(() if args.no_null else ("null_not_violated",)),
                                                       "buggy_falsified", "fixed_held")},
                     "fixed_verdicts": {v: sum(r["fixed"]["verdict"] == v for r in rows) for v in sorted({r["fixed"]["verdict"] for r in rows})},
                     "finished": datetime.datetime.now().isoformat()})
        rc.write_atomic(out / "run_info.json", json.dumps(info, indent=1))
        rc.write_atomic(marker, json.dumps({"validated_hits": n_val, "buggy_falsified": len(hits)}) + "\n")
        print("=" * 60)
        print(f">>> VALIDATED {bug}: {n_val} of {len(hits)} buggy-FALSIFIED candidates "
              f"({info['validated_trigger_reached']} reached by the triggering tests on fixed)")
        print(f"    failed checks: {info['failed_check_counts']}  fixed verdicts: {info['fixed_verdicts']}")
        print(f"    method map: {info['method_status']}  checker errors: {errors or '-'}")
        print("=" * 60)
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        rc.write_atomic(out / "run_info.json", json.dumps(info, indent=1))
        raise
    finally:
        rc.remove_work(work)


if __name__ == "__main__":
    try:
        main()
    except (rc.InfraError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
