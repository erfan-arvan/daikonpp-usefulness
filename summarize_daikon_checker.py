#!/usr/bin/env python3
"""Per-bug summary of run_daikon_checker_bug.py results next to the old
text-diff results (daikon_diff_invariants.py).

For each bug:
  old    FALSIFIED count from outputs_usefulness/<P>_<B>/daikon_outcomes.jsonl,
         split into "ppt missing" (the ppt has no block at all in
         invariantsFull.txt) and "ppt present" (the ppt is printed, the
         invariant text is not)
  new    FALSIFIED / HELD / UNEXERCISED / UNEVALUATED_MISSING / UNCHECKABLE
         from <out-root>/normal/<P>_<B>/daikon_checker_outcomes.jsonl, with
         FALSIFIED split by where the violations came from (falsified_by):
         direct / unmatched ENTER / OBJECT-CLASS propagated / propagated
         from an unmatched ENTER
  null   FALSIFIED from <out-root>/null/<P>_<B>/, split the same way
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
from run_daikon_checker_bug import ORIGINS, VERDICTS

ORIGIN_SHORT = {"direct": "dir", "unmatched_entry": "unm", "propagated": "prop",
                "propagated_unmatched_entry": "p-unm"}


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


def by_origin(outcomes: list[dict]) -> dict[str, int]:
    return {o: sum(1 for x in outcomes if x.get("falsified_by") == o) for o in ORIGINS}


def origin_str(outcomes: list[dict]) -> str:
    b = by_origin(outcomes)
    return "/".join(str(b[o]) for o in ORIGINS)


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

    orig_hdr = "/".join(ORIGIN_SHORT[o] for o in ORIGINS)
    print("######## COMPLETED (normal run finished; zero counts are real zeros) ########")
    print(f'{"bug":<16}{"old":>6}{"ppt-miss":>9}{"ppt-pres":>9} |{"FALS":>6} {orig_hdr:>16}{"HELD":>7}{"UNEX":>6}'
          f'{"MISS":>6}{"UNCHK":>6} |{"null FALS":>10} {orig_hdr:>16}')
    for bug, _, outcomes, nstate, noutcomes, old in complete:
        c = counts(outcomes)
        if noutcomes is not None:
            null, null_orig = str(counts(noutcomes)["FALSIFIED"]), origin_str(noutcomes)
        else:
            null = "not run" if nstate == "not run" else "failed" if nstate.startswith("failed") else "incompl."
            null_orig = ""
        print(f"{bug:<16}{old_cols(old)} |{c['FALSIFIED']:>6} {origin_str(outcomes):>16}{c['HELD']:>7}"
              f"{c['UNEXERCISED']:>6}{c['UNEVALUATED_MISSING']:>6}{c['UNCHECKABLE']:>6} |{null:>10} {null_orig:>16}")
    if not complete:
        print("  (none)")
    print("  old = daikon_outcomes.jsonl FALSIFIED (text diff); ppt-miss / ppt-pres = its ppt is absent from /")
    print("  present in invariantsFull.txt. FALS/HELD/UNEX/MISS/UNCHK = checker verdicts (MISS = UNEVALUATED_MISSING:")
    print("  ppt reached, but every sample had a missing/out-of-bounds variable; UNEX = ppt never reached).")
    print(f"  {orig_hdr} = FALSIFIED by origin: direct (the stock checker's path) / unmatched ENTER (call never")
    print("  returned) / propagated to OBJECT-CLASS / propagated from an unmatched ENTER.")
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
            split = ", ".join(f"{ORIGIN_SHORT[g]}={o[f'violations_{g}']}" for g in ORIGINS if o[f"violations_{g}"])
            print(f"    {o['ppt']}")
            print(f"        {o['invariant']}   (falsified_by={o['falsified_by']}; violations={o['violations']} "
                  f"[{split}] of {o['evaluations']} evaluations)")
            for s in o.get("violating_samples", [])[:2]:
                vals = ", ".join(f"{k}={v}" for k, v in s["values"].items())
                print(f"        sample: {vals}   [{s['status']}, {s.get('origin')}, "
                      f"{Path(s['file'] or '').name}:{s['line']}]")
        if noutcomes is not None:
            nf = [o for o in noutcomes if o["verdict"] == "FALSIFIED"]
            print(f"    null run: {len(nf)} FALSIFIED of {len(noutcomes)} candidates, by origin {by_origin(noutcomes)}")
            for o in nf[: args.examples]:
                print(f"        {o['ppt']} :: {o['invariant']}   (falsified_by={o['falsified_by']})")
        print()


if __name__ == "__main__":
    main()
