#!/usr/bin/env python3
"""Checks each relaxed-Oca true catch (held without the triggering test,
falsified with it; rq5_check.compute_rq5 on outputs_usefulness_relaxed/<bug>)
against the run of the same Oca on that bug's FIXED version
(submit_oca_bugfixed_relaxed.sh -> outputs_oca_bugfixed_relaxed/<P>_<N>f,
label "bugfixed"). Invariants are matched on (kind, method, expression), the
same key compute_rq5 uses.

Verdict of a catch on the fixed version:
  HELD          confirmed: holds on the fix, so it is falsified by the bug
  FALSIFIED     refuted: also falsified on the fix, so not specific to the bug
  <other>       Oca's own verdict there (e.g. never executed, not compiled)
  not generated the fixed run has no such invariant (e.g. the fix changed the
                method, so the replayed cassette had no answer for it)
A bug is confirmed if >= 1 of its catches is HELD on the fixed version.

Usage:
    python3 check_oca_catches_on_fixed.py [--project Collections] [--bugs "24 25 26 27 28"] [--list]
    python3 check_oca_catches_on_fixed.py --bugs "Codec_15 Math_105" [--list]
    python3 check_oca_catches_on_fixed.py --hits [--list]         # every relaxed hit, see --hits
    python3 check_oca_catches_on_fixed.py --hits --print-bugs     # just the bug list, for BUGS=
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from check_daikon_catches import bugs_by_project
from rq5_check import RunIncompleteError, _load_verdicts, compute_rq5

RELAXED = Path("outputs_usefulness_relaxed")
FIXED = Path("outputs_oca_bugfixed_relaxed")
LABEL = "bugfixed"
PROJECTS = "Cli Codec Collections Gson JxPath Math"


def relaxed_hits(projects, csv="bugs_last10.csv,bugs.csv", last=5):
    """<P>_<N> of every latest-<last> bug whose relaxed run is complete with >= 1 true catch."""
    bp = bugs_by_project(csv, last, exclude=())
    hits = []
    for p in projects:
        for b in bp.get(p, []):
            try:
                if compute_rq5(RELAXED / f"{p}_{b}")["true_catches"]:
                    hits.append(f"{p}_{b}")
            except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError):
                pass
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="Collections")
    ap.add_argument("--bugs", default="24 25 26 27 28")
    ap.add_argument("--hits", action="store_true",
                    help=f"check every relaxed hit among the latest 5 bugs of --projects instead of --bugs")
    ap.add_argument("--projects", default=PROJECTS)
    ap.add_argument("--print-bugs", action="store_true", help="only print the bug list (space separated) and exit")
    ap.add_argument("--list", action="store_true", help="print every catch with its verdict on the fixed version")
    args = ap.parse_args()
    if args.hits:
        bugs = relaxed_hits(args.projects.replace(",", " ").split())
    else:
        bugs = [b if "_" in b else f"{args.project}_{b}" for b in args.bugs.replace(",", " ").split()]
    if args.print_bugs:
        print(" ".join(bugs))
        return

    print(f'{"bug":<16}{"catches":>8}{"HELD":>6}{"FALSIFIED":>10}{"other":>7}{"not gen":>9}  result')
    confirmed = checked = 0
    listing = []
    for bug in bugs:
        try:
            catches = compute_rq5(RELAXED / bug)["true_catches"]
        except (FileNotFoundError, json.JSONDecodeError, RunIncompleteError) as e:
            print(f"{bug:<16}  relaxed buggy run not usable: {type(e).__name__}")
            continue
        fdir = FIXED / f"{bug}f"
        if not (fdir / "RUN_COMPLETE").is_file():
            print(f"{bug:<16}{len(catches):>8}  fixed-version run not complete ({fdir})")
            continue
        fixed = _load_verdicts(fdir / f"daikonpp_registry_{LABEL}.jsonl", fdir / f"daikonpp_outcomes_{LABEL}.jsonl")
        verdicts = {k: fixed.get(k, "not generated") for k in catches}
        c = Counter(verdicts.values())
        other = sum(n for v, n in c.items() if v not in ("HELD", "FALSIFIED", "not generated"))
        checked += 1
        ok = c["HELD"] > 0
        confirmed += ok
        result = "confirmed" if ok else ("no catches" if not catches else "not confirmed")
        print(f'{bug:<16}{len(catches):>8}{c["HELD"]:>6}{c["FALSIFIED"]:>10}{other:>7}{c["not generated"]:>9}  {result}')
        listing.append((bug, verdicts))

    print(f"\nconfirmed bugs: {confirmed}/{checked} with a complete fixed-version run")
    if args.list:
        for bug, verdicts in listing:
            print(f"\n######## {bug}")
            for k, v in sorted(verdicts.items(), key=lambda kv: (kv[1], kv[0])):
                print(f"  {v:<14} {k}")


if __name__ == "__main__":
    main()
