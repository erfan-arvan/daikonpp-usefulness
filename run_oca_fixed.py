#!/usr/bin/env python3
"""Run Oca (RQ3, expressiveness) on the LATEST FIXED version of ONE
Defects4J project: the fixed revision ("<N>f") of the project's highest
active bug id, with its full, unmodified test suite.

Same Oca configuration as the RQ5 runs (run_usefulness_bug.py): few-shot
prompting, METHOD_BODY,SCOPE,CLASS_DOC context, DP_TEST_FILTER=0, the same
LLM model and the same per-project LLM cassette dir
(outputs_usefulness/_cassettes/<PROJECT>). Only one phase is run, with no
test disabled.

By default the LLM is REPLAY-ONLY (DP_DISABLE_REAL_LLM=1): every prompt is
answered from the RQ5 cassettes and no API call is ever made. A prompt with
no cassette entry (e.g. a method changed by the bug's fix) gets no
invariants -- Oca's LlmInvariantGenerator returns an empty list on a
replay miss. Set OCA_FIXED_ALLOW_LLM=1 to query the real LLM for misses
(and record them).

Usage:
    python3 run_oca_fixed.py <PROJECT> [--bug N] [--maxk N] [--out-root outputs_expressiveness] [--label fixed]

Output: <out-root>/<PROJECT>_<N>f/ (default outputs_expressiveness)
    daikonpp_registry_<label>.jsonl, daikonpp_outcomes_<label>.jsonl, <label>.log,
    <label>_run_logs/, RUN_COMPLETE (<label> defaults to "fixed")
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_defects4j import capture, d4j_env  # noqa: E402
from run_usefulness_bug import build_daikonpp, phase  # noqa: E402


def latest_bug_id(project: str) -> str:
    """Highest ACTIVE bug id of the project (`defects4j bids` lists only
    active, non-deprecated bugs)."""
    out = capture(["defects4j", "bids", "-p", project], env=d4j_env())
    ids = [int(x) for x in out.split() if x.strip().isdigit()]
    if not ids:
        raise SystemExit(f"ERROR: `defects4j bids -p {project}` returned no bug ids")
    return str(max(ids))


def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--bug", default=None, help="bug id to use instead of the latest one")
    ap.add_argument("--maxk", type=int, default=int(os.environ.get("MAXK", "5")))
    ap.add_argument("--out-root", default="outputs_expressiveness", help="results go to <out-root>/<PROJECT>_<N>f")
    ap.add_argument("--label", default="fixed",
                    help="phase label: names the result files and the defects4j/<PROJECT>-<N>f_<label> checkout")
    args = ap.parse_args()

    bug_id = args.bug or latest_bug_id(args.project)
    # Same as run_usefulness_bug.py: keep defects4j's build/test output.
    os.environ["D4J_DEBUG"] = "1"

    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    dpp_dir = Path(os.environ.get("DPP_DIR", root / "daikonplusplus")).resolve()

    out_dir = root / args.out_root / f"{args.project}_{bug_id}f"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "RUN_COMPLETE").unlink(missing_ok=True)

    # Shared with the RQ5 runs of the same project (see run_usefulness_bug.py).
    cassette_dir = root / "outputs_usefulness" / "_cassettes" / args.project
    cassette_dir.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    print(f"[SYSTEM] {args.project}-{bug_id}f started at {datetime.now().isoformat(timespec='seconds')}")
    jar = build_daikonpp(dpp_dir)

    print("=" * 60)
    print(f">>> Oca on latest fixed version: {args.project}-{bug_id}f")
    print("=" * 60)
    outcomes = phase(
        project=args.project,
        bug_id=bug_id,
        root=root,
        dpp_dir=dpp_dir,
        jar=jar,
        out_dir=out_dir,
        cassette_dir=cassette_dir,
        maxk=args.maxk,
        disable_bug_test=False,
        disable_real_llm=os.environ.get("OCA_FIXED_ALLOW_LLM") != "1",
        label=args.label,
        version_suffix="f",
    )

    (out_dir / "RUN_COMPLETE").write_text(f"{args.project}-{bug_id}f\n")
    print("=" * 60)
    print(f">>> DONE {args.project}-{bug_id}f: outcomes -> {outcomes}")
    print(f"[SYSTEM] elapsed {time.monotonic() - start:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
