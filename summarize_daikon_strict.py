#!/usr/bin/env python3
"""Existing fixed-version validation vs STRICT validation (test-attributed)
per bug, from validate_daikon_strict.py results. The two counts are kept
separate; candidates the fixed triggering test never evaluates are listed on
their own and are never strict hits.

Columns: checker = buggy-FALSIFIED candidates; existing = validated by
validate_daikon_fixed.py; strict = also violated during an unambiguous buggy
triggering-test window and evaluated >= 1 and HELD during the fixed
triggering-test windows; not-eval = existing + violated during the buggy
triggering test, but 0 evaluations in the fixed triggering windows.

Usage:
    python3 summarize_daikon_strict.py [--bugs Cli_31,...] [--out-root outputs_daikon_strict_validation] [--examples 5]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CATS = ("strict", "not_evaluated_by_fixed_trigger", "violated_by_fixed_trigger", "fixed_trigger_uncheckable",
        "not_violated_during_buggy_trigger", "attribution_unavailable_buggy", "attribution_unavailable_fixed",
        "not_existing_validated")


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def fmt(samples, key=None):
    out = []
    for s in samples[:2]:
        d = s[key] if key else s
        out.append(", ".join(f"{a}={b}" for a, b in d.items()))
    return "; ".join(out) or "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bugs", default="")
    ap.add_argument("--out-root", default="outputs_daikon_strict_validation")
    ap.add_argument("--examples", type=int, default=5)
    args = ap.parse_args()
    root = Path(args.out_root)
    bugs = [b.strip() for b in args.bugs.split(",") if b.strip()] or \
        (sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else [])

    done, not_done = [], []
    for bug in bugs:
        d = root / bug
        info = json.loads((d / "run_info.json").read_text()) if (d / "run_info.json").is_file() else {}
        if (d / "STRICT_COMPLETE").is_file() and (d / "strict.jsonl").is_file():
            done.append((bug, read_jsonl(d / "strict.jsonl"), info))
        else:
            not_done.append((bug, info.get("error") or ("not run" if not d.is_dir() else "incomplete (running or killed)")))

    print("######## EXISTING vs STRICT VALIDATION ########")
    print(f'{"bug":<12}{"checker":>8}{"existing":>9}{"strict":>7}{"not-eval":>9}   triggering windows buggy | fixed')
    exp_e = exp_s = 0
    for bug, rows, info in done:
        c = {k: sum(r["category"] == k for r in rows) for k in CATS}
        ex = sum(r["existing_validated"] for r in rows)
        exp_e += ex > 0
        exp_s += c["strict"] > 0
        wb = info.get("windows_Buggy", {}).get("per_test")
        wf = info.get("windows_Fixed", {}).get("per_test")
        print(f"{bug:<12}{len(rows):>8}{ex:>9}{c['strict']:>7}{c['not_evaluated_by_fixed_trigger']:>9}   {wb} | {wf}")
    print(f"bugs exposed: existing-validated={exp_e}  strict={exp_s}  (of {len(done)})")
    print()

    print("######## INCOMPLETE / FAILED ########")
    for bug, why in not_done:
        print(f"{bug:<12} {why[:200]}")
    if not not_done:
        print("  (none)")
    print()

    print("######## DETAILS ########")
    for bug, rows, info in done:
        cats = {k: v for k, v in info.get("categories", {}).items()}
        print(f"=== {bug}: categories {cats}")
        for label, cat in (("STRICT", "strict"), ("NOT EVALUATED BY FIXED TRIGGER (not counted)",
                                                  "not_evaluated_by_fixed_trigger")):
            sel = [r for r in rows if r["category"] == cat]
            for r in sel[: args.examples]:
                print(f"    {label}: {r['ppt']}")
                print(f"        {r['invariant']}")
                print(f"        buggy triggering test violates: {fmt(r['buggy_trigger']['violating_samples'], 'values')}"
                      f"  [{r['buggy_trigger']['violations']} of {r['buggy_trigger']['evaluations']}]")
                ft = r["fixed_trigger"]
                print(f"        fixed triggering test: {ft['verdict']}, {ft['evaluations']} evaluation(s)"
                      f"{' (' + ft['reason'] + ')' if ft.get('reason') else ''}; samples: {fmt(ft['samples'])}")
            if len(sel) > args.examples:
                print(f"    ... {len(sel) - args.examples} more {cat} (strict.jsonl)")
        other = [r for r in rows if r["existing_validated"] and r["category"] not in
                 ("strict", "not_evaluated_by_fixed_trigger")]
        for r in other[: args.examples]:
            print(f"    existing-validated but {r['category']}: {r['ppt']} :: {r['invariant']}")
        print()


if __name__ == "__main__":
    main()
