#!/usr/bin/env python3
"""Keep only the samples of a marked Chicory trace (DaikonMarkedTestRunner +
DaikonTestMarker) that were recorded inside an UNAMBIGUOUS window of one of
the given tests.

A window is the records strictly between the ENTER records of
DaikonTestMarker.begin(id) and DaikonTestMarker.end(id, threadsStarted); the
runner's "MARK id class::method" lines map ids to tests. It is unambiguous
only if: the id maps to exactly one test, begin and end carry the same id,
no other begin comes in between (no nesting / overlap), the end record exists
(not truncated), and threadsStarted == 0 (no JVM thread was started while the
test ran, so no other thread can have interleaved samples). Everything else -- samples
outside any window, in other tests' windows, or in ambiguous windows -- is
dropped. Declarations and header records are kept (marker declarations
dropped), so the output checks against the same invA.

Test names are "class::method"; a JUnit4 parameterized "m[0]" matches "m".

Library: extract(trace, out, tests, id_names) -> summary dict. CLI:
    python3 daikon_test_windows.py IN.dtrace.gz OUT.dtrace.gz --marks M.json --test cls::m [--test ...] [--summary S.json]
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

from daikon_trace_map import decl_name, records

MARKER = "DaikonTestMarker."
BEGIN = "DaikonTestMarker.begin(int):::ENTER"
END = "DaikonTestMarker.end(int, long):::ENTER"
HEADERS = ("decl-version", "var-comparability", "input-language", "#")


def norm_test(name: str) -> str:
    cls, _, meth = name.partition("::")
    return cls + "::" + re.sub(r"\[.*\]$", "", meth)


def _values(rec: list[str]) -> dict[str, str]:
    body = rec[3:] if len(rec) > 2 and rec[1] == "this_invocation_nonce" else rec[1:]
    return {body[i]: body[i + 1] for i in range(0, len(body) - 2, 3)}


def parse_marks(lines) -> dict[str, list[str]]:
    """{id: [test names]} from the runner's "MARK id class::method" lines
    (more than one name for an id = ambiguous)."""
    marks: dict[str, list[str]] = {}
    for line in lines:
        m = re.match(r"^\[DaikonMarkedTestRunner\] MARK (\d+) (\S+)\s*$", line)
        if m:
            marks.setdefault(m.group(1), []).append(m.group(2))
    return marks


def extract(trace: Path, out: Path, tests, id_names: dict[str, list[str]]) -> dict:
    wanted = {norm_test(t) for t in tests}

    def test_of(test_id):
        names = id_names.get(test_id) if test_id is not None else None
        return names[0] if names and len(names) == 1 else None
    tmp = out.with_name(".partial-" + out.name)
    windows: list[dict] = []
    anomalies: list[str] = []
    counts = {"samples_kept": 0, "samples_outside_windows": 0, "samples_other_tests": 0,
              "samples_ambiguous_windows": 0, "marker_records": 0}
    cur: dict | None = None
    with gzip.open(tmp, "wt") as w:
        def close(win, status):
            win["status"] = status
            windows.append({k: v for k, v in win.items() if k != "buf"})
            if win["wanted"]:
                if status == "ok":
                    for rec in win["buf"]:
                        w.write("\n".join(rec) + "\n\n")
                    counts["samples_kept"] += len(win["buf"])
                else:
                    counts["samples_ambiguous_windows"] += win["samples"]

        for rec in records(trace):
            head = rec[0]
            if head.startswith("ppt "):
                if not decl_name(head).startswith(MARKER):
                    w.write("\n".join(rec) + "\n\n")
                continue
            if head.startswith(HEADERS):
                w.write("\n".join(rec) + "\n\n")
                continue
            if head.startswith(MARKER):
                counts["marker_records"] += 1
                vals = _values(rec)
                tid = vals.get("testId")
                name = test_of(tid)
                if head == BEGIN:
                    nested = cur is not None
                    if nested:
                        anomalies.append(f"begin({tid}={name}) inside window of {cur['id']}={cur['test']}")
                        close(cur, "ambiguous_nested")
                    cur = {"id": tid, "test": name, "wanted": name is not None and norm_test(name) in wanted,
                           "samples": 0, "buf": [], "nested": nested, "unknown_id": name is None}
                elif head == END:
                    if cur is None or cur["id"] != tid:
                        anomalies.append(f"end({tid}) without matching begin"
                                         + (f" (open window: {cur['id']})" if cur else ""))
                        if cur is not None:
                            close(cur, "ambiguous_mismatched_end")
                        cur = None
                        continue
                    try:
                        threads = int(vals.get("threadsStarted", "x"))
                    except ValueError:
                        threads = None
                    cur["threads_started"] = threads
                    if cur["unknown_id"]:
                        close(cur, "ambiguous_unknown_id")
                    elif cur["nested"]:
                        close(cur, "ambiguous_nested")
                    elif threads is None:
                        close(cur, "ambiguous_no_thread_count")
                    elif threads > 0:
                        close(cur, "ambiguous_threads")
                    else:
                        close(cur, "ok")
                    cur = None
                continue
            # an ordinary sample
            if cur is None:
                counts["samples_outside_windows"] += 1
            elif not cur["wanted"]:
                counts["samples_other_tests"] += 1
            else:
                cur["samples"] += 1
                cur["buf"].append(rec)
        if cur is not None:
            anomalies.append(f"window of {cur['test']} never ended (truncated trace)")
            close(cur, "ambiguous_unterminated")
    tmp.replace(out)
    per_test: dict[str, dict[str, int]] = {}
    for win in windows:
        if win["wanted"]:
            d = per_test.setdefault(norm_test(win["test"]), {})
            d[win["status"]] = d.get(win["status"], 0) + 1
    return {"tests": sorted(wanted), "per_test": per_test,
            "tests_without_ok_window": sorted(t for t in wanted if not per_test.get(t, {}).get("ok")),
            "windows": len(windows), "ambiguous_windows_all_tests": sum(w["status"] != "ok" for w in windows),
            "counts": counts, "anomalies": anomalies[:50],
            "wanted_windows": [w for w in windows if w["wanted"]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("out")
    ap.add_argument("--marks", required=True, help="json {id: [test names]} from the runner's MARK lines")
    ap.add_argument("--test", action="append", required=True)
    ap.add_argument("--summary")
    args = ap.parse_args()
    s = extract(Path(args.trace), Path(args.out), args.test, json.loads(Path(args.marks).read_text()))
    if args.summary:
        Path(args.summary).write_text(json.dumps(s, indent=1))
    print(json.dumps({k: s[k] for k in ("per_test", "tests_without_ok_window", "counts")}))


if __name__ == "__main__":
    main()
