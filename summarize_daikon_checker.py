#!/usr/bin/env python3
"""Per-bug summary of run_daikon_checker_bug.py / recheck_daikon_checker.py
results next to the old text-diff results (daikon_diff_invariants.py).

For each bug:
  old    FALSIFIED count from outputs_usefulness/<P>_<B>/daikon_outcomes.jsonl,
         split into "ppt missing" (the ppt has no block at all in
         invariantsFull.txt) and "ppt present" (the ppt is printed, the
         invariant text is not)
  new    FALSIFIED / HELD / UNEXERCISED / UNEVALUATED_MISSING / UNCHECKABLE
         from <out-root>/normal/<P>_<B>/daikon_checker_outcomes.jsonl
         (eligible samples only), FALSIFIED split by origin: direct (the
         stock checker's path) / propagated (OBJECT/CLASS)
  diag   candidates an unmatched-ENTER sample (a call that never returned)
         would violate -- diagnostic only, never part of a verdict
  inc    candidates classified UNCHECKABLE / inconsistent_with_inference_trace
         (violated on traceA, the samples they were inferred from)
  base   FALSIFIED when invA is checked against traceA itself (expected 0);
         any nonzero baseline is flagged "REQUIRES INVESTIGATION"
  null   FALSIFIED from <out-root>/null/<P>_<B>/, split the same way
  a few FALSIFIED invariants with their violating samples, every baseline
  violation, and a candidate-identity comparison of the baseline, null and
  normal results (keys are (ppt, invariant text))

Completed runs (CHECKER_COMPLETE present, including runs with zero results)
are listed apart from incomplete / failed / not-run ones. Runs written by an
earlier version of the checker (no baseline_A in run_info.json) count as
incomplete: rerun recheck_daikon_checker.py on them.

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
from run_daikon_checker_bug import DIAG_ORIGINS, ORIGINS, VERDICTS

SHORT = {"direct": "dir", "propagated": "prop"}


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


def run_state(d: Path):
    """('complete', outcomes, run_info) or (<why not>, None, None)."""
    if not d.is_dir():
        return "not run", None, None
    info = json.loads((d / "run_info.json").read_text()) if (d / "run_info.json").is_file() else {}
    if (d / "CHECKER_COMPLETE").is_file() and (d / "daikon_checker_outcomes.jsonl").is_file():
        if "requires_investigation" not in info.get("baseline_A", {}):
            return "old checker format -- run recheck", None, None
        return "complete", read_jsonl(d / "daikon_checker_outcomes.jsonl"), info
    if info.get("error"):
        return f"failed: {info['error'][:160]}", None, None
    logs = sorted((d / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime) if (d / "logs").is_dir() else []
    if logs:
        age = datetime.datetime.now().timestamp() - logs[-1].stat().st_mtime
        return f"incomplete (running or killed; last log {logs[-1].name}, {age / 60:.0f} min ago)", None, None
    return "incomplete (no logs yet)", None, None


def counts(outcomes: list[dict]) -> dict[str, int]:
    return {v: sum(1 for o in outcomes if o["verdict"] == v) for v in VERDICTS}


def origin_str(outcomes: list[dict]) -> str:
    return "/".join(str(sum(1 for o in outcomes if o.get("falsified_by") == g)) for g in ORIGINS)


def n_diag(outcomes: list[dict]) -> int:
    return sum(1 for o in outcomes if any(o.get(f"diag_violations_{g}", 0) for g in DIAG_ORIGINS))


def n_inc(outcomes: list[dict]) -> int:
    return sum(1 for o in outcomes if o.get("reason") == "inconsistent_with_inference_trace")


def keys(outcomes, pred) -> set:
    return {(o["ppt"], o["invariant"]) for o in outcomes if pred(o)}


def short_state(state: str) -> str:
    return ("not run" if state == "not run" else "failed" if state.startswith("failed")
            else "old fmt" if state.startswith("old") else "incompl.")


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
        normal = run_state(out_root / "normal" / bug)
        null = run_state(out_root / "null" / bug)
        row = (bug, normal, null, old_result(old_root / bug))
        (complete if normal[1] is not None else incomplete).append(row)

    def old_cols(old):
        if old is None:
            return f'{"n/a":>5}{"":>9}{"":>9}'
        n, miss, pres = old[:3]
        return f"{n:>5}{'?' if miss is None else miss:>9}{'?' if pres is None else pres:>9}"

    oh = "/".join(SHORT[g] for g in ORIGINS)
    print("######## COMPLETED (normal run finished; zero counts are real zeros) ########")
    print(f'{"bug":<12}{"old":>5}{"ppt-miss":>9}{"ppt-pres":>9} |{"FALS":>5} {oh:>8}{"HELD":>6}{"UNEX":>5}{"MISS":>5}'
          f'{"UNCHK":>6}{"inc":>5}{"diag":>5}{"base":>6} |{"null":>8} {oh:>8}{"inc":>5}{"base":>6}')
    flagged = []

    def base_col(inf, label, bug):
        b = inf["baseline_A"]["counts"]["FALSIFIED"]
        if b:
            flagged.append((bug, label, inf["baseline_A"]))
        return f"{b}{'!' if b else ''}"

    for bug, (_, outcomes, info), (nstate, noutcomes, ninfo), old in complete:
        c = counts(outcomes)
        base = base_col(info, "normal", bug)
        if noutcomes is not None:
            nc = f"{counts(noutcomes)['FALSIFIED']:>8} {origin_str(noutcomes):>8}{n_inc(noutcomes):>5}" \
                 f"{base_col(ninfo, 'null', bug):>6}"
        else:
            nc = f"{short_state(nstate):>8}"
        print(f"{bug:<12}{old_cols(old)} |{c['FALSIFIED']:>5} {origin_str(outcomes):>8}{c['HELD']:>6}"
              f"{c['UNEXERCISED']:>5}{c['UNEVALUATED_MISSING']:>5}{c['UNCHECKABLE']:>6}{n_inc(outcomes):>5}"
              f"{n_diag(outcomes):>5}{base:>6} |{nc}")
    if not complete:
        print("  (none)")
    print("  old = daikon_outcomes.jsonl FALSIFIED (text diff); ppt-miss / ppt-pres = its ppt absent from / present in")
    print("  invariantsFull.txt. Verdicts use eligible samples only (Daikon's inference samples):")
    print(f"  FALS ({oh} = direct, the stock checker's path / propagated to OBJECT-CLASS), HELD, UNEX (ppt never")
    print("  reached), MISS (UNEVALUATED_MISSING: reached, but every sample had a missing/out-of-bounds variable),")
    print("  UNCHK. diag = candidates an unmatched ENTER (call never returned) would violate: NOT in any verdict.")
    print("  inc = UNCHECKABLE because violated on its own inference trace (inconsistent_with_inference_trace).")
    print("  base = FALSIFIED when invA is checked against traceA itself (expected 0); '!' = requires investigation.")
    print()

    print("######## BASELINE VIOLATIONS -- REQUIRE INVESTIGATION ########")
    if not flagged:
        print("  (none: every completed run has 0 baseline violations)")
    for bug, label, b in flagged:
        classes = {}
        for o in b["falsified"]:
            k = (o.get("daikon_class") or "?").split(".")[-1]
            classes[k] = classes.get(k, 0) + 1
        print(f"{bug:<12} {label:<6} baseline FALSIFIED={b['counts']['FALSIFIED']}  "
              f"all-NaN-only={b.get('falsified_all_nan_only', '?')}  by class={dict(sorted(classes.items()))}")
    print()

    print("######## INCOMPLETE / FAILED / NOT RUN ########")
    shown = False
    for bug, (state, _, _), (nstate, noutcomes, _), _ in incomplete:
        print(f"{bug:<12} normal: {state} | null: {'complete' if noutcomes is not None else nstate}")
        shown = True
    for bug, _, (nstate, noutcomes, _), _ in complete:
        if noutcomes is None:
            print(f"{bug:<12} normal: complete | null: {nstate}")
            shown = True
    if not shown:
        print("  (none)")
    print()

    print("######## DETAILS ########")
    for bug, (_, outcomes, info), (_, noutcomes, ninfo), old in complete:
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
        print(f"    diagnostic unmatched-ENTER candidates by (eligible) verdict: "
              f"{info.get('diag_unmatched_entry_candidates')}")
        for o in fals[: args.examples]:
            split = ", ".join(f"{SHORT[g]}={o[f'violations_{g}']}" for g in ORIGINS if o[f"violations_{g}"])
            print(f"    {o['ppt']}")
            print(f"        {o['invariant']}   (falsified_by={o['falsified_by']}; violations={o['violations']} "
                  f"[{split}] of {o['evaluations']} evaluations)")
            for s in o.get("violating_samples", [])[:2]:
                vals = ", ".join(f"{k}={v}" for k, v in s["values"].items())
                print(f"        sample: {vals}   [{s['status']}, {s.get('origin')}, "
                      f"{Path(s['file'] or '').name}:{s['line']}]")
        for label, inf in (("normal", info), ("null", ninfo)):
            if inf is None:
                continue
            bf = inf["baseline_A"]["falsified"]
            print(f"    baseline ({label}, invA vs traceA): {len(bf)} FALSIFIED"
                  f"{' -- REQUIRES INVESTIGATION' if bf else ''}")
            for o in bf[: args.examples * 5]:
                vals = "; ".join(", ".join(f"{k}={v}" for k, v in s["values"].items()) for s in o["violating_samples"])
                print(f"        {o['ppt']} :: {o['invariant']}   ({o['falsified_by']}, {o['violations']} of "
                      f"{o['evaluations']}, all-NaN {o.get('violations_all_nan', '?')})  {vals}")
            if len(bf) > args.examples * 5:
                print(f"        ... {len(bf) - args.examples * 5} more (run_info.json: baseline_A.falsified)")
        # candidate identities across baseline, null and normal
        raw_n = keys(outcomes, lambda o: o.get("raw_verdict") == "FALSIFIED")
        fin_n = keys(outcomes, lambda o: o["verdict"] == "FALSIFIED")
        base_n = {(o["ppt"], o["invariant"]) for o in info["baseline_A"]["falsified"]}
        print(f"    identity: normal raw FALSIFIED={len(raw_n)}, of which baseline-violated={len(raw_n & base_n)}; "
              f"final FALSIFIED={len(fin_n)}")
        if noutcomes is not None:
            raw_u = keys(noutcomes, lambda o: o.get("raw_verdict") == "FALSIFIED")
            fin_u = keys(noutcomes, lambda o: o["verdict"] == "FALSIFIED")
            base_u = {(o["ppt"], o["invariant"]) for o in ninfo["baseline_A"]["falsified"]}
            cand_n, cand_u = keys(outcomes, lambda o: True), keys(noutcomes, lambda o: True)
            print(f"    identity: candidates normal={len(cand_n)} null={len(cand_u)} shared={len(cand_n & cand_u)}; "
                  f"baselines normal={len(base_n)} null={len(base_u)} shared={len(base_n & base_u)}")
            print(f"    identity: null raw FALSIFIED={len(raw_u)}, of which baseline-violated={len(raw_u & base_u)}; "
                  f"final null FALSIFIED={len(fin_u)}")
            print(f"    identity: final FALSIFIED in both normal and null={len(fin_n & fin_u)} "
                  f"(noise that survives the baseline filter); normal-only={len(fin_n - fin_u)}; "
                  f"normal raw FALSIFIED also raw in null={len(raw_n & raw_u)}")
            for k in sorted(fin_n & fin_u)[: args.examples]:
                print(f"        both: {k[0]} :: {k[1]}")
        if noutcomes is not None:
            nf = [o for o in noutcomes if o["verdict"] == "FALSIFIED"]
            print(f"    null run: {len(nf)} FALSIFIED of {len(noutcomes)} candidates ({oh} {origin_str(noutcomes)})")
            for o in nf[: args.examples]:
                print(f"        {o['ppt']} :: {o['invariant']}   (falsified_by={o['falsified_by']})")
        print()


if __name__ == "__main__":
    main()
