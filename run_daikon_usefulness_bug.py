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

  Phase B ("full_suite"): run Chicory over the FULL unmodified test suite
    (every test, including the triggering one) as ONE natural execution,
    producing traceFull.dtrace.gz. Run Daikon on traceFull ALONE (no
    merge, no reuse of traceA's data) -> invariantsFull.txt. Running the
    triggering test isolated-then-merged (an earlier version of this
    script) confounded "the bug's effect" with "artifacts of running one
    test alone" (different static-init order, JVM warm-up state, etc. than
    it would ever have as part of the real suite); running the whole suite
    together avoids that.

  Diff: any invariant present in invariantsA but missing from invariantsFull
    was contradicted by a sample somewhere in the full-suite run -- i.e.
    Daikon's equivalent of Oca's FALSIFIED verdict. Written to
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
import gzip
import os
import re
import shutil
import signal
import subprocess
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_defects4j import (  # noqa: E402
    capture,
    d4j_env,
    derive_package_pattern,
    disable_test_method,  # noqa: F401  (unused here; kept for parity/reference)
    kill_current_subprocess,
    list_test_classes,
    parse_triggering_tests,
    run,
)
from daikon_diff_invariants import load_and_diff, parse_daikon_invariants  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
RUNNER_SRC = THIS_DIR / "DaikonTestRunner.java"

OMIT_PATTERN = r"junit\.|org\.junit\.|sun\.|java\.|com\.sun\.proxy"

# Confirmed too small: JacksonDatabind-111's Phase A trace hit a plain
# java.lang.OutOfMemoryError at -Xmx4g running daikon.Daikon (the trace
# itself was fine -- rerunning with more heap is the whole fix). Bumped for
# every Chicory/Daikon invocation, not just JacksonDatabind, since any
# project's trace can end up this size; override via DAIKON_JAVA_XMX if a
# cluster's node memory can't fit this.
JAVA_XMX = os.environ.get("DAIKON_JAVA_XMX", "12g")

# Set right after work_dir is computed in main(), read by _handle_sigterm.
# A SIGTERM (the soft-timeout signal submit_daikon.sh's `timeout` sends, or
# one forwarded from run_daikon_usefulness_batch.py's own SIGTERM handler)
# terminates a plain Python process immediately by default -- no exception is
# raised, so the try/finally in main() never runs and the checkout (plus any
# partial multi-GB trace file already written into it) is orphaned. Installing
# an explicit handler here turns that into a clean, in-process cleanup instead.
_work_dir_for_cleanup: Path | None = None

# The raw dtrace/inv files are only ever intermediate data -- once
# invariantsA.txt/invariantsFull.txt/daikon_outcomes.jsonl exist (or the run
# never got that far), nothing downstream reads them again. Set right after
# out_dir is known in main(), before anything can fail, so cleanup always has
# a fixed, correct list of paths to remove regardless of which phase crashed.
_trace_files_for_cleanup: list[Path] = []


def _cleanup_traces():
    for p in _trace_files_for_cleanup:
        p.unlink(missing_ok=True)


def _handle_sigterm(signum, frame):
    # Kill whatever's actually still running FIRST. A SIGTERM only unwinds
    # OUR Python stack (via sys.exit() below) -- the child process a run()
    # call is blocked on (e.g. a `java daikon.Chicory` mid-write of a
    # multi-GB temp trace into work_dir) is never itself told to stop, so
    # without this it's orphaned: it keeps running and keeps writing into a
    # directory whose entry we're about to delete, wasting invisible disk
    # space until SLURM eventually reaps the whole cgroup. Only the checkout
    # (work_dir) is removed here -- NOT the raw traces already moved into
    # out_dir by a phase that finished cleanly before this timeout hit; see
    # the SKIP checks in main() and _cleanup_traces()'s call site (only on
    # genuine success).
    kill_current_subprocess()
    if _work_dir_for_cleanup is not None:
        print(f"[INFO] caught SIGTERM -- cleaning up {_work_dir_for_cleanup} before exit", flush=True)
        shutil.rmtree(_work_dir_for_cleanup, ignore_errors=True)
    sys.exit(143)  # 128 + SIGTERM(15), the conventional exit code for this


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


