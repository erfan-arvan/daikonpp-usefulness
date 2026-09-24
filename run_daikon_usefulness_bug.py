#!/usr/bin/env python3
"""Run the RQ5 (usefulness / property-based testing) experiment for ONE
Defects4J bug, using Daikon as the invariant-inference tool.

Daikon has no runtime "check this specific invariant" mode the way Oca's
injected probes do, so this reproduces the equivalent signal via trace
diffing (see daikon_diff_invariants.py for the full rationale):

  Phase A ("without_test"): run Chicory over every test EXCEPT the
    bug-revealing ("triggering") test method(s), producing traceA.dtrace.gz.
    Run Daikon on traceA alone -> invariantsA.txt. This is the candidate
    property set (mirrors Oca's without_test phase).

  Phase B ("bug_trace"): run Chicory over ONLY the triggering test
    method(s), on the SAME (unmodified) checkout, producing bug.dtrace.gz.
    Run Daikon on [traceA, bug.dtrace.gz] merged -> invariantsAB.txt.

  Diff: any invariant present in invariantsA but missing from invariantsAB
    was contradicted by a sample from the triggering test -- i.e. Daikon's
    equivalent of Oca's FALSIFIED verdict. Written to
    outputs_usefulness/<PROJECT>_<BUG>/daikon_outcomes.jsonl in the same
    {"ppt", "invariant", "verdict"} shape analyze_usefulness.py expects.

Unlike the Oca pipeline, no source files need to be modified here: Chicory
instruments classes at load time, so "run without the triggering test" just
means DaikonTestRunner never invokes that test method -- only one checkout
is needed per bug.

Usage:
    DAIKON_JAR=/path/to/daikon.jar python3 run_daikon_usefulness_bug.py <PROJECT> <BUG_ID>

Requires:
    - `defects4j` on PATH
    - `javac`/`java` on PATH (same JDK defects4j compile uses)
    - DAIKON_JAR env var pointing at a built daikon.jar
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_defects4j import (  # noqa: E402
    capture,
    d4j_env,
    derive_package_pattern,
    disable_test_method,  # noqa: F401  (unused here; kept for parity/reference)
    list_test_classes,
    parse_triggering_tests,
    run,
)
from daikon_diff_invariants import load_and_diff, parse_daikon_invariants  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
RUNNER_SRC = THIS_DIR / "DaikonTestRunner.java"

OMIT_PATTERN = r"junit\.|org\.junit\.|sun\.|java\.|com\.sun\.proxy"


def build_full_omit_pattern(test_classes: list[str]) -> str:
    """Extends OMIT_PATTERN to also exclude every test class actually being
    run under Chicory.

    The project's own test classes (e.g. org.apache.commons.cli.bug.BugCLI252Test)
    live under the SAME top-level package as the production code, so
    derive_package_pattern()'s --ppt-select-pattern matches them too, and
    OMIT_PATTERN alone only excludes JUnit/JDK internals -- not the
    project's own tests. Left unfixed, Daikon infers "invariants" over test
    FIXTURE state (the test class's own instance fields) instead of the
    actual program under test -- e.g. "this.options.requiredOpts has only
    one value" on a test class's own field, which trivially differs between
    Phase A and Phase B/bug runs for reasons having nothing to do with the
    bug (different test methods ran, populating that field differently),
    polluting the FALSIFIED count with noise instead of genuine catches.
    """
    if not test_classes:
        return OMIT_PATTERN
    test_class_pattern = "|".join(re.escape(c) for c in test_classes)
    return f"{OMIT_PATTERN}|{test_class_pattern}"


def checkout(project: str, version: str, work_dir: Path):
    if not work_dir.is_dir():
        run(
            ["defects4j", "checkout", "-p", project, "-v", version, "-w", str(work_dir)],
            env=d4j_env(),
        )


def compile_runner(cp_test: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["javac", "-cp", cp_test, "-d", str(out_dir), str(RUNNER_SRC)])
    return out_dir


def run_chicory(
    daikon_jar: str,
    runner_classes: Path,
    cp_test: str,
    pkg_pattern: str,
    omit_pattern: str,
    out_dtrace: Path,
    work_dir: Path,
    specs: list[str],
):
    if not specs:
        raise ValueError("no test specs given to Chicory")
    cp = f"{runner_classes}:{cp_test}:{daikon_jar}"
    # Chicory's --dtrace-file silently produces NO output file (no error either)
    # when given an absolute path -- confirmed against a real daikon.jar build.
    # Always pass a bare relative filename, let it land in the cwd (work_dir),
    # then move it to the real destination ourselves.
    tmp_name = f".chicory-{out_dtrace.name}"
    cmd = [
        "java",
        "-Xmx4g",
        "-cp",
        cp,
        "daikon.Chicory",
        f"--ppt-select-pattern={pkg_pattern}",
        f"--ppt-omit-pattern={omit_pattern}",
        f"--dtrace-file={tmp_name}",
        "DaikonTestRunner",
        *specs,
    ]
    run(cmd, cwd=work_dir)
    produced = work_dir / tmp_name
    if not produced.is_file():
        raise RuntimeError(
            f"Chicory did not produce {produced} (dtrace-file must be a relative "
            "path; check the command above ran from work_dir)"
        )
    out_dtrace.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(produced), str(out_dtrace))


def run_daikon(daikon_jar: str, dtrace_files: list[Path], out_inv: Path):
    cmd = [
        "java",
        "-Xmx4g",
        "-cp",
        daikon_jar,
        "daikon.Daikon",
        "--no_show_progress",
        "-o",
        str(out_inv),
        *[str(p) for p in dtrace_files],
    ]
    run(cmd)


def print_invariants(daikon_jar: str, inv_file: Path) -> str:
    cmd = ["java", "-cp", daikon_jar, "daikon.PrintInvariants", str(inv_file)]
    return capture(cmd, timeout=600)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("bug_id")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--pkg-pattern",
        default=None,
        help="override the auto-derived Chicory --ppt-select-pattern",
    )
    args = ap.parse_args()

    daikon_jar = os.environ.get("DAIKON_JAR")
    if not daikon_jar or not Path(daikon_jar).is_file():
        sys.exit(
            "ERROR: DAIKON_JAR must point at a built daikon.jar "
            f"(got: {daikon_jar!r})"
        )

    # By default defects4j swallows ant/maven build output on success (it's
    # captured via backticks and only printed on failure or when D4J_DEBUG is
    # set -- confirmed by reading Utils::exec_cmd in the defects4j source).
    # We need the real build/test logs for the experiment, so force it on for
    # every defects4j call this process or its children make.
    os.environ["D4J_DEBUG"] = "1"

    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    out_dir = (
        Path(args.out)
        if args.out
        else root / "outputs_usefulness" / f"{args.project}_{args.bug_id}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Defects4J's own install docs say v2.x requires Java 8, which may differ
    # from the JDK daikon.jar was built/needs to run under on this cluster's
    # `module load`. Set D4J_JAVA_HOME to pin the JDK used for every
    # `defects4j` call here; Chicory/Daikon/DaikonTestRunner below use
    # whatever's on PATH (matching submit_daikon.sh's own module load) --
    # this is safe even if that's a newer JDK than compiled the classes,
    # since a JVM runs older-targeted bytecode fine.
    d4j_subprocess_env = d4j_env()

    version = f"{args.bug_id}b"
    work_dir = root / "defects4j" / f"{args.project}-{version}_daikon"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    checkout(args.project, version, work_dir)

    run(["defects4j", "compile"], cwd=work_dir, env=d4j_subprocess_env)

    main_src = work_dir / capture(
        ["defects4j", "export", "-p", "dir.src.classes"], cwd=work_dir, env=d4j_subprocess_env
    ).strip()
    bin_tests = work_dir / capture(
        ["defects4j", "export", "-p", "dir.bin.tests"], cwd=work_dir, env=d4j_subprocess_env
    ).strip()
    cp_test = capture(
        ["defects4j", "export", "-p", "cp.test"], cwd=work_dir, env=d4j_subprocess_env
    ).strip()

    pkg_pattern = args.pkg_pattern or derive_package_pattern(str(main_src))
    print(f"[INFO] ppt-select-pattern = {pkg_pattern}")

    info = capture(
        ["defects4j", "info", "-p", args.project, "-b", args.bug_id], env=d4j_subprocess_env
    )
    triggering = parse_triggering_tests(info)
    if not triggering:
        sys.exit(
            f"ERROR: no triggering tests found for {args.project}-{args.bug_id}; "
            "cannot run the usefulness comparison (nothing to hold out / replay)"
        )
    print("[INFO] triggering tests:")
    for cls, meth in triggering:
        print(f"    {cls}::{meth}")

    trig_by_class: dict[str, list[str]] = {}
    for cls, meth in triggering:
        trig_by_class.setdefault(cls, []).append(meth)

    all_classes = list_test_classes(str(bin_tests))
    if not all_classes:
        sys.exit(f"ERROR: no compiled test classes found under {bin_tests}")

    omit_pattern = build_full_omit_pattern(all_classes)
    print(f"[INFO] ppt-omit-pattern (incl. {len(all_classes)} test classes) = {omit_pattern}")

    # Phase A specs: every test class, with triggering methods excluded from
    # whichever class(es) contain them.
    specs_without_bug = []
    for cls in all_classes:
        if cls in trig_by_class:
            excl = ",".join(f"!{m}" for m in trig_by_class[cls])
            specs_without_bug.append(f"{cls}::{excl}")
        else:
            specs_without_bug.append(cls)

    # Phase B specs: only the triggering method(s).
    specs_bug_only = [f"{cls}::{meth}" for cls, meth in triggering]

    runner_classes = compile_runner(cp_test, out_dir / "runner-classes")

    trace_a = out_dir / "traceA.dtrace.gz"
    trace_bug = out_dir / "traceBug.dtrace.gz"

    print("=" * 60)
    print(f">>> Chicory phase A (without triggering test): {args.project}-{args.bug_id}")
    print("=" * 60)
    run_chicory(
        daikon_jar, runner_classes, cp_test, pkg_pattern, omit_pattern, trace_a, work_dir, specs_without_bug
    )

    print("=" * 60)
    print(f">>> Chicory phase B (triggering test only): {args.project}-{args.bug_id}")
    print("=" * 60)
    run_chicory(
        daikon_jar, runner_classes, cp_test, pkg_pattern, omit_pattern, trace_bug, work_dir, specs_bug_only
    )

    inv_a = out_dir / "invA.inv.gz"
    inv_ab = out_dir / "invAB.inv.gz"

    print(">>> Daikon on trace A alone")
    run_daikon(daikon_jar, [trace_a], inv_a)

    print(">>> Daikon on trace A + bug trace")
    run_daikon(daikon_jar, [trace_a, trace_bug], inv_ab)

    text_a = print_invariants(daikon_jar, inv_a)
    text_ab = print_invariants(daikon_jar, inv_ab)
    (out_dir / "invariantsA.txt").write_text(text_a)
    (out_dir / "invariantsAB.txt").write_text(text_ab)

    before = parse_daikon_invariants(text_a)
    after = parse_daikon_invariants(text_ab)
    from daikon_diff_invariants import diff_invariants

    rows = diff_invariants(before, after)

    outcomes_path = out_dir / "daikon_outcomes.jsonl"
    import json

    with open(outcomes_path, "w") as f:
        for ppt, inv, verdict in rows:
            f.write(json.dumps({"ppt": ppt, "invariant": inv, "verdict": verdict}) + "\n")

    n_held = sum(1 for _, _, v in rows if v == "HELD")
    n_fals = sum(1 for _, _, v in rows if v == "FALSIFIED")
    print("=" * 60)
    print(f">>> DONE {args.project}-{args.bug_id}: total={len(rows)} held={n_held} falsified={n_fals}")
    print(f"    outcomes -> {outcomes_path}")
    print("=" * 60)

    shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
