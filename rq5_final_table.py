#!/usr/bin/env python3
"""Final RQ5 table: per project, the relaxed-Oca hit rate and the Daikon hit
rate (new checker/fixed/strict pipeline) over each project's latest 5 bugs,
computed from the results on disk every time it runs.

A bug is a hit for
  relaxed Oca  if its run is confirmed complete (oca_status.py) and has >= 1
               true catch (held without the triggering test, falsified with
               it; rq5_check.compute_rq5) in outputs_usefulness_relaxed/;
  Daikon       if the new pipeline completed and found >= 1 strict hit. A bug
               whose old text-diff run (outputs_usefulness/<bug>/
               daikon_outcomes.jsonl) reported no FALSIFIED invariant is a
               completed miss without running the new pipeline.
Hit rate = hits / completed bugs. Bugs that are not completed (running,
failed, uncheckable, no result yet) are never counted as misses: they are
listed under the table with their state.

Usage:
    python3 rq5_final_table.py [--invariants] [--projects "Cli Codec ..."]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from check_daikon_catches import bugs_by_project
from oca_status import oca_status
from rq5_check import RunIncompleteError, compute_rq5
from summarize_daikon_strict_batch import classify, load, old_count

PROJECTS = "Cli Codec Collections Gson JxPath Math"
RELAXED = Path("outputs_usefulness_relaxed")
BATCH = Path("outputs_daikon_strict_batch")
PILOT_STRICT = Path("outputs_daikon_strict_validation")


def oca_result(bug):
    """('hit'|'miss', catches) or (state, None) when not countable."""
    d = RELAXED / bug
    try:
        catches = compute_rq5(d)["true_catches"]
    except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError) as e:
        state, why = oca_status(d)
        return (f"unreadable ({type(e).__name__})" if state == "complete" else f"{state}: {why}"), None
    return ("hit" if catches else "miss"), catches


def strict_dir(bug):
    st = load(BATCH / "status" / f"{bug}.json")
    if st and st.get("state") == "completed":
        return Path(st.get("results_dir") or BATCH) / "strict" / bug
    return PILOT_STRICT / bug


def daikon_result(bug):
    """('hit'|'miss', invariants) or (state, None) when not countable."""
    old = old_count(bug)
    if old is None:
        return "no old Daikon result", None
    if old == 0:
        return "miss", []
    group, _, note = classify(bug, BATCH, 6.0)
    if group in ("hit", "zero"):
        rows = [json.loads(l) for l in (strict_dir(bug) / "strict.jsonl").read_text().splitlines() if l.strip()]
        hits = [f'{r["ppt"]} :: {r["invariant"]}' for r in rows if r["category"] == "strict"]
        return ("hit" if hits else "miss"), hits
    names = {"uncheckable": "completed but uncheckable", "deferred": "new pipeline deferred",
             "failed": "new pipeline failed", "incomplete": "new pipeline running/incomplete",
             "not_started": "new pipeline not started"}
    return names[group] + (f" ({note})" if note else ""), None


def cell(results):
    done = [r for r in results if r[0] in ("hit", "miss")]
    hits = sum(r[0] == "hit" for r in done)
    s = f"{hits}/{len(done)}" + (f" ({hits / len(done):.0%})" if done else "")
    pending = len(results) - len(done)
    return s + (f" +{pending} pending" if pending else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--invariants", action="store_true", help="list each tool's falsified invariants per project and bug")
    ap.add_argument("--projects", default=PROJECTS)
    ap.add_argument("--csv", default="bugs_last10.csv,bugs.csv")
    args = ap.parse_args()
    projects = args.projects.replace(",", " ").split()
    bp = bugs_by_project(args.csv, 5, exclude=())

    table, details, pending = [], {}, []
    all_oca, all_dk = [], []
    for p in projects:
        bugs = [f"{p}_{b}" for b in bp.get(p, [])]
        oca = [(b, *oca_result(b)) for b in bugs]
        dk = [(b, *daikon_result(b)) for b in bugs]
        all_oca += [(s, c) for _, s, c in oca]
        all_dk += [(s, c) for _, s, c in dk]
        table.append((p, len(bugs), cell([(s, c) for _, s, c in oca]), cell([(s, c) for _, s, c in dk])))
        details[p] = (oca, dk)
        pending += [(b, "relaxed Oca", s) for b, s, _ in oca if s not in ("hit", "miss")]
        pending += [(b, "Daikon", s) for b, s, _ in dk if s not in ("hit", "miss")]

    w = max(len(p) for p in projects + ["TOTAL"])
    print(f'{"project":<{w}}  {"bugs":>4}  {"relaxed Oca hit rate":<26}  Daikon hit rate (new pipeline)')
    for p, n, o, d in table:
        print(f"{p:<{w}}  {n:>4}  {o:<26}  {d}")
    print(f'{"TOTAL":<{w}}  {sum(t[1] for t in table):>4}  {cell(all_oca):<26}  {cell(all_dk)}')

    if pending:
        print("\nNot counted (not completed):")
        for b, tool, s in pending:
            print(f"  {b:<16} {tool:<12} {s}")

    if args.invariants:
        for p in projects:
            oca, dk = details[p]
            print(f"\n######## {p} ########")
            for tool, rows in (("relaxed Oca", oca), ("Daikon", dk)):
                print(f"  -- {tool}")
                for b, s, invs in rows:
                    if s == "hit":
                        print(f"    {b}: {len(invs)} falsified")
                        for i in invs:
                            print(f"        {i}")
                    else:
                        print(f"    {b}: {s if s != 'miss' else 'none'}")


if __name__ == "__main__":
    main()
