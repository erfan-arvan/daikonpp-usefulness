#!/usr/bin/env python3
"""Standalone fixture test for test attribution (DaikonMarkedTestRunner +
DaikonTestMarker + daikon_test_windows.py), as used by validate_daikon_strict.py.
Needs java/javac, daikon.jar and a JUnit 4 jar (default: the one under the
defects4j install, as the pipeline uses; or --junit PATH).

Part 1, real run under Chicory. wf.P.f(x, y) is called by:
  wft.WTest::normal     (i, i+1), i = 1..30          (not a triggering test)
  wft.WTest::trig       (-5, -9)                     (triggering)
  wft.WTest::threaded   (7, 1) in a new thread, then (8, 2)   (triggering, but
                        it starts a thread -> ambiguous window, must not count)
  wft.W3Test::testX     (3, 4)  (JUnit 3)            (triggering)
Checks: window statuses; the window trace holds exactly the f samples x=-5
and x=3; with invA inferred from WTest::normal only, "x < y" is violated in
the windows only by (-5, -9) -- never by the threaded test's samples.

Part 2, synthetic traces: nested begin, end with another id, unknown id and a
truncated (unterminated) window are all ambiguous and dropped.

Usage:
    DAIKON_JAR=/path/to/daikon.jar python3 test_daikon_test_windows_fixture.py [--junit JAR] [--keep DIR]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DAIKON_JAVA_XMX", "2g")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_daikon_checker_bug as rc  # noqa: E402
from daikon_test_windows import BEGIN, END, extract, parse_marks  # noqa: E402
from daikon_trace_map import records  # noqa: E402

THIS = Path(__file__).resolve().parent
P_SRC = "package wf;\npublic class P { public static int f(int x, int y) { return x; } }\n"
WTEST = """package wft;
import org.junit.Test;
public class WTest {
  @Test public void normal() { for (int i = 1; i <= 30; i++) wf.P.f(i, i + 1 + i % 3); }
  @Test public void trig() { wf.P.f(-5, -9); }
  @Test public void threaded() throws Exception {
    Thread t = new Thread(() -> wf.P.f(7, 1));
    t.start();
    t.join();
    wf.P.f(8, 2);
  }
}
"""
W3TEST = """package wft;
public class W3Test extends junit.framework.TestCase {
  public void testX() { wf.P.f(3, 4); }
}
"""
TRIG = ["wft.WTest::trig", "wft.WTest::threaded", "wft.W3Test::testX"]
ENTER_F = "wf.P.f(int, int):::ENTER"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--junit", default=None)
    ap.add_argument("--keep", default=None)
    args = ap.parse_args()
    jar = os.environ.get("DAIKON_JAR")
    if not jar or not Path(jar).is_file():
        sys.exit(f"ERROR: DAIKON_JAR must point at daikon.jar (got {jar!r})")
    jar = str(Path(jar).resolve())
    if args.junit:
        junit = str(Path(args.junit).resolve())
    else:
        from run_daikon_usefulness_bug import find_junit4_jar
        junit = find_junit4_jar()
    hamcrest = sorted(Path(junit).parent.rglob("hamcrest-core*.jar")) or \
        sorted(Path(junit).parents[3].rglob("hamcrest-core*.jar")) if len(Path(junit).parents) > 3 else []
    cp_junit = ":".join([junit] + [str(h) for h in hamcrest[:1]])
    if args.keep:
        w = Path(args.keep).resolve()
        w.mkdir(parents=True, exist_ok=False)
        run(w, jar, cp_junit)
    else:
        with tempfile.TemporaryDirectory(prefix="daikon_windows_") as d:
            run(Path(d), jar, cp_junit)


def f_values(trace: Path) -> list[str]:
    vals = []
    for rec in records(trace):
        if rec[0] == ENTER_F:
            vals.append(rec[rec.index("x") + 1])
    return vals


def synthetic(path: Path, recs: list[list[str]]):
    with gzip.open(path, "wt") as w:
        w.write("decl-version 2.0\nvar-comparability none\n\n")
        for r in recs:
            w.write("\n".join(r) + "\n\n")


def marker(kind, nonce, tid, threads=None):
    rec = [BEGIN if kind == "begin" else END, "this_invocation_nonce", str(nonce), "testId", str(tid), "1"]
    if threads is not None:
        rec += ["threadsStarted", str(threads), "1"]
    return rec


def sample(nonce, x):
    return [ENTER_F, "this_invocation_nonce", str(nonce), "x", str(x), "1", "y", "0", "1"]


def run(w: Path, jar: str, cp_junit: str):
    failures = []

    def expect(cond, what):
        print(("PASS  " if cond else "FAIL  ") + what)
        if not cond:
            failures.append(what)

    logs = w / "logs"
    src = w / "src"
    (src / "wf").mkdir(parents=True)
    (src / "wft").mkdir(parents=True)
    (src / "wf" / "P.java").write_text(P_SRC)
    (src / "wft" / "WTest.java").write_text(WTEST)
    (src / "wft" / "W3Test.java").write_text(W3TEST)
    classes = w / "classes"
    rc.run_logged(["javac", "-g", "-cp", cp_junit, "-d", classes, src / "wf" / "P.java", src / "wft" / "WTest.java",
                   src / "wft" / "W3Test.java", THIS / "DaikonTestMarker.java", THIS / "DaikonMarkedTestRunner.java"],
                  logs / "javac.log")
    select = r"(?:^wf\.)|^DaikonTestMarker\."
    omit = r"junit\.|org\.junit\.|sun\.|java\.|com\.sun\.proxy|^wft\."

    def chicory(name, specs):
        lines = rc.run_logged(["java", "-cp", f"{classes}:{cp_junit}:{jar}", "daikon.Chicory",
                               f"--ppt-select-pattern={select}", f"--ppt-omit-pattern={omit}",
                               f"--dtrace-file={name}", "DaikonMarkedTestRunner", *specs], logs / f"{name}.log", cwd=w)
        return w / name, parse_marks(lines)

    full, marks = chicory("full.dtrace.gz", ["wft.WTest", "wft.W3Test"])
    expect(len(marks) == 4 and all(len(v) == 1 for v in marks.values()),
           f"runner printed one MARK per test (got {marks})")
    wt = w / "windows.dtrace.gz"
    s = extract(full, wt, TRIG, marks)
    pt = s["per_test"]
    expect(pt.get("wft.WTest::trig") == {"ok": 1}, f"trig: one unambiguous window (got {pt.get('wft.WTest::trig')})")
    expect(pt.get("wft.W3Test::testX") == {"ok": 1}, f"JUnit3 testX: one unambiguous window (got {pt.get('wft.W3Test::testX')})")
    expect(pt.get("wft.WTest::threaded") == {"ambiguous_threads": 1},
           f"threaded: ambiguous because it started a thread (got {pt.get('wft.WTest::threaded')})")
    vals = sorted(f_values(wt), key=int)
    expect(vals == ["-5", "3"], f"window trace holds exactly f(-5) and f(3) (got {vals})")
    expect({"7", "8", "-5", "3"} <= set(f_values(full)), "full trace has all f samples incl. the threaded 7, 8")

    # invA from the non-triggering test only; check it against the windows
    train, _ = chicory("train.dtrace.gz", ["wft.WTest::normal"])
    inv = w / "invA.inv.gz"
    rc.run_daikon(jar, train, inv, r"^wf\.", logs / "daikon.log")
    inv_txt = w / "invariantsA.txt"
    rc.print_invariants(jar, inv, inv_txt, logs / "print.log")
    cc = rc.compile_checker(jar, w / "cc", logs / "javac_checker.log")
    res = rc.check_one(jar, cc, inv, inv_txt.read_text(), wt, w / "check", logs, "_w", 50)
    o = {(x["ppt"], x["invariant"]): x for x in res["_outcomes"]}.get((ENTER_F, "x < y"), {})
    bad = [(sm["values"].get("x"), sm["values"].get("y")) for sm in o.get("violating_samples", [])]
    expect(o.get("verdict") == "FALSIFIED" and bad == [("-5", "-9")],
           f"'x < y' violated in the triggering windows only by (-5, -9) (got {o.get('verdict')}, {bad})")
    expect(o.get("evaluations") == 2, f"evaluated on exactly the 2 attributed samples (got {o.get('evaluations')})")

    # synthetic malformed markers
    ids = {"1": ["t.A::a"], "2": ["t.A::b"], "3": ["t.A::a", "t.A::c"]}
    cases = {
        "nested": [marker("begin", 1, 1), sample(2, 10), marker("begin", 3, 2), sample(4, 11),
                   marker("end", 5, 2, 0), marker("end", 6, 1, 0)],
        "mismatched_end": [marker("begin", 1, 1), sample(2, 10), marker("end", 3, 2, 0)],
        "unknown_id": [marker("begin", 1, 3), sample(2, 10), marker("end", 3, 3, 0)],
        "unterminated": [marker("begin", 1, 1), sample(2, 10)],
        "clean": [marker("begin", 1, 1), sample(2, 10), marker("end", 3, 1, 0), sample(4, 99)],
    }
    for name, recs in cases.items():
        tin, tout = w / f"syn_{name}.dtrace.gz", w / f"syn_{name}.out.dtrace.gz"
        synthetic(tin, recs)
        s = extract(tin, tout, ["t.A::a", "t.A::b", "t.A::c"], ids)
        kept = f_values(tout)
        if name == "clean":
            expect(kept == ["10"], f"synthetic clean window keeps its sample only, not the one after end (got {kept})")
        else:
            expect(kept == [], f"synthetic {name}: nothing attributed (got {kept}; statuses {s['per_test']})")

    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
