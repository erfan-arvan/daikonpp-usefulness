#!/usr/bin/env python3
"""Run the RQ5 (usefulness / property-based testing) experiment from the
Oca paper for ONE Defects4J bug, using the current daikonplusplus CLI.

Reproduces the paper's section 4.5 procedure:

  Phase A ("without_test"): infer invariants on the BUGGY version with the
    bug-revealing ("triggering") test(s) disabled, so the LLM/inference never
    sees the failing input. Invariants that survive (are not falsified) are
    the candidate behavioral properties. LLM responses are recorded to a
    per-bug cassette directory.

  Phase B ("with_test"): re-run the SAME candidate invariants (replayed from
    the cassette recorded in Phase A -- no new LLM calls, so the exact same
    set of candidates is injected) against the ORIGINAL, unmodified test
    suite (bug-revealing test present). Any invariant that is FALSIFIED here
    was violated by the bug-triggering input, i.e. Oca "caught" the bug.

Differences from the old daikonppTests/run_daikonpp_defects4j_{with,without}_test.py:
  - Uses --external-project / --project-root / --main-src / --test-src /
    --runner-script exactly as the current App.java expects (unchanged).
  - Drops DP_REGISTRY_IN / DP_REGISTRY_READONLY, which no longer exist in
    DpConfig -- the "replay the same invariants" step is instead done the
    way the tool actually supports it: DP_LLM_CASSETTES record-then-replay
    (see llm/LlmInvariantGenerator.buildLlmFromEnv). Cassette keys are a hash
    of the (system, user) LLM prompt, which only depends on MAIN source +
    program point, so Phase A and Phase B propose identical invariants even
    though the TEST source differs between the two phases.
  - Disables the triggering test with a small regex/brace-matching pass
    (lib_defects4j.disable_test_method) instead of the missing RemoveMethod /
    RewriteMethodViaLLM JavaParser tool, so no extra jar/tool build is needed.
  - Turns on DP_TEST_FILTER=1 (test-driven side-effect isolation / delta
    debugging, section 3.3.4 of the paper) which the old scripts left off.

Usage:
    python3 run_usefulness_bug.py <PROJECT> <BUG_ID> [--maxk N] [--out DIR]

Environment (mirrors the promptstudy submit.sh on the NJIT HPC):
    ROOT            defaults to $PWD; expects $ROOT/daikonplusplus checked out
    OPENAI_API_KEY  required for Phase A (real LLM calls)
    MAXK            default 5 (max invariants proposed per program point)
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
    _ensure_svn_stub,
    capture,
    d4j_env,
    disable_test_method,
    find_test_file,
    force_rel_under,
    parse_triggering_tests,
    run,
)
from rq5_check import compute_rq5, format_summary, write_summary  # noqa: E402


def build_daikonpp(dpp_dir: Path) -> Path:
    # build.gradle's Java toolchain reads DP_JAVA_VERSION (default 17 if
    # unset -- see build.gradle's `toolchain { languageVersion = ... }`) and
    # requires an ACTUAL JDK of that exact version to be locally installed;
    # it does not just accept "17 or newer" already on PATH. This cluster
    # has no JDK 17 install (only the module-loaded 23 and the private 11
    # setup.sh provisions for Defects4J), so leaving DP_JAVA_VERSION unset
    # makes Gradle fail with "Cannot find a Java installation ... matching
    # this task's requirements: {languageVersion=17}". setup.sh works around
    # this by detecting the ambient JDK and exporting DP_JAVA_VERSION to
    # match it; do the same here for the identical `./gradlew shadowJar`
    # call this script makes on its own.
    env = os.environ.copy()
    java_home = env.get("DPP_JAVA_HOME")
    java_bin = f"{java_home}/bin/java" if java_home else "java"
    try:
        ver_out = subprocess.run(
            [java_bin, "-version"], capture_output=True, text=True
        ).stderr
        m = re.search(r'"(\d+)', ver_out)
        java_ver = m.group(1) if m else "17"
    except (OSError, subprocess.SubprocessError):
        java_ver = "17"
    env.setdefault("DP_JAVA_VERSION", java_ver)

    # No `clean` here: `shadowJar` depends on `test`, and `clean` wipes
    # Gradle's incremental-build cache, forcing `test` to actually re-run
    # every single build instead of being skipped as UP-TO-DATE. Your own
    # working run_prompt_study.sh builds with plain `./gradlew -q
    # shadowJar` (no clean) for exactly this reason -- match that here.
    run(["./gradlew", "-q", "shadowJar"], cwd=dpp_dir, env=env)
    jar = dpp_dir / "build" / "libs" / "daikonplusplus.jar"
    if not jar.exists():
        raise SystemExit(f"ERROR: {jar} missing after build")
    return jar


def write_runner(dpp_dir: Path, java_home: str | None) -> Path:
    runner = dpp_dir / "build" / f"d4j-runner-{os.getpid()}.sh"
    java_prefix = (
        f'export JAVA_HOME="{java_home}"\nexport PATH="$JAVA_HOME/bin:$PATH"\n\n'
        if java_home
        else ""
    )
    # `defects4j test`'s own stdout/stderr is what App.java's async reader
    # thread captures into $DP_RUN_LOG (it reads this whole script's merged
    # stdout+stderr stream, see JavaRunner.runExternalScript). Don't redirect
    # it anywhere else here: the old scripts this was ported from wrote it to
    # a "$D4J_ROOT/output.txt" that nothing ever populated, then cat'd that
    # empty file into $DP_RUN_LOG under a "=== Defects4J stdout ===" header --
    # that header always contained nothing, and directly appending to
    # $DP_RUN_LOG from here would race the reader thread that is
    # concurrently appending to the same file from the piped stdout.
    runner.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "set -x\n\n"
        f"{java_prefix}"
        "defects4j test || true\n\n"
        "sync\n"
        "sleep 1\n"
    )
    runner.chmod(0o755)
    return runner


def write_compile_script(dpp_dir: Path, java_home: str | None) -> Path:
    script = dpp_dir / "build" / f"d4j-compile-{os.getpid()}.sh"
    java_prefix = (
        f'export JAVA_HOME="{java_home}"\nexport PATH="$JAVA_HOME/bin:$PATH"\n'
        if java_home
        else ""
    )
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "set -x\n\n"
        'cd "$DP_PROJECT_ROOT"\n'
        f"{java_prefix}\n"
        "defects4j compile\n"
    )
    script.chmod(0o755)
    return script


def checkout(project: str, version: str, work_dir: Path):
    if not work_dir.is_dir():
        run(
            ["defects4j", "checkout", "-p", project, "-v", version, "-w", str(work_dir)],
            env=d4j_env(),
        )


def run_daikonpp(
    jar: Path,
    work_dir: Path,
    runner: Path,
    maxk: int,
    env: dict,
):
    main_src = capture(["defects4j", "export", "-p", "dir.src.classes"], cwd=work_dir).strip()
    test_src = capture(["defects4j", "export", "-p", "dir.src.tests"], cwd=work_dir).strip()
    main_src = force_rel_under(str(work_dir), main_src)
    test_src = force_rel_under(str(work_dir), test_src)

    cmd = [
        "java",
        "-Xmx4g",
        "-jar",
        str(jar),
        "--external-project",
        "--project-root",
        str(work_dir),
        "--main-src",
        main_src,
        "--test-src",
        test_src,
        "--runner-script",
        str(runner),
        str(maxk),
    ]
    subprocess.run(cmd, env=env, check=True)


def phase(
    *,
    project: str,
    bug_id: str,
    root: Path,
    dpp_dir: Path,
    jar: Path,
    out_dir: Path,
    cassette_dir: Path,
    maxk: int,
    disable_bug_test: bool,
    disable_real_llm: bool,
    label: str,
):
    version = f"{bug_id}b"
    work_dir = root / "defects4j" / f"{project}-{version}_{label}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    checkout(project, version, work_dir)

    if disable_bug_test:
        info = capture(["defects4j", "info", "-p", project, "-b", bug_id], env=d4j_env())
        triggering = parse_triggering_tests(info)
        if not triggering:
            print(f"[WARN] no triggering tests found for {project}-{bug_id}; "
                  "inference will see the bug-revealing input")
        test_src_rel = capture(
            ["defects4j", "export", "-p", "dir.src.tests"], cwd=work_dir, env=d4j_env()
        ).strip()
        for test_class, test_method in triggering:
            test_file = find_test_file(str(work_dir), test_src_rel, test_class)
            if not test_file:
                print(f"[WARN] could not locate {test_class} to disable {test_method}")
                continue
            ok = disable_test_method(test_file, test_method)
            print(f"[{'OK' if ok else 'WARN'}] disable {test_class}::{test_method} -> {test_file}")

    # daikonplusplus copies the project into DP_WORKDIR/project-<timestamp>/
    # and, in external-project mode, runs the test suite there via the
    # runner script. The runner script's stdout/stderr (i.e. the ACTUAL
    # `defects4j test` / ant / maven output, including every test's own
    # printed output) is written ONLY to <that copy>/daikonpp-run.log by
    # App.java's async reader thread -- it is never echoed to daikonplusplus's
    # own stdout (confirmed by reading JavaRunner.runExternalScript), so it
    # would otherwise never appear in this script's log/console output at
    # all. Point DP_WORKDIR at a known location under out_dir, force
    # DP_KEEP_WORK=1 so daikonplusplus doesn't delete it before we can copy
    # the run log out, then do our own cleanup below.
    dp_workdir = out_dir / f"dp_workdir_{label}"

    # Two separate environments, because Defects4J's own install docs say
    # v2.x requires Java 8, while daikonplusplus needs 17+ (its README) --
    # these are not always the same JDK on an HPC `module load`. Set
    # D4J_JAVA_HOME to pin the JDK used for every `defects4j` call (checkout,
    # compile, test-inside-the-runner-script, export, info); DPP_JAVA_HOME
    # (or just whatever's on PATH if unset) is used for the daikonplusplus
    # JVM itself. If your cluster's single `module load` JDK works for both,
    # leave both unset and everything just uses PATH as before.
    d4j_subprocess_env = d4j_env()
    d4j_subprocess_env["D4J_ROOT"] = str(root / "defects4j")

    env = os.environ.copy()
    dpp_java_home = env.get("DPP_JAVA_HOME")
    if dpp_java_home:
        env["JAVA_HOME"] = dpp_java_home
        env["PATH"] = f"{dpp_java_home}/bin:" + env.get("PATH", "")
    # daikonplusplus's own JVM (started with this `env`) shells out to
    # `defects4j compile` mid-run via the compile/runner scripts it invokes
    # (write_compile_script/write_runner below) -- those subprocesses
    # inherit THIS env, not d4j_subprocess_env above, so they need the same
    # svn stub d4j_env() adds, or they hit the identical Utils.pm crash.
    env["PATH"] = f"{_ensure_svn_stub()}:" + env.get("PATH", "")
    env["D4J_ROOT"] = str(root / "defects4j")
    env["DP_WORKDIR"] = str(dp_workdir)
    env["DP_KEEP_WORK"] = "1"
    env["DP_DISABLE_REAL_LLM"] = "1" if disable_real_llm else "0"
    env["DP_LLM_CASSETTES"] = str(cassette_dir)
    env["DP_TEST_FILTER"] = "1"
    # Oca config for this experiment: few-shot prompting, and context limited
    # to method body + in-scope variables/types + enclosing class javadoc
    # (DpConfig defaults to prompt strategy "baseline" and ALL 8 ContextKinds
    # if these are left unset).
    env["DP_PROMPT_STRATEGY"] = "fewshot"
    env["DP_CONTEXTS"] = "METHOD_BODY,SCOPE,CLASS_DOC"
    # DpConfig defaults DP_LLM_TOTAL_TIMEOUT_SEC to 180s and
    # DP_LLM_REQ_TIMEOUT_SEC to 45s if left unset -- far too small for a
    # whole file's worth of program points. Confirmed directly on Lang-65:
    # only 2316 of 3776 LLM tasks completed before the 180s total budget
    # ran out ("LLM phase timed out; proceeding with completed tasks"),
    # and the buggy method (DateUtils.truncate/modify) was among those
    # that never got queried at all -- zero candidates were ever proposed
    # for it, which is the root cause of RQ5 showing no catch for that
    # bug. Match the same values the working promptstudy scripts use.
    env["DP_LLM_TOTAL_TIMEOUT_SEC"] = "14400"
    env["DP_LLM_REQ_TIMEOUT_SEC"] = "30"
    env["DP_REGISTRY"] = str(out_dir / f"daikonpp_registry_{label}.jsonl")
    env["DP_OUTCOMES"] = str(out_dir / f"daikonpp_outcomes_{label}.jsonl")
    env["DP_REGISTRY_RESET"] = "true"

    run(["defects4j", "compile"], cwd=work_dir, env=d4j_subprocess_env)
    env["DP_EXTERNAL_COMPILE_CP"] = str(work_dir / "target" / "classes")

    runner = write_runner(dpp_dir, d4j_subprocess_env.get("JAVA_HOME"))
    compile_script = write_compile_script(dpp_dir, d4j_subprocess_env.get("JAVA_HOME"))
    env["DP_COMPILE_MAIN_SCRIPT"] = str(compile_script)
    env["DP_COMPILE_TEST_SCRIPT"] = str(compile_script)

    log_file = out_dir / f"{label}.log"
    print(f"[INFO] {label} log -> {log_file}")
    with open(log_file, "w") as logf:
        proc = subprocess.Popen(
            [
                "java",
                "-Xmx4g",
                "-jar",
                str(jar),
                "--external-project",
                "--project-root",
                str(work_dir),
                "--main-src",
                force_rel_under(
                    str(work_dir),
                    capture(
                        ["defects4j", "export", "-p", "dir.src.classes"],
                        cwd=work_dir,
                        env=d4j_subprocess_env,
                    ).strip(),
                ),
                "--test-src",
                force_rel_under(
                    str(work_dir),
                    capture(
                        ["defects4j", "export", "-p", "dir.src.tests"],
                        cwd=work_dir,
                        env=d4j_subprocess_env,
                    ).strip(),
                ),
                "--runner-script",
                str(runner),
                str(maxk),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            print(line, end="")
            logf.write(line)
        proc.wait()

    # Pull the real test-run log(s) out of the daikonplusplus working copy
    # before we reclaim the disk space it used. There is one daikonpp-run.log
    # per recovery iteration (archived as daikonpp-run-N.log by App.java on
    # each retry after a stale/timeout kill); grab all of them.
    run_logs_dir = out_dir / f"{label}_run_logs"
    run_logs_dir.mkdir(parents=True, exist_ok=True)
    copied_any = False
    for project_copy in sorted(dp_workdir.glob("project-*")):
        for run_log in sorted(project_copy.glob("daikonpp-run*.log")):
            shutil.copy2(run_log, run_logs_dir / run_log.name)
            copied_any = True
    if copied_any:
        print(f"[INFO] copied real defects4j/ant test-run log(s) -> {run_logs_dir}")
    else:
        print(f"[WARN] no daikonpp-run*.log found under {dp_workdir}; "
              "test-run output may be missing")

    shutil.rmtree(dp_workdir, ignore_errors=True)

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, "daikonplusplus")

    shutil.rmtree(work_dir, ignore_errors=True)

    return Path(env["DP_OUTCOMES"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("bug_id")
    ap.add_argument("--maxk", type=int, default=int(os.environ.get("MAXK", "5")))
    ap.add_argument(
        "--out",
        default=None,
        help="output dir (default: ./outputs_usefulness/<project>_<bug>)",
    )
    args = ap.parse_args()

    # By default defects4j swallows ant/maven build/test output on success
    # (captured via backticks in Utils::exec_cmd, only printed on failure or
    # when D4J_DEBUG is set -- confirmed by reading the defects4j source).
    # We need the real build/test logs, so force it on globally: run() calls
    # with env=None inherit this, and env dicts built from os.environ.copy()
    # below pick it up too.
    os.environ["D4J_DEBUG"] = "1"

    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    dpp_dir = Path(os.environ.get("DPP_DIR", root / "daikonplusplus")).resolve()

    out_dir = Path(args.out) if args.out else root / "outputs_usefulness" / f"{args.project}_{args.bug_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # out_dir is reused across re-runs of the same bug (nothing clears its
    # old registry/outcomes/log files), so if this is a re-run, remove the
    # PREVIOUS run's completion marker now, before Phase A starts
    # overwriting those files in place. Otherwise the marker would keep
    # claiming "done" for the whole overwrite window based on the old run.
    (out_dir / "RUN_COMPLETE").unlink(missing_ok=True)

    # Shared PER-PROJECT, not per-bug: DP_LLM_CASSETTES keys a recorded
    # response by a hash of the (system, user) prompt, which is derived
    # from MAIN source + program point -- and different bug IDs of the SAME
    # Defects4J project share almost all of their main source (they differ
    # only by that one bug's fix diff). A per-bug cassette dir would re-pay
    # for a near-identical LLM call on every single bug of a project;
    # sharing one cassette dir across all of a project's bugs means a
    # prompt already recorded for one bug's untouched-by-that-bug's-diff
    # method is reused free for every other bug of the same project.
    cassette_dir = root / "outputs_usefulness" / "_cassettes" / args.project
    cassette_dir.mkdir(parents=True, exist_ok=True)

    jar = build_daikonpp(dpp_dir)

    print("=" * 60)
    print(f">>> Phase A (without_test): {args.project}-{args.bug_id}")
    print("=" * 60)
    outcomes_a = phase(
        project=args.project,
        bug_id=args.bug_id,
        root=root,
        dpp_dir=dpp_dir,
        jar=jar,
        out_dir=out_dir,
        cassette_dir=cassette_dir,
        maxk=args.maxk,
        disable_bug_test=True,
        disable_real_llm=False,
        label="without_test",
    )

    print("=" * 60)
    print(f">>> Phase B (with_test): {args.project}-{args.bug_id}")
    print("=" * 60)
    outcomes_b = phase(
        project=args.project,
        bug_id=args.bug_id,
        root=root,
        dpp_dir=dpp_dir,
        jar=jar,
        out_dir=out_dir,
        cassette_dir=cassette_dir,
        maxk=args.maxk,
        disable_bug_test=False,
        disable_real_llm=True,
        label="with_test",
    )

    print("=" * 60)
    print(f">>> DONE {args.project}-{args.bug_id}")
    print(f"    without_test outcomes: {outcomes_a}")
    print(f"    with_test outcomes:    {outcomes_b}")
    print("=" * 60)

    print(">>> RQ5 (held-in-A -> falsified-in-B) analysis:")
    rq5_result = compute_rq5(out_dir)
    print(format_summary(rq5_result))
    summary_path = write_summary(out_dir, rq5_result)
    print(f"[INFO] RQ5 summary -> {summary_path}")

    # Written LAST, only once both phases and the RQ5 analysis have actually
    # finished. rq5_check.py's --all mode refuses to report on a bug_dir
    # missing this marker -- out_dir is reused across re-runs of the same
    # bug (nothing clears it), so a bug currently being re-run overwrites
    # its registry/outcomes files in place; a snapshot taken mid-overwrite
    # can still parse as valid, complete-looking JSON even though it's a
    # mix of the old finished run and a few freshly-appended records from
    # the new one in progress. Confirmed directly on Lang-65: rq5_check.py
    # reported a clean "0 true catches" result while defects4j checkout
    # for the SAME bug's new attempt was already fresh in
    # daikonpp_registry_without_test.jsonl.
    (out_dir / "RUN_COMPLETE").write_text(f"{args.project}-{args.bug_id}\n")


if __name__ == "__main__":
    main()
