#!/usr/bin/env python3
"""Standalone fixture test for daikon_trace_map.py (buggy -> fixed ppt
mapping used by validate_daikon_fixed.py). Needs java/javac and daikon.jar.

Buggy and fixed versions of a tiny class:
  same()     unchanged                          -> identical, candidates checked
  moved(x)   body gains a line (EXITnn renumbered) -> exits_shifted: ENTER and
             combined EXIT checked, EXITnn candidates UNEXERCISED (never HELD)
  sig(a)     parameter renamed (declaration changes) -> changed: UNCHECKABLE
  gone()     removed in the fixed version        -> missing: UNCHECKABLE
Checks: the statuses above; the checker on the mapped fixed trace gives the
expected verdicts; the raw fixed trace makes Daikon fail and run_check
captures that instead of crashing; renaming the exits of an unchanged trace
leaves every ENTER / combined EXIT / OBJECT evaluation count unchanged.

Usage:
    DAIKON_JAR=/path/to/daikon.jar python3 test_daikon_trace_map_fixture.py [--keep DIR]
"""
from __future__ import annotations

import argparse
import gzip
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DAIKON_JAVA_XMX", "2g")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_daikon_checker_bug as rc  # noqa: E402
from daikon_trace_map import build_map, ppt_status, write_mapped  # noqa: E402
from validate_daikon_fixed import run_check  # noqa: E402

BUGGY = """package tm;
public class C {
  public int f = 1;
  public int same(int x) { return x + 1; }
  public int moved(int x) {
    if (x > 100) {
      return 0;
    }
    return x * 2;
  }
  public int sig(int a) { return a; }
  public int gone() { return f; }
}
"""
FIXED = """package tm;
public class C {
  public int f = 1;
  public int same(int x) { return x + 1; }
  public int moved(int x) {
    int y = x;
    if (y > 100) {
      return 0;
    }
    return y * 2;
  }
  public int sig(int b) { return b; }
}
"""
DRIVER_BUGGY = """package tm;
public class D { public static void main(String[] a) { C c = new C();
  for (int i = 0; i < 30; i++) { c.same(i); c.moved(i); c.sig(i); c.gone(); } } }
"""
DRIVER_FIXED = """package tm;
public class D { public static void main(String[] a) { C c = new C();
  for (int i = 0; i < 30; i++) { c.same(i); c.moved(i); c.sig(i); } } }
"""
M = "tm.C."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default=None)
    args = ap.parse_args()
    jar = os.environ.get("DAIKON_JAR")
    if not jar or not Path(jar).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at daikon.jar (got {jar!r})")
    jar = str(Path(jar).resolve())
    if args.keep:
        w = Path(args.keep).resolve()
        w.mkdir(parents=True, exist_ok=False)
        run(w, jar)
    else:
        with tempfile.TemporaryDirectory(prefix="daikon_trace_map_") as d:
            run(Path(d), jar)


def trace(w: Path, jar: str, tag: str, src: str, driver: str) -> Path:
    d = w / tag / "tm"
    d.mkdir(parents=True)
    (d / "C.java").write_text(src)
    (d / "D.java").write_text(driver)
    rc.run_logged(["javac", "-g", "-d", w / tag / "classes", d / "C.java", d / "D.java"], w / "logs" / f"javac_{tag}.log")
    rc.run_logged(["java", "-cp", f"{w / tag / 'classes'}:{jar}", "daikon.Chicory", r"--ppt-select-pattern=^tm\.C",
                   f"--dtrace-file={tag}.dtrace.gz", "tm.D"], w / "logs" / f"chicory_{tag}.log", cwd=w)
    return w / f"{tag}.dtrace.gz"


