#!/usr/bin/env python3
"""Batch summary for run_daikon_strict_batch_bug.py: old text-diff, checker,
fixed-validated and strict detection per bug, with completed zero-hit bugs
reported apart from uncheckable, deferred, failed, incomplete and not-started
ones. Reads only status files and saved (possibly trace-cleaned) results;
never needs or regenerates traces.

A bug is strictly detected iff its strict stage reports >= 1 strict hit.
A completed bug with 0 strict hits is UNCHECKABLE (not a zero result) if any
checker error occurred on the fixed/window traces, if triggering-test
attribution was unavailable, if a validated candidate could not be checked
on the fixed triggering tests, or if none of its checker hits could be
checked on the fixed version at all (all UNCHECKABLE/UNEXERCISED there).

The default scope is the dataset: the latest --last (5) bugs of each project
(bugs_last10.csv + bugs.csv) without check_daikon_catches.DROPPED_PROJECTS,
restricted to the bugs whose old text-diff run reported >= 1 FALSIFIED
invariant. Pilot results (outputs_daikon_checker/normal,
outputs_daikon_fixed_validation, outputs_daikon_strict_validation) are used
for in-scope bugs that have no batch status. Bugs whose old run has not
finished are listed apart. --extra shows other bugs (e.g. pilot bugs outside
the scope) in a separate section that is never in the totals.

Usage:
    python3 summarize_daikon_strict_batch.py [--last 5] [--include-dropped] [--bugs "Codec_17 Gson_18 ..."]
        [--results-root outputs_daikon_strict_batch] [--extra "Cli_31 Cli_34"]
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path

from check_daikon_catches import DROPPED_PROJECTS, bugs_by_project

UNCHECKABLE_CATS = ("fixed_trigger_uncheckable", "attribution_unavailable_buggy", "attribution_unavailable_fixed")


def load(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def old_count(bug):
    f = Path("outputs_usefulness") / bug / "daikon_outcomes.jsonl"
    return sum('"FALSIFIED"' in l for l in f.read_text().splitlines()) if f.is_file() else None


def row_from_dirs(bug, checker_dir: Path, fixed_dir: Path, strict_dir: Path):
    ci, vi, si = load(checker_dir / "run_info.json"), load(fixed_dir / "run_info.json"), load(strict_dir / "run_info.json")
    if not (ci and vi and si and (strict_dir / "STRICT_COMPLETE").is_file()):
        return None
    return {"old_falsified": old_count(bug), "checker_falsified": ci["counts"]["FALSIFIED"],
            "fixed_validated": vi["validated_hits"], "strict": si["strict_hits"],
            "strict_categories": si.get("categories", {}), "fixed_verdicts": vi.get("fixed_verdicts", {}),
            "checker_errors": {**(vi.get("checker_errors") or {}), **(si.get("checker_errors") or {})},
            "baseline_falsified": ci["baseline_A"]["counts"]["FALSIFIED"]}


def uncheckable_reason(r):
    if r["strict"] > 0:
        return None
    if r["checker_errors"]:
        return f"checker errors: {sorted(r['checker_errors'])}"
    bad = {c: n for c, n in r["strict_categories"].items() if c in UNCHECKABLE_CATS}
    if bad:
        return f"strict categories {bad}"
    n = r["checker_falsified"]
    fv = r["fixed_verdicts"]
    if n and fv.get("UNCHECKABLE", 0) + fv.get("UNEXERCISED", 0) == n:
        return f"none of the {n} checker hits checkable on the fixed version ({fv})"
    return None


def classify(bug, res: Path, stale_hours: float):
    """(group, row, note) from the batch status, else from the pilot roots."""
    st = load(res / "status" / f"{bug}.json")
    if st is None:
        r = row_from_dirs(bug, Path("outputs_daikon_checker/normal") / bug,
                          Path("outputs_daikon_fixed_validation") / bug, Path("outputs_daikon_strict_validation") / bug)
        if r is None:
            return "not_started", {"old_falsified": old_count(bug)}, None
        r["source"] = "pilot"
    elif st.get("state") == "completed":
        base = Path(st.get("results_dir") or res)
        r = row_from_dirs(bug, base / "checker" / "normal" / bug, base / "fixed_validation" / bug, base / "strict" / bug)
        if r is None:
            return "incomplete", st, "status completed but results missing under " + str(base)
        r["source"] = "batch" + (" (traces cleaned)" if st.get("traces_cleaned") else "")
    elif st.get("state") == "deferred":
        return "deferred", st, f"free {st.get('free_gb')} GB < {st.get('required_gb')} GB"
    elif st.get("state") == "failed":
        return "failed", st, f"stage {st.get('failed_stage')} rc={st.get('exit_code')}: {(st.get('error') or '')[:160]}"
    else:
        age = ""
        try:
            h = (datetime.datetime.now() - datetime.datetime.fromisoformat(st["updated"])).total_seconds() / 3600
            age = f"last update {h:.1f} h ago" + (" (STALE: killed or timed out?)" if h > stale_hours else "")
        except (KeyError, ValueError):
            pass
        done = [s for s, v in (st.get("stages_done") or {}).items() if v]
        return "incomplete", st, f"{st.get('state')}; stages done {done}; {age}"
    u = uncheckable_reason(r)
    return ("hit" if r["strict"] else ("uncheckable" if u else "zero")), r, u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bugs", default="", help="space- or comma-separated <P>_<B> (default: the dataset scope, see --last)")
    ap.add_argument("--last", type=int, default=5, help="default scope: latest N bugs per project (default 5)")
    ap.add_argument("--csv", default="bugs_last10.csv,bugs.csv")
    ap.add_argument("--include-dropped", action="store_true", help=f"also include {', '.join(DROPPED_PROJECTS)}")
    ap.add_argument("--results-root", default="outputs_daikon_strict_batch")
    ap.add_argument("--pilot", "--extra", dest="extra", default="",
                    help="bugs OUTSIDE the scope to show in a separate section, never in the totals")
    ap.add_argument("--stale-hours", type=float, default=6.0, help="a 'running' status older than this is shown as stale")
    args = ap.parse_args()
    res = Path(args.results_root)
    exclude = () if args.include_dropped else DROPPED_PROJECTS
    old_pending = []
    if args.bugs:
        bugs = [b for b in args.bugs.replace(",", " ").split() if b and b.rpartition("_")[0] not in exclude]
        scope = f"--bugs list ({len(bugs)} bugs)"
    else:
        bugs = []
        for p, ids in sorted(bugs_by_project(args.csv, args.last, exclude).items()):
            for b in ids:
                n = old_count(f"{p}_{b}")
                if n is None:
                    old_pending.append(f"{p}_{b}")
                elif n > 0:
                    bugs.append(f"{p}_{b}")
        scope = f"latest {args.last} bugs per project with >=1 old text-diff FALSIFIED ({len(bugs)} bugs)"
    extra = [b for b in args.extra.replace(",", " ").split() if b and b not in bugs]
    print(f"scope: {scope}; excluded projects: {', '.join(exclude) or 'none'}")
    print()

    groups = {k: [] for k in ("hit", "zero", "uncheckable", "deferred", "failed", "incomplete", "not_started")}
    for bug in bugs:
        g, r, note = classify(bug, res, args.stale_hours)
        groups[g].append((bug, r, note))

    def table(title, rows):
        print(f"######## {title} ({len(rows)}) ########")
        if not rows:
            print("  (none)")
        else:
            print(f'{"bug":<22}{"old":>6}{"checker":>9}{"fixed-val":>10}{"strict":>8}  {"source / note"}')
            for bug, r, note in rows:
                print(f"{bug:<22}{str(r.get('old_falsified', '-')):>6}{str(r.get('checker_falsified', '-')):>9}"
                      f"{str(r.get('fixed_validated', '-')):>10}{str(r.get('strict', '-')):>8}  "
                      f"{r.get('source', '')}{(' | ' + note) if note else ''}")
        print()

    table("COMPLETED: STRICTLY DETECTED", groups["hit"])
    table("COMPLETED: ZERO STRICT HITS", groups["zero"])
    table("COMPLETED BUT UNCHECKABLE (not counted as zero)", groups["uncheckable"])
    for key, title in (("deferred", "DEFERRED (disk space)"), ("failed", "FAILED (traces kept)"),
                       ("incomplete", "INCOMPLETE / RUNNING"), ("not_started", "NOT STARTED")):
        rows = groups[key]
        print(f"######## {title} ({len(rows)}) ########")
        for bug, _, note in rows:
            print(f"{bug:<22}{note or ''}")
        if not rows:
            print("  (none)")
        print()

    done = groups["hit"] + groups["zero"] + groups["uncheckable"]
    old_det = sum(1 for _, r, _ in done if (r.get("old_falsified") or 0) > 0)
    print("######## TOTALS (completed bugs only) ########")
    print(f"completed={len(done)}  of which: old text-diff detected={old_det}  "
          f"checker={sum(1 for _, r, _ in done if r['checker_falsified'] > 0)}  "
          f"fixed-validated={sum(1 for _, r, _ in done if r['fixed_validated'] > 0)}  "
          f"STRICT={len(groups['hit'])}  uncheckable={len(groups['uncheckable'])}")
    print(f"not completed: deferred={len(groups['deferred'])} failed={len(groups['failed'])} "
          f"incomplete={len(groups['incomplete'])} not started={len(groups['not_started'])}")
    if old_pending:
        print()
        print(f"######## OLD TEXT-DIFF RESULT MISSING -- not yet known whether in scope ({len(old_pending)}) ########")
        print(" ".join(old_pending))
    if extra:
        print()
        table("OUTSIDE THE SCOPE (not in the totals)", [(b, *classify(b, res, args.stale_hours)[1:]) for b in extra])


if __name__ == "__main__":
    main()
