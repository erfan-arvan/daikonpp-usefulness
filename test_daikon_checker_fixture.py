#!/usr/bin/env python3
"""Standalone fixture test for run_daikon_checker_bug.py's checker step
(DaikonCandidateChecker + candidate mapping + stock cross-check), on a tiny
Java program instead of a Defects4J bug. Needs java/javac and a daikon.jar;
no defects4j.

Trace A infers the candidates; trace B is checked against them. B differs
from A only by a few extra calls, so the expected verdicts are known:

  known held       Calc.held ENTER "a < b"            HELD (40 evaluations)
  known violated   Calc.viol ENTER "a < b"            FALSIFIED (1 violation: a=50, b=10),
                                                      falsified_by=direct
  unexercised ppt  Calc.onlyA ENTER "a < b"           UNEXERCISED (never called in B)
  missing values   Calc.hold ENTER "h.x < h.y"        UNEVALUATED_MISSING (called in B only
                                                      with h == null)
  unmatched ENTER  Calc.boom ENTER "a < b"            HELD on the 40 calls that returned; the
                                                      one violating call throws, so its ENTER
                                                      sample is diagnostic only
                                                      (diag_violations_unmatched_entry=1)
  equality alias   Calc.alias ENTER "a == b"          FALSIFIED (b is an alias of leader a)
                   Calc.alias ENTER "a < c"           HELD (printed over the leader only;
                                                      "b < c" is not a candidate)
  OBJECT point     Range:::OBJECT "this.lo < this.hi" FALSIFIED, falsified_by=propagated
  EXIT / EXITnn    Calc.maxOf EXIT and EXITnn         evaluated (combined EXIT gets every exit)

It also checks invA against trace A itself (0 FALSIFIED expected), that
the stock InvariantChecker agrees with the wrapper on its own path, and the
--null test-inventory comparison (including multiplicities).

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
    InfraError,
    classify,
    compile_checker,
    load_records,
    parse_stock,
    print_invariants,
    run_daikon,
    run_logged,
    run_stock_checker,
    run_wrapper,
    verify_same_inventory,
)

CALC = """package fixture;

public class Calc {
  public static int held(int a, int b) { return b - a; }
  public static int viol(int a, int b) { return b - a; }
  public static int onlyA(int a, int b) { return b - a; }
  public static int alias(int a, int b, int c) { return a + b + c; }
  public static int boom(int a, int b) {
    if (a > b) {
      throw new IllegalArgumentException("a > b");
    }
    return b - a;
  }
  public static int hold(Holder h) { return h == null ? 0 : h.y - h.x; }
  public static int maxOf(int a, int b) {
    if (a >= b) {
      return a;
    }
    return b;
  }
}
"""

HOLDER = """package fixture;