def is_valid_gzip(path: Path) -> bool:
    """True if `path` is a complete, uncorrupted gzip stream.

    The "exists == fully completed" invariant Chicory/run_daikon's atomic
    writes are supposed to guarantee doesn't hold for trace/inv files left
    over from before that atomic-write logic existed -- confirmed directly:
    several bugs' traceA.dtrace.gz files decompressed into garbage/repeated
    bytes ("Bad modbit ... 6t6t6t6t...", "Mismatch ... context.cur8(uuuu...")
    that Daikon only detects when it actually tries to read them, at which
    point the SKIP-if-exists resume logic had already trusted the file and
    skipped regenerating it -- forever repeating the identical failure on
    every retry. Reading the whole stream through to EOF forces gzip's own
    CRC32/size trailer check, which reliably catches a truncated/corrupted
    write; raises here on any mismatch, treated as "not valid" by the caller.
    """
    try:
        with gzip.open(path, "rb") as f:
            while f.read(1 << 20):
                pass
        return True
    except (OSError, EOFError, zlib.error):
        # EOFError (truncated stream, missing end-of-stream marker) is NOT an
        # OSError subclass in Python's gzip module -- confirmed directly:
        # a gzip file cut off mid-stream raises a bare EOFError, which a
        # plain `except OSError` silently misses, defeating the whole point
        # of this check. BadGzipFile (bad CRC/size in the trailer) IS an
        # OSError subclass and was already covered. zlib.error (corrupted
        # DEFLATE data, e.g. "invalid code lengths set") is neither --
        # confirmed directly: Collections-27's corrupted trace raised a bare
        # zlib.error that an `except (OSError, EOFError)` alone let through
        # uncaught, crashing this whole script instead of correctly
        # detecting the corruption.
        return False


def checkout(project: str, version: str, work_dir: Path):
    if not work_dir.is_dir():
        run(
            ["defects4j", "checkout", "-p", project, "-v", version, "-w", str(work_dir)],
            env=d4j_env(),
        )


def find_junit4_jar() -> str:
    """Locates a JUnit 4 jar under the defects4j install, independent of
    whatever JUnit version the target project's own `cp.test` classpath
    pins.

    DaikonTestRunner.java always uses JUnit 4's org.junit.runner.* APIs
    (JUnitCore, Request, Filter, Description), regardless of which JUnit
    version the project under test was originally built against. Older
    Defects4J bug revisions (e.g. pre-migration Commons CLI) pin JUnit
    3.8.2 only, with no JUnit 4 classes anywhere on `cp.test` -- compiling
    DaikonTestRunner.java against that classpath alone fails outright with
    "package org.junit.runner does not exist", for every single bug on
    that lib version, confirmed via Cli-30..34.

    JUnit 4's JUnitCore can run legacy junit.framework.TestCase classes
    fine (it auto-wraps them via JUnit38ClassRunner), and defects4j's own
    compile.tests step already puts a JUnit 3.8.2 jar and Ant's bundled
    junit-4.12.jar on the same classpath together successfully -- so
    adding a JUnit 4 jar here for our own runner's compile/run classpath
    is safe and doesn't touch the project's own `defects4j compile` step.
    """
    d4j_bin = shutil.which("defects4j")
    if not d4j_bin:
        raise RuntimeError("defects4j not found on PATH")
    d4j_home = Path(d4j_bin).resolve().parent.parent  # .../framework/bin/defects4j -> D4J_HOME
    candidates = sorted(d4j_home.rglob("junit-4*.jar"))
    if not candidates:
        raise RuntimeError(f"no junit-4*.jar found under {d4j_home}")
    return str(candidates[0])


def compile_runner(cp_runner: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["javac", "-cp", cp_runner, "-d", str(out_dir), str(RUNNER_SRC)])
    return out_dir


