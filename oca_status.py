#!/usr/bin/env python3
"""Whether an Oca run (run_usefulness_bug.py) of one bug really finished,
for the original (outputs_usefulness/) and relaxed
(outputs_usefulness_relaxed/) runs.

A run is COMPLETE only if
  * its RUN_COMPLETE marker was written by run_usefulness_bug.py itself
    (content "<P>-<B>"; the script deletes it before a re-run starts), or
  * its own job log shows it: in the latest attempt of that bug found in the
    logs, ">>> DONE <P>-<B>" follows ">>> Phase A (without_test): <P>-<B>"
    with no phase FAILED in between, and none of the four jsonl result files
    was modified after that log was last written.
Nothing is inferred from SLURM accounting and no marker is ever written.

Logs searched (under ROOT, the directory holding outputs_usefulness/):
  original: *.out and logs/*.out except oca-relaxed.*, plus
            outputs_usefulness/batch_logs/<P>_<B>.log
  relaxed:  oca-relaxed.*.out (and logs/oca-relaxed.*.out) that announce
            ">>> Relaxed Oca for <P>-<B> -> <that bug's relaxed dir>"
A per-file event index is cached in ROOT/.oca_log_index.json (refreshed
when a log's size or mtime changes).

States: complete | unverified (result files present, a non-genuine marker
such as "backfilled from sacct" or none, and no log proving completion) |
failed (latest attempt logged a FAILED phase) | incomplete (latest attempt
has neither DONE nor FAILED: still running, or killed) | not_started.

Usage:
    python3 oca_status.py outputs_usefulness/Cli_40 [outputs_usefulness_relaxed/JxPath_18 ...]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

JSONL = ("daikonpp_registry_without_test.jsonl", "daikonpp_outcomes_without_test.jsonl",
         "daikonpp_registry_with_test.jsonl", "daikonpp_outcomes_with_test.jsonl")
RELAXED_DIR = "outputs_usefulness_relaxed"
MTIME_SLACK = 120  # seconds between the last result write and the log's last write

_EVENT = re.compile(r"^(?:>>> Phase A \(without_test\): (?P<a>\S+)$"
                    r"|>>> DONE (?P<done>\S+)$"
                    r"|\[SYSTEM\] (?P<f>\S+) phase=\S+ FAILED\b"
                    r"|>>> Relaxed Oca for (?P<rel>\S+) -> (?P<out>\S+)$)")
_index_cache: dict[Path, dict] = {}


def _scan(path: Path) -> list:
    """[(kind, bug, extra)] in file order; kind in a/done/f/rel."""
    events = []
    with open(path, "rb") as fh:
        for raw in fh:
            if not (raw.startswith(b">>> ") or raw.startswith(b"[SYSTEM] ")):
                continue
            m = _EVENT.match(raw.decode("utf-8", "replace").rstrip("\r\n"))
            if not m:
                continue
            for k in ("a", "done", "f"):
                if m[k]:
                    events.append((k, m[k], None))
            if m["rel"]:
                events.append(("rel", m["rel"], m["out"]))
    return events


def _log_files(root: Path) -> list[Path]:
    files = [*root.glob("*.out"), *root.glob("logs/*.out"), *root.glob("outputs_usefulness/batch_logs/*.log")]
    return sorted(p for p in files if p.is_file())


def _index(root: Path) -> dict:
    """{str(path): {"size", "mtime", "events"}} for every log under root."""
    root = root.resolve()
    if root in _index_cache:
        return _index_cache[root]
    cache_path = root / ".oca_log_index.json"
    try:
        cached = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cached = {}
    index, changed = {}, False
    for p in _log_files(root):
        st = p.stat()
        c = cached.get(str(p))
        if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime:
            index[str(p)] = c
        else:
            index[str(p)] = {"size": st.st_size, "mtime": st.st_mtime, "events": _scan(p)}
            changed = True
    if changed or set(index) != set(cached):
        try:
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(index))
            tmp.replace(cache_path)
        except OSError:
            pass
    _index_cache[root] = index
    return index


def _attempts(root: Path, bug: str, relaxed: bool, bug_dir: Path) -> list:
    """Every attempt of bug in the matching logs: (log mtime, position, outcome, log)."""
    out = []
    for path, entry in _index(root).items():
        name = Path(path).name
        if relaxed != name.startswith("oca-relaxed"):
            continue
        events = entry["events"]
        if relaxed and not any(k == "rel" and b == bug and Path(o).name == bug_dir.name
                               and Path(o).parent.name == RELAXED_DIR for k, b, o in events):
            continue
        if not relaxed and name.endswith(".log") and name != f"{bug_dir.name}.log":
            continue
        starts = [i for i, (k, b, _) in enumerate(events) if k == "a" and b == bug]
        for n, s in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else len(events)
            seg = [(k, b) for k, b, _ in events[s:end] if b == bug]
            outcome = ("failed" if ("f", bug) in seg else "complete" if ("done", bug) in seg else "incomplete")
            out.append((entry["mtime"], s, outcome, path))
    return sorted(out)


def oca_status(bug_dir: Path) -> tuple[str, str]:
    """(state, evidence) for one Oca output dir <...>/<P>_<B>."""
    bug_dir = Path(bug_dir).resolve()
    relaxed = bug_dir.parent.name == RELAXED_DIR
    project, _, bug_id = bug_dir.name.rpartition("_")
    bug = f"{project}-{bug_id}"
    if not bug_dir.is_dir():
        return "not_started", "no output dir"
    marker = bug_dir / "RUN_COMPLETE"
    if marker.is_file() and marker.read_text().strip() == bug:
        return "complete", "RUN_COMPLETE written by run_usefulness_bug.py"
    files = [bug_dir / f for f in JSONL]
    have = all(f.is_file() and f.stat().st_size > 0 for f in files)
    attempts = _attempts(bug_dir.parent.parent, bug, relaxed, bug_dir)
    if attempts:
        mtime, _, outcome, log = attempts[-1]
        if outcome == "complete":
            if not have:
                return "incomplete", f"log {Path(log).name} says DONE but result files are missing"
            newest = max(f.stat().st_mtime for f in files)
            if newest > mtime + MTIME_SLACK:
                return "unverified", f"results modified after the last logged attempt ({Path(log).name})"
            return "complete", f"log {Path(log).name}: DONE, no FAILED phase"
        if outcome == "failed":
            return "failed", f"latest attempt FAILED ({Path(log).name})"
        return "incomplete", f"latest attempt has no DONE/FAILED: running or killed ({Path(log).name})"
    if have:
        why = f"marker says {marker.read_text().strip()!r}" if marker.is_file() else "no RUN_COMPLETE marker"
        return "unverified", f"result files present, {why}, no job log found"
    return "incomplete", "output dir without results, no job log found"


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for d in sys.argv[1:]:
        state, why = oca_status(Path(d))
        print(f"{d}: {state} -- {why}")


if __name__ == "__main__":
    main()
