#!/usr/bin/env python3
"""Standalone fixture test for run_daikon_checker_bug.py's checker step
(DaikonCandidateChecker + candidate mapping + stock cross-check), on a tiny
Java program instead of a Defects4J bug. Needs java/javac and a daikon.jar;
no defects4j.

Trace A infers the candidates; trace B is checked against them. B differs
from A only by a few extra calls, so the expected verdicts are known:

  known held       Calc.held ENTER "a < b"            HELD (40 evaluations)
  known violated   Calc.viol ENTER "a < b"            FALSIFIED (1 violation: a=50, b=10)
  unexercised ppt  Calc.onlyA ENTER "a < b"           UNEXERCISED (never called in B)
  equality alias   Calc.alias ENTER "a == b"          FALSIFIED (b is an alias of leader a)
                   Calc.alias ENTER "a < c"           HELD (printed over the leader only;
                                                      "b < c" is not a candidate)
  OBJECT point     Range:::OBJECT "this.lo < this.hi" FALSIFIED via propagation from methods
  EXIT / EXITnn    Calc.maxOf EXIT and EXITnn         evaluated (combined EXIT gets every exit)

It also checks invA against trace A itself (0 FALSIFIED expected) and that
the stock InvariantChecker agrees with the wrapper.

Usage:
    DAIKON_JAR=/path/to/daikon.jar python3 test_daikon_checker_fixture.py [--keep DIR]
Exit status 0 = all checks passed.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# A toy program: don't size the JVM heap from the SLURM allocation.
os.environ.setdefault("DAIKON_JAVA_XMX", "2g")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_daikon_checker_bug import (  # noqa: E402
    classify,
    compile_checker,
    load_records,
    parse_stock,
    print_invariants,
    run_daikon,
    run_logged,
    run_stock_checker,
    run_wrapper,
)

CALC = """package fixture;

public class Calc {
  public static int held(int a, int b) { return b - a; }
  public static int viol(int a, int b) { return b - a; }
  public static int onlyA(int a, int b) { return b - a; }
  public static int alias(int a, int b, int c) { return a + b + c; }
  public static int maxOf(int a, int b) {
    if (a >= b) {
      return a;
    }
    return b;
  }
}
"""

RANGE = """package fixture;

public class Range {
  private int lo;
  private int hi;
  public Range(int lo, int hi) { this.lo = lo; this.hi = hi; }
  public void setHi(int h) { this.hi = h; }
  public int width() { return hi - lo; }
}
"""

DRIVER = """package fixture;

