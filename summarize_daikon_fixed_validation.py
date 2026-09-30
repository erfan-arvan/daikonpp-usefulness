#!/usr/bin/env python3
"""Compare Daikon's old text-diff hits, the checker's buggy FALSIFIED
candidates, and the hits validated against the fixed version
(validate_daikon_fixed.py).

Per bug:
  old       FALSIFIED records in outputs_usefulness/<P>_<B>/daikon_outcomes.jsonl
  checker   FALSIFIED on the buggy full run (checker, normal mode)
  valid     validated hits: HELD on traceA, not violated on the null trace,
            FALSIFIED on buggy, HELD on the fixed full run
  +trig     validated hits whose ppt the triggering tests reach on fixed
  failed-check counts, and what each old hit became
Bug level: exposed = at least one hit. Completed validations are listed
apart from incomplete / failed ones.

Usage:
    python3 summarize_daikon_fixed_validation.py [--bugs Cli_31,Cli_34,Cli_39]
        [--out-root outputs_daikon_fixed_validation] [--old-root outputs_usefulness] [--examples 5]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CHECKS = ("baseline_held", "null_not_violated", "buggy_falsified", "fixed_held")
SHORT = {"baseline_held": "base", "null_not_violated": "null", "buggy_falsified": "bug", "fixed_held": "fixed"}


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bugs", default="")
    ap.add_argument("--out-root", default="outputs_daikon_fixed_validation")
    ap.add_argument("--old-root", default="outputs_usefulness")
    ap.add_argument("--examples", type=int, default=5)
    args = ap.parse_args()
    root, old_root = Path(args.out_root), Path(args.old_root)
    bugs = [b.strip() for b in args.bugs.split(",") if b.strip()] or \
        sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []

    done, not_done = [], []
    for bug in bugs:
        d = root / bug
        info = json.loads((d / "run_info.json").read_text()) if (d / "run_info.json").is_file() else {}
        if (d / "VALIDATION_COMPLETE").is_file() and (d / "validation.jsonl").is_file():
            done.append((bug, read_jsonl(d / "validation.jsonl"), info))
        else:
            not_done.append((bug, info.get("error") or ("not run" if not d.is_dir() else "incomplete (running or killed)")))

    def old_hits(bug):
        f = old_root / bug / "daikon_outcomes.jsonl"
        return None if not f.is_file() else {(r["ppt"], r["invariant"]) for r in read_jsonl(f) if r["verdict"] == "FALSIFIED"}

    print("######## COMPLETED VALIDATIONS ########")
    hdr = "/".join(SHORT[c] for c in CHECKS)
    print(f'{"bug":<12}{"old":>5}{"checker":>9}{"valid":>7}{"+trig":>7}   failed checks ({hdr})   exposed: old/checker/valid')
    tot = {"old": 0, "checker": 0, "valid": 0}
    for bug, rows, info in done:
        old = old_hits(bug)
        valid = [r for r in rows if r["validated"]]
        trig = [r for r in valid if r["trigger_reached_on_fixed"]]
        failed = "/".join(str(sum(c in r["failed_checks"] for r in rows)) for c in CHECKS)
        exp = (("yes" if old else "no") if old is not None else "n/a", "yes" if rows else "no", "yes" if valid else "no")
        tot["old"] += bool(old)
        tot["checker"] += bool(rows)
        tot["valid"] += bool(valid)
        print(f"{bug:<12}{len(old) if old is not None else 'n/a':>5}{len(rows):>9}{len(valid):>7}{len(trig):>7}   "
              f"{failed:<24}   {'/'.join(exp)}")
    print(f"bugs exposed: old={tot['old']}  checker={tot['checker']}  fixed-validated={tot['valid']}  (of {len(done)})")
    print()

    print("######## INCOMPLETE / FAILED ########")
    for bug, why in not_done:
        print(f"{bug:<12} {why[:200]}")
    if not not_done:
        print("  (none)")
    print()

    print("######## DETAILS ########")
    for bug, rows, info in done:
        valid = [r for r in rows if r["validated"]]
        print(f"=== {bug}: {len(valid)} of {len(rows)} checker hits validated on the fixed version; "
              f"method map {info.get('method_status')}; checker errors {info.get('checker_errors') or '-'}")
        old = old_hits(bug)
        if old is not None:
            by = {(r["ppt"], r["invariant"]): r for r in rows}
            fate = {}
            for k in old:
                r = by.get(k)
                v = "not a checker hit" if r is None else ("validated" if r["validated"] else
                                                          "rejected: " + ",".join(SHORT[c] for c in r["failed_checks"]))
                fate[v] = fate.get(v, 0) + 1
            print(f"    old hits -> {dict(sorted(fate.items()))}")
        fixed = {}
        for r in rows:
            fixed[r["fixed"]["verdict"]] = fixed.get(r["fixed"]["verdict"], 0) + 1
        print(f"    fixed verdicts of checker hits: {fixed}")
        for r in valid[: args.examples]:
            bv = "; ".join(", ".join(f"{a}={b}" for a, b in s["values"].items()) for s in r["buggy"]["violating_samples"][:2])
            fs = r["fixed"]["samples_from_triggering_tests"] or r["fixed"]["samples_from_full_suite"]
            fv = "; ".join(", ".join(f"{a}={b}" for a, b in s.items()) for s in fs[:2]) or "(no direct samples: OBJECT/CLASS)"
            print(f"    VALID {r['ppt']}")
            print(f"        {r['invariant']}   (trigger reaches it on fixed: {r['trigger_reached_on_fixed']})")
            print(f"        buggy violating: {bv}")
            print(f"        fixed samples:   {fv}   [{r['fixed']['evaluations']} evaluations, HELD]")
        rej = [r for r in rows if not r["validated"]]
        for r in rej[: args.examples]:
            print(f"    rejected ({','.join(SHORT[c] for c in r['failed_checks'])}; fixed {r['fixed']['verdict']}"
                  f"{': ' + r['fixed']['reason'] if r['fixed'].get('reason') else ''}) {r['ppt']} :: {r['invariant']}")
        if len(rej) > args.examples:
            print(f"    ... {len(rej) - args.examples} more rejected (validation.jsonl)")
        print()


if __name__ == "__main__":
    main()
