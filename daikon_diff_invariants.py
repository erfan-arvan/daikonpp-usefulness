"""Parse daikon.PrintInvariants text output and diff two invariant sets.

Daikon has no built-in "check this specific invariant against a new trace"
mode the way Oca's runtime probes do. The equivalent signal used here:

  - Run Daikon on trace A (suite minus the bug-revealing test) -> set A.
  - Run Daikon on traces [A, bug] merged -> set A_plus_bug.
  - An invariant in A that is MISSING from A_plus_bug was contradicted by a
    sample in the bug trace -- Daikon simply stops reporting anything any
    observed sample violates. That's the Daikon-side analog of "FALSIFIED".
  - An invariant in A that is still present in A_plus_bug survived -> "HELD".
  - Invariants that appear only in A_plus_bug (not proposed from A) are
    ignored, mirroring Oca's model where only candidates from the
    without-test phase are candidates at all.

Verified against real `daikon.PrintInvariants` output (daikon.jar built
from the codespecs/daikon source, version 5.9.1) on a small hand-written
Java program. Confirmed shape:

    ===========================================================================
    Calc.add(int, int):::ENTER
    arg0 one of { 1, 5 }
    arg1 one of { -3, 2 }
    ===========================================================================
    Calc.add(int, int):::EXIT
    return one of { 2, 3 }

Two things the first version of this parser got wrong, fixed after seeing
real output:

  - The `===`-separator line does NOT precede the very first ppt block in
    the file (it starts directly with e.g. `Calc:::OBJECT`) -- treating
    only "line after a separator" as a ppt header silently dropped that
    first block. Fixed by classifying any line containing ":::" as a ppt
    header directly, independent of separator lines (which are otherwise
    ignored).
  - Ppt names can repeat with different exit-line suffixes (e.g. multiple
    `:::EXIT56`, `:::EXIT62` for a method with several return statements);
    these are kept as distinct ppts, matching how Daikon reports them.
"""
from __future__ import annotations

from pathlib import Path

_SEP_CHARS = "="


def parse_daikon_invariants(text: str) -> dict[str, set[str]]:
    ppts: dict[str, set[str]] = {}
    current: str | None = None

    for raw_line in text.splitlines():
        s = raw_line.strip()
        if not s:
            continue
        if set(s) == {_SEP_CHARS}:
            continue
        if ":::" in s:
            current = s
            ppts.setdefault(current, set())
            continue
        if current is not None:
            ppts[current].add(s)

    return ppts


def diff_invariants(
    before: dict[str, set[str]], after: dict[str, set[str]]
) -> list[tuple[str, str, str]]:
    """Returns (ppt, invariant, verdict) for every invariant proposed in
    `before`. verdict is HELD if still present in `after` at the same ppt,
    else FALSIFIED."""
    rows = []
    for ppt, invs in before.items():
        after_invs = after.get(ppt, set())
        for inv in invs:
            verdict = "HELD" if inv in after_invs else "FALSIFIED"
            rows.append((ppt, inv, verdict))
    return rows


def load_and_diff(before_path: Path, after_path: Path) -> list[tuple[str, str, str]]:
    before = parse_daikon_invariants(before_path.read_text(errors="replace"))
    after = parse_daikon_invariants(after_path.read_text(errors="replace"))
    return diff_invariants(before, after)
