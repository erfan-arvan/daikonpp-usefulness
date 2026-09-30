#!/usr/bin/env python3
"""NaN regression fixture for run_daikon_checker_bug.py's check step
(run_checks: baseline invA vs traceA first, then invA vs trace B).

Reproduces the Daikon inference/check inconsistency seen on Math_106:
PptSliceEquality.createEqualityInvs regroups variables split off an equality
set in a HashMap keyed by the boxed value, and Double.equals(NaN, NaN) is
true. In pair(k, a, b), a and b equal k on every call except one where k=5
and a, b are NaN: a and b are split off k's set together, regrouped into one
set, and Daikon prints "a == b" -- which Daikon's own FloatEqual check
(fuzzy NaN != NaN) rejects on that same sample.

Checks:
  1. Daikon prints pair ENTER "a == b" (the case is really reproduced).
  2. The baseline (invA vs traceA) FALSIFIES it, only with all-NaN samples,
     and the stock InvariantChecker reports it too (it is Daikon's own check).
  3. NaN candidates that ARE consistent stay HELD in the baseline:
     constNaN ENTER "x == Double.NaN", Holder OBJECT "this.arr[] == [NaN, NaN]".
  4. In B it is UNCHECKABLE / inconsistent_with_inference_trace, with its raw
     verdict kept -- not HELD, not FALSIFIED.
  5. NaN is not ignored: in B, lessThan(NaN, 1) FALSIFIES "v < w",
     constNaN(1.0) FALSIFIES "x == Double.NaN", and a plain numeric
     violation FALSIFIES ratio ENTER "p < q".
  6. No stock/wrapper disagreement; baseline flagged requires_investigation.

Usage:
    DAIKON_JAR=/path/to/daikon.jar python3 test_daikon_checker_nan_fixture.py [--keep DIR]
Exit status 0 = all checks passed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DAIKON_JAVA_XMX", "2g")  # a toy program
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_daikon_checker_bug import print_invariants, run_checks, run_daikon, run_logged  # noqa: E402

N_SRC = """package nanfx;

public class N {
  public static double pair(double k, double a, double b) { return k; }
  public static double constNaN(double x) { return 0.0; }
  public static boolean lessThan(double v, double w) { return v < w; }
  public static double ratio(int p, int q) { return (double) p / q; }
}
"""

HOLDER_SRC = """package nanfx;

public class Holder {
  public double[] arr = new double[] {Double.NaN, Double.NaN};
  public int size() { return arr.length; }
}
"""

DRIVER_SRC = """package nanfx;

