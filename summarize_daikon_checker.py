#!/usr/bin/env python3
"""Per-bug summary of run_daikon_checker_bug.py results next to the old
text-diff results (daikon_diff_invariants.py).

For each bug:
  old    FALSIFIED count from outputs_usefulness/<P>_<B>/daikon_outcomes.jsonl,
         split into "ppt missing" (the ppt has no block at all in
         invariantsFull.txt) and "ppt present" (the ppt is printed, the
         invariant text is not)
  new    FALSIFIED / HELD / UNEXERCISED / UNCHECKABLE from
         <out-root>/normal/<P>_<B>/daikon_checker_outcomes.jsonl
  null   FALSIFIED from <out-root>/null/<P>_<B>/
  a few FALSIFIED invariants with their violating samples

Completed runs (CHECKER_COMPLETE present, including runs with zero results)
are listed apart from incomplete / not-run ones.

Usage:
    python3 summarize_daikon_checker.py [--bugs Cli_31,Cli_34,Cli_39] [--out-root outputs_daikon_checker]
                                        [--old-root outputs_usefulness] [--examples 3]
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path

from daikon_diff_invariants import parse_daikon_invariants

VERDICTS = ("FALSIFIED", "HELD", "UNEXERCISED", "UNCHECKABLE")


def read_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def old_result(old_dir: Path):
    """(n_falsified, ppt_missing, ppt_present, falsified keys) or None."""
    outcomes, full = old_dir / "daikon_outcomes.jsonl", old_dir / "invariantsFull.txt"
    if not outcomes.is_file():
        return None
    fals = [r for r in read_jsonl(outcomes) if r["verdict"] == "FALSIFIED"]
    if not full.is_file():
        return len(fals), None, None, {(r["ppt"], r["invariant"]) for r in fals}
    full_ppts = set(parse_daikon_invariants(full.read_text(errors="replace")))
    missing = sum(1 for r in fals if r["ppt"] not in full_ppts)
    return len(fals), missing, len(fals) - missing, {(r["ppt"], r["invariant"]) for r in fals}


def run_state(d: Path) -> tuple[str, list[dict] | None]:
    """('complete', outcomes) or (<why incomplete>, None)."""
    if not d.is_dir():
        return "not run", None
    if (d / "CHECKER_COMPLETE").is_file() and (d / "daikon_checker_outcomes.jsonl").is_file():
        return "complete", read_jsonl(d / "daikon_checker_outcomes.jsonl")
    info = d / "run_info.json"
    if info.is_file():
        err = json.loads(info.read_text()).get("error")
        if err:
            return f"failed: {err[:160]}", None
    logs = sorted((d / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime) if (d / "logs").is_dir() else []
    if logs:
        age = datetime.datetime.now().timestamp() - logs[-1].stat().st_mtime
        return f"incomplete (running or killed; last log {logs[-1].name}, {age / 60:.0f} min ago)", None
    return "incomplete (no logs yet)", None


def counts(outcomes: list[dict]) -> dict[str, int]:
    return {v: sum(1 for o in outcomes if o["verdict"] == v) for v in VERDICTS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bugs", default="", help="comma-separated <Project>_<Bug> (default: every bug found)")
    ap.add_argument("--out-root", default="outputs_daikon_checker")
    ap.add_argument("--old-root", default="outputs_usefulness")
    ap.add_argument("--examples", type=int, default=3, help="FALSIFIED invariants shown per bug")
    args = ap.parse_args()
    out_root, old_root = Path(args.out_root), Path(args.old_root)

    bugs = [b.strip() for b in args.bugs.split(",") if b.strip()]
    if not bugs:
        bugs = sorted({p.name for m in ("normal", "null") if (out_root / m).is_dir()
                       for p in (out_root / m).iterdir() if p.is_dir()})

    complete, incomplete = [], []
    for bug in bugs:
        state, outcomes = run_state(out_root / "normal" / bug)
        nstate, noutcomes = run_state(out_root / "null" / bug)
        row = (bug, state, outcomes, nstate, noutcomes, old_result(old_root / bug))
        (complete if outcomes is not None else incomplete).append(row)

    def old_cols(old):
        if old is None:
            return f'{"n/a":>6}{"":>9}{"":>9}'
        n, miss, pres = old[:3]
        return f"{n:>6}{'?' if miss is None else miss:>9}{'?' if pres is None else pres:>9}"

    print("######## COMPLETED (normal run finished; zero counts are real zeros) ########")
    print(f'{"bug":<16}{"old":>6}{"ppt-miss":>9}{"ppt-pres":>9} |{"FALS":>6}{"HELD":>7}{"UNEX":>6}{"UNCHK":>6} |{"null FALS":>10}')
    for bug, _, outcomes, nstate, noutcomes, old in complete:
        c = counts(outcomes)
        null = str(counts(noutcomes)["FALSIFIED"]) if noutcomes is not None else (
            "not run" if nstate == "not run" else "failed" if nstate.startswith("failed") else "incompl.")
        print(f"{bug:<16}{old_cols(old)} |{c['FALSIFIED']:>6}{c['HELD']:>7}{c['UNEXERCISED']:>6}{c['UNCHECKABLE']:>6} |{null:>10}")
    if not complete:
        print("  (none)")
    print("  old = daikon_outcomes.jsonl FALSIFIED (text diff); ppt-miss / ppt-pres = its ppt is absent from /")
    print("  present in invariantsFull.txt. FALS/HELD/UNEX/UNCHK = checker verdicts on the same kind of candidates.")
    print()

    print("######## INCOMPLETE / NOT RUN ########")
    for bug, state, _, nstate, noutcomes, old in incomplete:
        null = f"null: {'complete, FALSIFIED=' + str(counts(noutcomes)['FALSIFIED']) if noutcomes is not None else nstate}"
        print(f"{bug:<16} normal: {state} | {null}")
    for bug, _, _, nstate, noutcomes, _ in complete:
        if noutcomes is None:
            print(f"{bug:<16} normal: complete | null: {nstate}")
    if not incomplete and all(r[4] is not None for r in complete):
        print("  (none)")
    print()

    print("######## DETAILS ########")
    for bug, _, outcomes, _, noutcomes, old in complete:
        fals = [o for o in outcomes if o["verdict"] == "FALSIFIED"]
        print(f"=== {bug}: {len(outcomes)} candidates, {len(fals)} FALSIFIED")
        if old is not None:
            new_by_key = {(o["ppt"], o["invariant"]): o["verdict"] for o in outcomes}
            cross = {}
            for k in old[3]:
                v = new_by_key.get(k, "not a candidate in the new run")
                cross[v] = cross.get(v, 0) + 1
            print(f"    old FALSIFIED -> new verdict: {dict(sorted(cross.items())) or '-'}")
        reasons = {}
        for o in outcomes:
            if o["verdict"] == "UNCHECKABLE":
                reasons[o.get("reason", "?")] = reasons.get(o.get("reason", "?"), 0) + 1
        if reasons:
            print(f"    UNCHECKABLE reasons: {reasons}")
        for o in fals[: args.examples]:
            print(f"    {o['ppt']}")
            print(f"        {o['invariant']}   (violations={o['violations']} of {o['evaluations']} evaluations"
                  f"{', via OBJECT/CLASS propagation' if o.get('violations_direct') == 0 else ''})")
            for s in o.get("violating_samples", [])[:2]:
                vals = ", ".join(f"{k}={v}" for k, v in s["values"].items())
                print(f"        sample: {vals}   [{s['status']}, {Path(s['file'] or '').name}:{s['line']}]")
        if noutcomes is not None:
            nf = [o for o in noutcomes if o["verdict"] == "FALSIFIED"]
            print(f"    null run: {len(nf)} FALSIFIED of {len(noutcomes)} candidates")
            for o in nf[: args.examples]:
                print(f"        {o['ppt']} :: {o['invariant']}")
        print()


if __name__ == "__main__":
    main()