public class Driver {
  public static void main(String[] args) {
    boolean b = args[0].equals("B");
    for (int i = 0; i < 40; i++) {
      Calc.held(i, i + 1 + i % 5);
      Calc.viol(i, i + 2 + i % 3);
      if (!b) {
        Calc.onlyA(i, i + 3 + i % 4);
      }
      Calc.alias(i, i, i + 5 + i % 3);
      Calc.maxOf(i % 7, i % 5);
      Range r = new Range(i, i + 10 + i % 3);
      r.setHi(i + 11 + i % 4);
      r.width();
    }
    if (b) {
      Calc.viol(50, 10);
      Calc.alias(7, 8, 20);
      Range r = new Range(5, 20);
      r.setHi(1);
      r.width();
    }
  }
}
"""

CALC_PPT = "fixture.Calc.{}:::ENTER"


def build(work: Path, jar: str) -> dict:
    src = work / "src" / "fixture"
    src.mkdir(parents=True)
    (src / "Calc.java").write_text(CALC)
    (src / "Range.java").write_text(RANGE)
    (src / "Driver.java").write_text(DRIVER)
    classes = work / "classes"
    logs = work / "logs"
    run_logged(["javac", "-g", "-d", classes, *sorted(src.glob("*.java"))], logs / "javac.log")
    traces = {}
    for phase in ("A", "B"):
        name = f"trace{phase}.dtrace.gz"
        run_logged(["java", "-cp", f"{classes}:{jar}", "daikon.Chicory",
                    r"--ppt-select-pattern=^fixture\.", r"--ppt-omit-pattern=^fixture\.Driver",
                    f"--dtrace-file={name}", "fixture.Driver", phase], logs / f"chicory_{phase}.log", cwd=work)
        traces[phase] = work / name
        assert traces[phase].is_file(), f"Chicory wrote no {name}"
    return traces


def check_against(work: Path, jar: str, checker_classes: Path, inv: Path, trace: Path, tag: str):
    out = work / f"check_{tag}"
    out.mkdir()
    records_path, _ = run_wrapper(jar, checker_classes, inv, trace, out, work / "logs" / f"wrapper_{tag}.log", 5)
    stock = run_stock_checker(jar, inv, trace, out, work / "logs" / f"stock_{tag}.log")
    stock_failed, unparsed = parse_stock(stock)
    outcomes, cross = classify((work / "invariantsA.txt").read_text(), load_records(records_path), stock_failed)
    return outcomes, cross, stock_failed, unparsed


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
        with tempfile.TemporaryDirectory(prefix="daikon_checker_fixture_") as d:
            run(Path(d), jar)


def run(work: Path, jar: str):
    failures: list[str] = []

    def expect(cond: bool, what: str):
        print(("PASS  " if cond else "FAIL  ") + what)
        if not cond:
            failures.append(what)

    traces = build(work, jar)
    inv = work / "invA.inv.gz"
    run_daikon(jar, traces["A"], inv, r"^fixture\.", work / "logs" / "daikon_A.log")
    print_invariants(jar, inv, work / "invariantsA.txt", work / "logs" / "printinvariants.log")
    checker_classes = compile_checker(jar, work / "checker-classes", work / "logs" / "javac_checker.log")

    # ---- invA against its own training trace: nothing can be violated.
    self_out, self_cross, _, _ = check_against(work, jar, checker_classes, inv, traces["A"], "self")
    expect(not any(o["verdict"] == "FALSIFIED" for o in self_out),
           "invA checked against trace A itself: 0 FALSIFIED "
           f"(got {[(o['ppt'], o['invariant']) for o in self_out if o['verdict'] == 'FALSIFIED']})")

    # ---- invA against trace B
    outcomes, cross, stock_failed, unparsed = check_against(work, jar, checker_classes, inv, traces["B"], "B")
    by = {(o["ppt"], o["invariant"]): o for o in outcomes}

    def verdict(ppt, text):
        o = by.get((ppt, text))
        if o is None:
            cands = sorted(i for p, i in by if p == ppt)
            print(f"      candidates at {ppt}: {cands}")
            return None, {}
        return o["verdict"], o

    v, o = verdict(CALC_PPT.format("held(int, int)"), "a < b")
    expect(v == "HELD" and o.get("evaluations") == 40, f"held: 'a < b' HELD with 40 evaluations (got {v}, {o.get('evaluations')})")

    v, o = verdict(CALC_PPT.format("viol(int, int)"), "a < b")
    samples = o.get("violating_samples", [])
    expect(v == "FALSIFIED" and o.get("violations") == 1, f"violated: 'a < b' FALSIFIED once (got {v}, {o.get('violations')})")
    expect(bool(samples) and samples[0]["values"] == {"a": "50", "b": "10"},
           f"violated: violating sample is a=50, b=10 (got {samples[:1]})")

    v, o = verdict(CALC_PPT.format("onlyA(int, int)"), "a < b")
    expect(v == "UNEXERCISED" and o.get("evaluations") == 0, f"unexercised ppt: 'a < b' UNEXERCISED (got {v})")
    only_a = [x for x in outcomes if x["ppt"].startswith("fixture.Calc.onlyA(")]
    expect(bool(only_a) and all(x["verdict"] == "UNEXERCISED" for x in only_a),
           f"unexercised ppt: all {len(only_a)} onlyA candidates UNEXERCISED")

    alias_ppt = CALC_PPT.format("alias(int, int, int)")
    v, o = verdict(alias_ppt, "a == b")
    expect(v == "FALSIFIED", f"equality alias: 'a == b' FALSIFIED (got {v})")
    v, o = verdict(alias_ppt, "a < c")
    expect(v == "HELD", f"equality alias: leader invariant 'a < c' HELD (got {v})")
    expect((alias_ppt, "b < c") not in by, "equality alias: no separate candidate 'b < c' for the alias")

    v, o = verdict("fixture.Range:::OBJECT", "this.lo < this.hi")
    expect(v == "FALSIFIED" and o.get("violations_propagated", 0) >= 1,
           f"OBJECT: 'this.lo < this.hi' FALSIFIED via propagation (got {v}, {o.get('violations_propagated')})")

    exits = [x for x in outcomes if x["ppt"].startswith("fixture.Calc.maxOf(") and ":::EXIT" in x["ppt"]]
    combined = [x for x in exits if x["ppt"].endswith(":::EXIT")]
    numbered = [x for x in exits if not x["ppt"].endswith(":::EXIT")]
    expect(bool(combined) and all(x["verdict"] == "HELD" and x["evaluations"] == 40 for x in combined),
           f"combined EXIT: {len(combined)} maxOf candidates HELD, each on all 40 exits")
    expect(bool(numbered) and all(x["verdict"] == "HELD" and 0 < x["evaluations"] < 40 for x in numbered),
           f"numbered EXITnn: {len(numbered)} maxOf candidates HELD, each on only its own exits")

    expect(not cross["stock_only_disagreements"] and unparsed == 0,
           f"stock InvariantChecker agrees with the wrapper (disagreements={cross['stock_only_disagreements']}, "
           f"unparsed={unparsed})")
    expect((CALC_PPT.format("viol(int, int)"), "a < b") in stock_failed
           and (alias_ppt, "a == b") in stock_failed,
           "stock InvariantChecker also reports the two direct violations")
    expect(not any(o["verdict"] == "UNCHECKABLE" for o in outcomes),
           f"no UNCHECKABLE candidates "
           f"(got {[(o['ppt'], o['invariant'], o.get('reason')) for o in outcomes if o['verdict'] == 'UNCHECKABLE']})")

    counts = {v: sum(1 for o in outcomes if o["verdict"] == v) for v in ("FALSIFIED", "HELD", "UNEXERCISED", "UNCHECKABLE")}
    print(f"\ncandidates={len(outcomes)} {counts}")
    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