public class Driver {
  public static void main(String[] args) {
    boolean b = args[0].equals("B");
    Holder h = new Holder();
    for (int i = 0; i < 40; i++) {
      N.pair(i, i, i);
      N.constNaN(Double.NaN);
      N.lessThan(i, i + 1 + i % 3);
      N.ratio(i, i + 2 + i % 4);
      h.size();
    }
    N.pair(5, Double.NaN, Double.NaN);
    for (int i = 0; i < 40; i++) {
      N.pair(i, i, i);
    }
    if (b) {
      N.lessThan(Double.NaN, 1.0);
      N.constNaN(1.0);
      N.ratio(9, 3);
    }
  }
}
"""

PAIR = "nanfx.N.pair(double, double, double):::ENTER"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default=None, help="work in this (new) directory and keep it")
    args = ap.parse_args()
    jar = os.environ.get("DAIKON_JAR")
    if not jar or not Path(jar).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at daikon.jar (got {jar!r})")
    jar = str(Path(jar).resolve())
    if args.keep:
        work = Path(args.keep).resolve()
        work.mkdir(parents=True, exist_ok=False)
        run(work, jar)
    else:
        with tempfile.TemporaryDirectory(prefix="daikon_checker_nan_") as d:
            run(Path(d), jar)


def run(work: Path, jar: str):
    failures: list[str] = []

    def expect(cond: bool, what: str):
        print(("PASS  " if cond else "FAIL  ") + what)
        if not cond:
            failures.append(what)

    src = work / "src" / "nanfx"
    src.mkdir(parents=True)
    (src / "N.java").write_text(N_SRC)
    (src / "Holder.java").write_text(HOLDER_SRC)
    (src / "Driver.java").write_text(DRIVER_SRC)
    logs = work / "logs"
    run_logged(["javac", "-g", "-d", work / "classes", *sorted(src.glob("*.java"))], logs / "javac.log")
    out = work / "out"
    out.mkdir()
    traces = {}
    for phase in ("A", "B"):
        name = f"trace{phase}.dtrace.gz"
        run_logged(["java", "-cp", f"{work / 'classes'}:{jar}", "daikon.Chicory",
                    r"--ppt-select-pattern=^nanfx\.", r"--ppt-omit-pattern=^nanfx\.Driver",
                    f"--dtrace-file={name}", "nanfx.Driver", phase], logs / f"chicory_{phase}.log", cwd=work)
        traces[phase] = work / name
    inv = out / "invA.inv.gz"
    run_daikon(jar, traces["A"], inv, r"^nanfx\.", logs / "daikon_A.log")
    invariants_a = out / "invariantsA.txt"
    print_invariants(jar, inv, invariants_a, logs / "printinvariants.log")

    text = invariants_a.read_text()
    expect(f"{PAIR}\na == b" in text or ("a == b" in text.split(PAIR, 1)[-1].split("=====", 1)[0]),
           "reproduced: Daikon prints pair ENTER 'a == b' although a, b were NaN together once")

    info = {"project": "nanfx", "bug_id": "0", "mode": "normal"}
    run_checks(jar, out, inv, invariants_a, traces["A"], traces["B"], logs, info, 5)
    b = json.loads((out / "run_info.json").read_text())["baseline_A"]
    base = {(o["ppt"], o["invariant"]): o for o in b["falsified"]}
    outcomes = {(o["ppt"], o["invariant"]): o
                for o in map(json.loads, (out / "daikon_checker_outcomes.jsonl").read_text().splitlines())}
    base_all = {(o["ppt"], o["invariant"]): o
                for o in map(json.loads, (out / "baseline_A" / "daikon_checker_outcomes.jsonl").read_text().splitlines())}
    stock_base = (out / "baseline_A" / "stock_checker_verbose.txt").read_text()

    o = base.get((PAIR, "a == b"))
    expect(o is not None and o["violations"] >= 1 and o["violations_all_nan"] == o["violations"],
           f"baseline FALSIFIES pair 'a == b', only on all-NaN samples (got {o and (o['violations'], o['violations_all_nan'])})")
    expect(all(x["violations_all_nan"] == x["violations"] for x in base.values()),
           f"every baseline violation is all-NaN ({len(base)} baseline FALSIFIED: {sorted(k[1] for k in base)})")
    expect(f"At ppt {PAIR}, Invariant 'a == b' invalidated" in stock_base,
           "the stock InvariantChecker also rejects 'a == b' on the inference trace")
    expect(b["requires_investigation"] is True, "baseline is flagged requires_investigation")

    for key in (("nanfx.N.constNaN(double):::ENTER", "x == Double.NaN"),
                ("nanfx.Holder:::OBJECT", "this.arr[] == [NaN, NaN]")):
        v = base_all.get(key, {}).get("verdict")
        expect(v == "HELD", f"consistent NaN candidate {key[1]!r} HELD in the baseline (got {v})")

    o = outcomes.get((PAIR, "a == b"), {})
    expect(o.get("verdict") == "UNCHECKABLE" and o.get("reason") == "inconsistent_with_inference_trace"
           and o.get("raw_verdict") == "FALSIFIED" and o.get("baseline_violations", 0) >= 1,
           f"B: pair 'a == b' UNCHECKABLE/inconsistent_with_inference_trace, raw verdict kept "
           f"(got {o.get('verdict')}, {o.get('reason')}, raw={o.get('raw_verdict')})")

    for key, sample, what in (
            (("nanfx.N.lessThan(double, double):::ENTER", "v < w"), {"v": "NaN", "w": "1.0"}, "NaN input"),
            (("nanfx.N.constNaN(double):::ENTER", "x == Double.NaN"), {"x": "1.0"}, "non-NaN input"),
            (("nanfx.N.ratio(int, int):::ENTER", "p < q"), {"p": "9", "q": "3"}, "numeric input")):
        o = outcomes.get(key, {})
        vals = [s["values"] for s in o.get("violating_samples", [])]
        expect(o.get("verdict") == "FALSIFIED" and sample in vals,
               f"B: {key[1]!r} FALSIFIED by {what} {sample} (got {o.get('verdict')}, {vals[:2]})")

    ri = json.loads((out / "run_info.json").read_text())
    dis = [ri["cross_check"][k] for k in ("stock_only_disagreements", "wrapper_only_disagreements")] + \
          [b["cross_check"][k] for k in ("stock_only_disagreements", "wrapper_only_disagreements")]
    expect(not any(dis), f"no stock/wrapper disagreement in baseline or B (got {dis})")

    print(f"\nbaseline FALSIFIED={len(base)}  B counts={ri['counts']}  "
          f"inconsistent={ri['inconsistent_with_inference_trace']}")
    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
