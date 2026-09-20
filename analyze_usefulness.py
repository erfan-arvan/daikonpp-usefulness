#!/usr/bin/env python3
"""Summarize the RQ5 (usefulness) results across all bugs run so far, for
Oca and/or Daikon.

For each bug:
  - Oca:    daikonpp_outcomes_with_test.jsonl, verdict field, FALSIFIED =
            an invariant was violated by the bug-revealing input.
  - Daikon: daikon_outcomes.jsonl (from run_daikon_usefulness_bug.py),
            same {"verdict": "HELD"|"FALSIFIED"} shape via trace diffing.

Reports, per project and in aggregate, for each tool that has data:
  - Bug Exposure Rate: fraction of bugs with >=1 invariant violation.
  - Invariant Violation Count: total FALSIFIED invariants across bugs.

Usage:
    python3 analyze_usefulness.py [outputs_usefulness_dir]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def load_outcomes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summarize(base: Path, filename: str, label: str):
    per_project = defaultdict(lambda: {"bugs": 0, "exposed": 0, "violations": 0})
    rows = []

    for bug_dir in sorted(base.iterdir()):
        if not bug_dir.is_dir() or "_" not in bug_dir.name:
            continue
        project, bug_id = bug_dir.name.rsplit("_", 1)
        outcomes = load_outcomes(bug_dir / filename)
        if not outcomes:
            continue

        n_falsified = sum(1 for o in outcomes if o.get("verdict") == "FALSIFIED")
        exposed = n_falsified > 0

        per_project[project]["bugs"] += 1
        per_project[project]["violations"] += n_falsified
        if exposed:
            per_project[project]["exposed"] += 1

        rows.append((project, bug_id, len(outcomes), n_falsified, exposed))

    if not rows:
        print(f"(no {label} results found under {base})")
        return None

    print(f"=== {label} ===")
    print(f"{'project':<12} {'bug':<8} {'#invariants':<12} {'#violations':<12} exposed")
    for project, bug_id, n_inv, n_fals, exposed in rows:
        print(f"{project:<12} {bug_id:<8} {n_inv:<12} {n_fals:<12} {exposed}")

    print()
    print(f"{'project':<12} {'bugs':<6} {'exposed':<8} {'rate':<8} {'violations':<10}")
    total_bugs = total_exposed = total_violations = 0
    for project, s in sorted(per_project.items()):
        rate = s["exposed"] / s["bugs"] if s["bugs"] else 0.0
        print(f"{project:<12} {s['bugs']:<6} {s['exposed']:<8} {rate:<8.2%} {s['violations']:<10}")
        total_bugs += s["bugs"]
        total_exposed += s["exposed"]
        total_violations += s["violations"]

    print("-" * 50)
    overall_rate = total_exposed / total_bugs if total_bugs else 0.0
    print(f"{'TOTAL':<12} {total_bugs:<6} {total_exposed:<8} {overall_rate:<8.2%} {total_violations:<10}")
    print()
    return {"bugs": total_bugs, "exposed": total_exposed, "rate": overall_rate, "violations": total_violations}


def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("outputs_usefulness")
    if not base.is_dir():
        sys.exit(f"ERROR: {base} is not a directory")

    oca = summarize(base, "daikonpp_outcomes_with_test.jsonl", "Oca")
    daikon = summarize(base, "daikon_outcomes.jsonl", "Daikon")

    if oca and daikon:
        print("=== RQ5 comparison (paper section 4.5.5) ===")
        print(f"{'tool':<10} {'bugs':<6} {'exposed':<8} {'rate':<8} {'violations':<10}")
        print(f"{'Oca':<10} {oca['bugs']:<6} {oca['exposed']:<8} {oca['rate']:<8.2%} {oca['violations']:<10}")
        print(
            f"{'Daikon':<10} {daikon['bugs']:<6} {daikon['exposed']:<8} "
            f"{daikon['rate']:<8.2%} {daikon['violations']:<10}"
        )


if __name__ == "__main__":
    main()