public class Holder {
  public int x;
  public int y;
  public Holder(int x, int y) { this.x = x; this.y = y; }
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
      Calc.boom(i, i + 1 + i % 6);
      if (!b) {
        Calc.hold(new Holder(i, i + 2 + i % 5));
      }
      Calc.maxOf(i % 7, i % 5);
      Range r = new Range(i, i + 10 + i % 3);
      r.setHi(i + 11 + i % 4);
      r.width();
    }
    if (b) {
      Calc.viol(50, 10);
      Calc.alias(7, 8, 20);
      try {
        Calc.boom(9, 3);
      } catch (IllegalArgumentException e) {
        // expected: this call never returns normally
      }
      Calc.hold(null);
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
    (src / "Holder.java").write_text(HOLDER)
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
    expect(v == "FALSIFIED" and o.get("violations") == 1 and o.get("falsified_by") == "direct",
           f"violated: 'a < b' FALSIFIED once, falsified_by=direct (got {v}, {o.get('violations')}, {o.get('falsified_by')})")
    expect(bool(samples) and samples[0]["values"] == {"a": "50", "b": "10"},
           f"violated: violating sample is a=50, b=10 (got {samples[:1]})")

    v, o = verdict(CALC_PPT.format("onlyA(int, int)"), "a < b")
    expect(v == "UNEXERCISED" and o.get("evaluations") == 0 and o.get("skipped_missing") == 0,
           f"unexercised ppt: 'a < b' UNEXERCISED, no samples at all (got {v}, skipped={o.get('skipped_missing')})")
    only_a = [x for x in outcomes if x["ppt"].startswith("fixture.Calc.onlyA(")]
    expect(bool(only_a) and all(x["verdict"] == "UNEXERCISED" for x in only_a),
           f"unexercised ppt: all {len(only_a)} onlyA candidates UNEXERCISED")

    v, o = verdict(CALC_PPT.format("hold(fixture.Holder)"), "h.x < h.y")
    expect(v == "UNEVALUATED_MISSING" and o.get("evaluations") == 0 and o.get("skipped_missing", 0) >= 1,
           f"missing values: 'h.x < h.y' UNEVALUATED_MISSING, not UNEXERCISED "
           f"(got {v}, skipped_missing={o.get('skipped_missing')})")

    boom_ppt = CALC_PPT.format("boom(int, int)")
    v, o = verdict(boom_ppt, "a < b")
    samples = o.get("diag_violating_samples", [])
    expect(v == "HELD" and o.get("evaluations") == 40 and o.get("violations") == 0,
           f"unmatched ENTER: 'a < b' HELD on the 40 returned calls, not FALSIFIED "
           f"(got {v}, evaluations={o.get('evaluations')}, violations={o.get('violations')})")
    expect(o.get("diag_violations_unmatched_entry") == 1 and o.get("diag_evaluations_unmatched_entry") == 1,
           f"unmatched ENTER: kept as a diagnostic (diag evaluations={o.get('diag_evaluations_unmatched_entry')}, "
           f"diag violations={o.get('diag_violations_unmatched_entry')})")
    expect(bool(samples) and samples[0].get("origin") == "unmatched_entry"
           and samples[0]["values"] == {"a": "9", "b": "3"},
           f"unmatched ENTER: diagnostic sample a=9, b=3 tagged unmatched_entry (got {samples[:1]})")
    expect((boom_ppt, "a < b") not in stock_failed,
           "unmatched ENTER: the stock InvariantChecker does not report it")

    alias_ppt = CALC_PPT.format("alias(int, int, int)")
    v, o = verdict(alias_ppt, "a == b")
    expect(v == "FALSIFIED", f"equality alias: 'a == b' FALSIFIED (got {v})")
    v, o = verdict(alias_ppt, "a < c")
    expect(v == "HELD", f"equality alias: leader invariant 'a < c' HELD (got {v})")
    expect((alias_ppt, "b < c") not in by, "equality alias: no separate candidate 'b < c' for the alias")

    v, o = verdict("fixture.Range:::OBJECT", "this.lo < this.hi")
    expect(v == "FALSIFIED" and o.get("falsified_by") == "propagated" and o.get("violations_propagated", 0) >= 1
           and all(x.get("origin") == "propagated" for x in o.get("violating_samples", [])),
           f"OBJECT: 'this.lo < this.hi' FALSIFIED, falsified_by=propagated "
           f"(got {v}, {o.get('falsified_by')}, {o.get('violations_propagated')})")

    exits = [x for x in outcomes if x["ppt"].startswith("fixture.Calc.maxOf(") and ":::EXIT" in x["ppt"]]
    combined = [x for x in exits if x["ppt"].endswith(":::EXIT")]
    numbered = [x for x in exits if not x["ppt"].endswith(":::EXIT")]
    expect(bool(combined) and all(x["verdict"] == "HELD" and x["evaluations"] == 40 for x in combined),
           f"combined EXIT: {len(combined)} maxOf candidates HELD, each on all 40 exits")
    expect(bool(numbered) and all(x["verdict"] == "HELD" and 0 < x["evaluations"] < 40 for x in numbered),
           f"numbered EXITnn: {len(numbered)} maxOf candidates HELD, each on only its own exits")

    expect(not cross["stock_only_disagreements"] and not cross["wrapper_only_disagreements"] and unparsed == 0,
           f"stock InvariantChecker agrees with the wrapper on its path (stock-only={cross['stock_only_disagreements']}, "
           f"wrapper-only={cross['wrapper_only_disagreements']}, unparsed={unparsed})")
    expect((CALC_PPT.format("viol(int, int)"), "a < b") in stock_failed
           and (alias_ppt, "a == b") in stock_failed,
           "stock InvariantChecker also reports the two direct violations")
    expect(not any(o["verdict"] == "UNCHECKABLE" for o in outcomes),
           f"no UNCHECKABLE candidates "
           f"(got {[(o['ppt'], o['invariant'], o.get('reason')) for o in outcomes if o['verdict'] == 'UNCHECKABLE']})")

    # ---- --null inventory comparison (pure function; multiplicities count)
    rec = {}
    verify_same_inventory(["T::a", "T::b", "T::b"], ["T::b", "T::a", "T::b"], rec)
    expect(rec["inventory_diff"] == {}, "null inventory: same tests, same multiplicities -> accepted")
    for a_, b_, what in ((["T::a", "T::b"], ["T::a", "T::b", "T::b"], "a test run twice in B"),
                         (["T::a", "T::b"], ["T::a"], "a test missing from B"),
                         (["T::a"], ["T::a", "U::c"], "an extra test in B")):
        rec = {}
        try:
            verify_same_inventory(a_, b_, rec)
            ok = False
        except InfraError:
            ok = bool(rec["inventory_diff"])
        expect(ok, f"null inventory: rejected when {what} (diff={rec.get('inventory_diff')})")

    counts = {v: sum(1 for o in outcomes if o["verdict"] == v)
              for v in ("FALSIFIED", "HELD", "UNEXERCISED", "UNEVALUATED_MISSING", "UNCHECKABLE")}
    print(f"\ncandidates={len(outcomes)} {counts}")
    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