def run(w: Path, jar: str):
    failures = []

    def expect(cond, what):
        print(("PASS  " if cond else "FAIL  ") + what)
        if not cond:
            failures.append(what)

    logs = w / "logs"
    tb, tf = trace(w, jar, "buggy", BUGGY, DRIVER_BUGGY), trace(w, jar, "fixed", FIXED, DRIVER_FIXED)
    inv = w / "invA.inv.gz"
    rc.run_daikon(jar, tb, inv, r"^tm\.C", logs / "daikon.log")
    inv_txt = w / "invariantsA.txt"
    rc.print_invariants(jar, inv, inv_txt, logs / "print.log")
    text = inv_txt.read_text()
    cc = rc.compile_checker(jar, w / "cc", logs / "javac_checker.log")

    tm = build_map([tb], tf)
    st = tm.method_status
    expect(st.get(M + "same(int)") == "identical", f"same(): identical (got {st.get(M + 'same(int)')})")
    expect(st.get(M + "moved(int)") == "exits_shifted", f"moved(): exits_shifted (got {st.get(M + 'moved(int)')})")
    expect(st.get(M + "sig(int)") == "changed", f"sig(): changed (got {st.get(M + 'sig(int)')})")
    expect(st.get(M + "gone()") == "missing", f"gone(): missing (got {st.get(M + 'gone()')})")
    expect(tm.point_status.get("tm.C:::OBJECT") == "identical", "OBJECT: identical")

    mapped = w / "fixed.mapped.dtrace.gz"
    write_mapped(tf, mapped, tm)
    res = rc.check_one(jar, cc, inv, text, mapped, w / "fixed_check", logs, "_fixed", 5)
    out = {(o["ppt"], o["invariant"]): o for o in res["_outcomes"]}

    def final(k):
        s, forced, _ = ppt_status(tm, k[0])
        return forced or out[k]["verdict"]

    by_ppt = {}
    for k in out:
        by_ppt.setdefault(k[0], []).append(k)
    for ppt, ks in sorted(by_ppt.items()):
        point = ppt.split(":::")[1]
        verdicts = {final(k) for k in ks}
        if ppt.startswith(M + "same(") or (ppt.startswith(M + "moved(") and point in ("ENTER", "EXIT")):
            expect(verdicts <= {"HELD"} and all(out[k]["evaluations"] > 0 for k in ks),
                   f"{ppt}: {len(ks)} candidate(s) checked and HELD (got {verdicts})")
        elif ppt.startswith(M + "moved(") and re.fullmatch(r"EXIT\d+", point):
            expect(verdicts == {"UNEXERCISED"}, f"{ppt}: shifted EXITnn candidates UNEXERCISED, never HELD (got {verdicts})")
        elif ppt.startswith((M + "sig(", M + "gone(")):
            expect(verdicts == {"UNCHECKABLE"}, f"{ppt}: UNCHECKABLE (got {verdicts})")
    expect(any(p.startswith(M + "moved(") and p.endswith(":::EXIT") for p in by_ppt),
           "fixture has combined-EXIT candidates for the shifted method")
    # Shifted EXITnn at the invariant level (Daikon may print no EXITnn candidate for them):
    recs = rc.load_records(w / "fixed_check" / "checker_records.jsonl")
    nn = [r for r in recs if r["ppt"].startswith(M + "moved(") and re.search(r":::EXIT\d+$", r["ppt"])]
    expect(bool(nn) and all(r["evaluations"] == 0 for r in nn),
           f"moved() buggy EXITnn: all {len(nn)} invariants get 0 evaluations on the mapped fixed trace")
    expect(bool(nn) and all(ppt_status(tm, r["ppt"])[1] == "UNEXERCISED" for r in nn),
           "moved() buggy EXITnn: ppt_status forces UNEXERCISED")

    raw, err = run_check("raw fixed", lambda: rc.check_one(jar, cc, inv, text, tf, w / "raw_check", logs, "_raw", 5))
    expect(raw is None and err, "raw fixed trace: Daikon's failure captured by run_check, no crash")

    shifted = w / "shifted.dtrace.gz"
    with gzip.open(tb, "rt") as r, gzip.open(shifted, "wt") as o:
        for line in r:
            o.write(re.sub(r":::EXIT(\d+)$", r":::EXIT7\1", line))
    tm2 = build_map([tb], shifted)
    m2 = w / "shifted.mapped.dtrace.gz"
    write_mapped(shifted, m2, tm2)
    a = {(o["ppt"], o["invariant"]): o for o in rc.check_one(jar, cc, inv, text, tb, w / "orig", logs, "_o", 5)["_outcomes"]}
    b = {(o["ppt"], o["invariant"]): o for o in rc.check_one(jar, cc, inv, text, m2, w / "shift", logs, "_s", 5)["_outcomes"]}
    diffs = [k for k in a if not re.search(r":::EXIT\d+$", k[0]) and
             (a[k]["evaluations"], a[k]["verdict"]) != (b[k]["evaluations"], b[k]["verdict"])]
    expect(not diffs, f"renamed exits: ENTER/EXIT/OBJECT counts unchanged ({len(a)} candidates, diffs={diffs[:3]})")

    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
