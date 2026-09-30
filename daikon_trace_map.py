#!/usr/bin/env python3
"""Map a Chicory trace of the FIXED version onto the program points of the
BUGGY version's invariants, keeping only what matches unambiguously.

Daikon's checker reads a trace against a frozen invA: a ppt name that exists
in invA is decoded with invA's variable declarations. A fixed-version ppt
with the same name but a different declaration would be misread (or crash
the checker), and a numbered exit EXITnn is named after a source line, so
after the fix the same number can be a different return statement.

Per method (ppt name before ":::"), comparing the declaration blocks (all
lines after the "ppt" line) of the fixed trace with the buggy reference
traces:
  identical      ENTER and every EXITnn declared identically, same EXITnn
                 numbers -> kept as is
  exits_shifted  ENTER and exit declarations identical, but the EXITnn
                 numbers differ (lines moved) -> the fixed EXITnn are renamed
                 to EXIT<SHIFT_BASE + nn>, so they feed the combined EXIT
                 (unambiguous) but never an invA EXITnn (whose candidates stay
                 UNEXERCISED)
  changed        some declaration differs -> the method's declarations and
                 samples are dropped (candidates UNCHECKABLE)
  missing        not declared in the fixed trace -> nothing to drop
                 (candidates UNCHECKABLE)
  new            only in the fixed trace -> dropped
OBJECT / CLASS points: identical declaration -> kept, else changed/missing/new
(dropped). Records of dropped points are removed from the mapped trace.

Library use: build_map(reference_traces, fixed_trace) -> TraceMap;
write_mapped(fixed_trace, out, tmap); ppt_status(tmap, invA_ppt_name).
CLI: python3 daikon_trace_map.py REF.dtrace.gz [REF2 ...] --fixed F.dtrace.gz --out M.dtrace.gz --map map.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SHIFT_BASE = 900000
_EXIT_NN = re.compile(r"^EXIT(\d+)$")
HEADER_PREFIXES = ("decl-version", "var-comparability", "input-language", "#")


def _open(path: Path):
    return gzip.open(path, "rt", errors="replace") if str(path).endswith(".gz") else open(path, errors="replace")


def records(path: Path):
    """Yields each blank-line-separated record of a dtrace as a list of lines
    (comment lines dropped)."""
    rec: list[str] = []
    with _open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("//"):
                continue
            if line.strip() == "":
                if rec:
                    yield rec
                    rec = []
            else:
                rec.append(line)
    if rec:
        yield rec


def decl_name(line: str) -> str:
    """'ppt a.B.m(int,\\_int):::ENTER' -> 'a.B.m(int, int):::ENTER'"""
    return line[4:].replace("\\_", " ").replace("\\\\", "\\")


def split(name: str) -> tuple[str, str]:
    method, _, point = name.partition(":::")
    return method, point


def read_decls(paths) -> tuple[dict[str, tuple], set[str]]:
    """{ppt name: declaration body}, and names declared with conflicting bodies."""
    decls: dict[str, tuple] = {}
    conflicts: set[str] = set()
    for p in paths:
        for rec in records(Path(p)):
            if rec[0].startswith("ppt "):
                name, body = decl_name(rec[0]), tuple(l.rstrip() for l in rec[1:])
                if name in decls and decls[name] != body:
                    conflicts.add(name)
                decls.setdefault(name, body)
    return decls, conflicts


@dataclass
class TraceMap:
    method_status: dict[str, str] = field(default_factory=dict)   # method -> status
    point_status: dict[str, str] = field(default_factory=dict)    # OBJECT/CLASS ppt -> status
    rename: dict[str, str] = field(default_factory=dict)          # fixed ppt name -> mapped name
    keep: set[str] = field(default_factory=set)                   # fixed ppt names kept (pre-rename)
    detail: dict[str, str] = field(default_factory=dict)          # method/ppt -> why

    def to_json(self) -> dict:
        return {"method_status": self.method_status, "point_status": self.point_status,
                "rename": self.rename, "kept": sorted(self.keep), "detail": self.detail,
                "shift_base": SHIFT_BASE}


def build_map(reference_traces, fixed_trace) -> TraceMap:
    bug, bug_conf = read_decls(reference_traces)
    fix, fix_conf = read_decls([fixed_trace])
    tm = TraceMap()
    methods: dict[str, dict[str, dict]] = {}
    for side, decls in (("bug", bug), ("fix", fix)):
        for name in decls:
            m, point = split(name)
            if point in ("OBJECT", "CLASS") or not (point == "ENTER" or _EXIT_NN.match(point)):
                continue
            methods.setdefault(m, {"bug": {}, "fix": {}})[side][point] = name

    for m, sides in sorted(methods.items()):
        b, f = sides["bug"], sides["fix"]
        if not f:
            tm.method_status[m] = "missing"
            continue
        if not b:
            tm.method_status[m] = "new"
            continue
        names = list(b.values()) + list(f.values())
        if any(n in bug_conf or n in fix_conf for n in names):
            tm.method_status[m] = "changed"
            tm.detail[m] = "conflicting declarations within one version"
            continue
        if ("ENTER" in b) != ("ENTER" in f) or ("ENTER" in b and bug[b["ENTER"]] != fix[f["ENTER"]]):
            tm.method_status[m] = "changed"
            tm.detail[m] = "ENTER declaration differs"
            continue
        b_exits = {p: bug[n] for p, n in b.items() if p != "ENTER"}
        f_exits = {p: fix[n] for p, n in f.items() if p != "ENTER"}
        bodies = set(b_exits.values()) | set(f_exits.values())
        if len(bodies) > 1:
            tm.method_status[m] = "changed"
            tm.detail[m] = "exit declarations differ"
            continue
        tm.keep.update(f.values())
        if set(b_exits) == set(f_exits):
            tm.method_status[m] = "identical"
        else:
            tm.method_status[m] = "exits_shifted"
            tm.detail[m] = f"buggy exits {sorted(b_exits)} vs fixed {sorted(f_exits)}"
            for p, n in f.items():
                if p != "ENTER":
                    nn = int(_EXIT_NN.match(p).group(1))
                    tm.rename[n] = f"{m}:::EXIT{SHIFT_BASE + nn}"

    for name in sorted(set(bug) | set(fix)):
        m, point = split(name)
        if point not in ("OBJECT", "CLASS"):
            continue
        if name not in fix:
            tm.point_status[name] = "missing"
        elif name not in bug:
            tm.point_status[name] = "new"
        elif name in bug_conf or name in fix_conf or bug[name] != fix[name]:
            tm.point_status[name] = "changed"
        else:
            tm.point_status[name] = "identical"
            tm.keep.add(name)
    return tm


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace(" ", "\\_")


def write_mapped(fixed_trace: Path, out: Path, tm: TraceMap) -> dict:
    """Writes the mapped trace atomically; returns record counts."""
    tmp = out.with_name(".partial-" + out.name)
    counts = {"decls_kept": 0, "decls_dropped": 0, "samples_kept": 0, "samples_dropped": 0, "renamed_samples": 0}
    with gzip.open(tmp, "wt") as w:
        for rec in records(fixed_trace):
            head = rec[0]
            if head.startswith("ppt "):
                name = decl_name(head)
                if name in tm.keep:
                    new = tm.rename.get(name, name)
                    w.write("ppt " + _escape(new) + "\n" + "\n".join(rec[1:]) + "\n\n")
                    counts["decls_kept"] += 1
                else:
                    counts["decls_dropped"] += 1
            elif head.startswith(HEADER_PREFIXES):
                w.write("\n".join(rec) + "\n\n")
            elif head in tm.keep:
                if head in tm.rename:
                    rec = [tm.rename[head]] + rec[1:]
                    counts["renamed_samples"] += 1
                w.write("\n".join(rec) + "\n\n")
                counts["samples_kept"] += 1
            else:
                counts["samples_dropped"] += 1
    tmp.replace(out)
    return counts


def ppt_status(tm: TraceMap, ppt: str) -> tuple[str, str | None, str | None]:
    """For an invA ppt name: ('ok', None, None) if checker verdicts on the
    mapped fixed trace can be used, else (status, forced verdict, reason)."""
    m, point = split(ppt)
    if point in ("OBJECT", "CLASS"):
        st = tm.point_status.get(ppt, "missing")
        if st == "identical":
            return "ok", None, None
        return st, "UNCHECKABLE", f"{point} declaration {st} in the fixed version"
    st = tm.method_status.get(m, "missing")
    if st in ("changed", "missing", "new"):
        return st, "UNCHECKABLE", f"method declaration {st} in the fixed version ({tm.detail.get(m, '')})".rstrip(" ()")
    if st == "exits_shifted" and _EXIT_NN.match(point):
        return st, "UNEXERCISED", f"EXITnn line numbers shifted in the fixed version ({tm.detail.get(m)})"
    if point == "ENTER" or point == "EXIT" or _EXIT_NN.match(point):
        return "ok", None, None
    return "unknown_point", "UNCHECKABLE", f"unsupported program point kind {point!r}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("reference", nargs="+", help="buggy-version traces (same version as invA)")
    ap.add_argument("--fixed", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--map", required=True)
    args = ap.parse_args()
    tm = build_map([Path(p) for p in args.reference], Path(args.fixed))
    counts = write_mapped(Path(args.fixed), Path(args.out), tm)
    Path(args.map).write_text(json.dumps({**tm.to_json(), "counts": counts}, indent=1))
    print(json.dumps({"methods": {s: sum(1 for v in tm.method_status.values() if v == s)
                                  for s in set(tm.method_status.values())}, **counts}))


if __name__ == "__main__":
    main()