def run_chicory(
    daikon_jar: str,
    runner_classes: Path,
    cp_runner: str,
    pkg_pattern: str,
    omit_pattern: str,
    out_dtrace: Path,
    work_dir: Path,
    specs: list[str],
):
    if not specs:
        raise ValueError("no test specs given to Chicory")
    cp = f"{runner_classes}:{cp_runner}:{daikon_jar}"
    # Chicory's --dtrace-file silently produces NO output file (no error either)
    # when given an absolute path -- confirmed against a real daikon.jar build.
    # Always pass a bare relative filename, let it land in the cwd (work_dir),
    # then move it to the real destination ourselves.
    tmp_name = f".chicory-{out_dtrace.name}"
    cmd = [
        "java",
        f"-Xmx{JAVA_XMX}",
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
    # Unlike Chicory (which writes to a temp name in work_dir and only moves
    # the result into out_dir on success), Daikon's -o writes DIRECTLY to the
    # final path -- if this process crashes or is killed mid-write, a
    # partial/corrupt .inv.gz would be left at exactly the path main()'s
    # skip-if-exists check tests, causing the next attempt to wrongly reuse
    # a broken file instead of redoing this step. Writing to a temp name and
    # renaming only after run() returns (i.e. only on a clean exit) gives
    # this the same "exists == fully completed" guarantee Chicory already has.
    tmp_out = out_inv.with_name(out_inv.name + ".tmp")
    tmp_out.unlink(missing_ok=True)
    cmd = [
        "java",
        f"-Xmx{JAVA_XMX}",
        "-cp",
        daikon_jar,
        "daikon.Daikon",
        "--no_show_progress",
        # Disables Daikon's implication/splitter generation entirely
        # (daikon.split.PptSplitter.add_implications_pair). Confirmed root
        # cause of a real crash class: "RuntimeException: found eq_inv ...
        # but can't find slice" inside that method, hit on Math-104/105's
        # BigMatrixImpl.isSquare boolean-return EXIT split. This isn't
        # specific to that one class -- it's a general Daikon bug in
        # reconciling equality invariants across an implicit true/false EXIT
        # split, so any project with a boolean-returning method can trigger
        # it. Applied globally (not just for Math) since it's a no-op for
        # any run that never hits this path, and implication invariants
        # aren't part of what this RQ5 comparison's ppt/invariant diffing
        # (daikon_diff_invariants.py) is measuring in the first place --
        # Oca has no equivalent invariant category to compare against.
        #
        # NOTE the field name here has NO "dkconfig_" prefix: Daikon's
        # Configuration.apply() splits this string at the LAST dot and
        # prepends "dkconfig_" itself when looking up the field (see
        # daikon.config.Configuration.PREFIX) -- passing the prefix here too
        # doubles it into "dkconfig_dkconfig_disable_splitting", which
        # doesn't exist. Confirmed directly: the first version of this fix
        # (with the prefix included) failed every single run with "Unknown
        # configuration option daikon.split.PptSplitter.dkconfig_disable_..."
        "--config_option",
        "daikon.split.PptSplitter.disable_splitting=true",
        "-o",
        str(tmp_out),
        *[str(p) for p in dtrace_files],
    ]
    run(cmd)
    tmp_out.rename(out_inv)


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

    trace_a = out_dir / "traceA.dtrace.gz"
    trace_full = out_dir / "traceFull.dtrace.gz"
    inv_a = out_dir / "invA.inv.gz"
    inv_full = out_dir / "invFull.inv.gz"
    global _trace_files_for_cleanup
    _trace_files_for_cleanup = [trace_a, trace_full, inv_a, inv_full]

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

    global _work_dir_for_cleanup
    _work_dir_for_cleanup = work_dir
    signal.signal(signal.SIGTERM, _handle_sigterm)

    checkout(args.project, version, work_dir)

    # Everything from here on (compile, both Chicory phases, Daikon
    # inference) writes into work_dir -- including, for a large project's
    # full-suite trace, tens of GB of temp .chicory-*.dtrace.gz data before
    # it's moved out to out_dir on success. Without this try/finally, ANY
    # exception here (a disk-quota error, a compile failure, a killed job)
    # skipped the cleanup below entirely and left the checkout PLUS that
    # partial multi-GB trace file orphaned in defects4j/ forever -- exactly
    # what silently grew that directory to 1.3TB over repeated failed/killed
    # runs across this experiment's lifetime.
    try:
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

        # Phase B specs: the FULL unmodified test suite (every class, nothing
        # excluded, including the triggering test) -- run as ONE natural
        # execution, not the triggering test in isolation. Running the
        # triggering test alone in its own JVM (the old design) and then
        # merging that isolated trace with trace_a at the Daikon level
        # confounds "the bug's actual effect" with "artifacts of running this
        # one test alone" (different static-init order, different JVM warm-up
        # state, different execution context than it would ever naturally have
        # as part of the real suite). Running the whole suite together instead
        # gives the triggering test its normal execution context, and Daikon
        # infers invFull directly from that single trace -- no merge, no reuse
        # of trace_a's data at all.
        specs_full_suite = list(all_classes)

        junit4_jar = find_junit4_jar()
        print(f"[INFO] junit4 jar (for DaikonTestRunner, independent of project's own JUnit version) = {junit4_jar}")
        cp_runner = f"{cp_test}:{junit4_jar}"

        runner_classes = compile_runner(cp_runner, out_dir / "runner-classes")

        # Each of these four steps is skipped if its output already exists --
        # a prior attempt at this exact bug may have completed one or more
        # phases before being killed by a timeout (or crashing partway
        # through a LATER phase). A trace/inv file only ever exists in
        # out_dir once its producing step has fully finished (Chicory writes
        # to a temp name in work_dir first, moving it here only on success;
        # run_daikon's -o target is likewise only valid once it returns) --
        # so reusing one here is safe, and for a large project (Closure's
        # phases can each take an hour+) this can save most of a retry's
        # runtime instead of redoing already-finished work from scratch.
        if trace_a.exists() and not is_valid_gzip(trace_a):
            print(f"[WARN] {trace_a} exists but is not a valid gzip stream (corrupted/truncated "
                  "leftover from a prior attempt) -- deleting so Chicory phase A reruns")
            trace_a.unlink()
        if trace_a.exists():
            print(f"[INFO] SKIP Chicory phase A -- {trace_a} already exists from a prior attempt")
        else:
            print("=" * 60)
            print(f">>> Chicory phase A (without triggering test): {args.project}-{args.bug_id}")
            print("=" * 60)
            run_chicory(
                daikon_jar, runner_classes, cp_runner, pkg_pattern, omit_pattern, trace_a, work_dir, specs_without_bug
            )

        if trace_full.exists() and not is_valid_gzip(trace_full):
            print(f"[WARN] {trace_full} exists but is not a valid gzip stream (corrupted/truncated "
                  "leftover from a prior attempt) -- deleting so Chicory phase B reruns")
            trace_full.unlink()
        if trace_full.exists():
            print(f"[INFO] SKIP Chicory phase B -- {trace_full} already exists from a prior attempt")
        else:
            print("=" * 60)
            print(f">>> Chicory phase B (full unmodified suite): {args.project}-{args.bug_id}")
            print("=" * 60)
            run_chicory(
                daikon_jar, runner_classes, cp_runner, pkg_pattern, omit_pattern, trace_full, work_dir, specs_full_suite
            )

        if inv_a.exists() and not is_valid_gzip(inv_a):
            print(f"[WARN] {inv_a} exists but is not a valid gzip stream (corrupted/truncated "
                  "leftover from a prior attempt) -- deleting so Daikon reruns on trace A")
            inv_a.unlink()
        if inv_a.exists():
            print(f"[INFO] SKIP Daikon on trace A -- {inv_a} already exists from a prior attempt")
        else:
            print(">>> Daikon on trace A alone")
            run_daikon(daikon_jar, [trace_a], inv_a)

        if inv_full.exists() and not is_valid_gzip(inv_full):
            print(f"[WARN] {inv_full} exists but is not a valid gzip stream (corrupted/truncated "
                  "leftover from a prior attempt) -- deleting so Daikon reruns on the full-suite trace")
            inv_full.unlink()
        if inv_full.exists():
            print(f"[INFO] SKIP Daikon on the full-suite trace -- {inv_full} already exists from a prior attempt")
        else:
            print(">>> Daikon on the full-suite trace alone (no merge with trace A)")
            run_daikon(daikon_jar, [trace_full], inv_full)

        text_a = print_invariants(daikon_jar, inv_a)
        text_ab = print_invariants(daikon_jar, inv_full)
        (out_dir / "invariantsA.txt").write_text(text_a)
        (out_dir / "invariantsFull.txt").write_text(text_ab)

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

        # Only clean up the raw traces on a genuine success (outcomes_path
        # written) -- on a crash/timeout, a phase that already finished
        # should stay on disk so a retry can skip it (see the SKIP checks
        # above), rather than being deleted and redone from scratch.
        _cleanup_traces()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
