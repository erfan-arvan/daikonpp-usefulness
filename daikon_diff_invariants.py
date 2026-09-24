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

import re
from pathlib import Path

_SEP_CHARS = "="

# Daikon invariants that assert equality/membership against a specific
# LITERAL constant (a quoted string, a bare number) rather than a relation
# between two program values are the dominant source of noise in this RQ5
# comparison: with only a small, per-bug trace to infer from, Daikon happily
# reports "always one of {these 2-3 strings I happened to see}" or "always
# equals this exact number", and ANY single differing value in the
# bug-triggering test trivially "falsifies" it -- with zero relation to the
# actual bug. Confirmed directly: e.g. Cli_36 produced 111 FALSIFIED
# invariants after excluding test-fixture ppts, and every single one was
# this shape (`... one of { "age", "size" }`, `this.argName.toString ==
# "SIZE"`, `this.startTime == 1790227646867L`, etc.) over genuine
# production code, not test scaffolding.
#
# Relational invariants between two variables/expressions (`a == b`,
# `a > b`, `!= null`, `== true`/`== false`) don't have this problem -- they
# hold or fail based on the RELATIONSHIP, not on having observed every
# possible concrete value, so they're not filtered here. Deliberately
# excludes null/true/false comparisons from the literal-equality pattern
# below since those are categorical (binary), not a numeric/string
# coincidence.
_ENUM_PATTERN = re.compile(r"\bone of \{")
_DEGENERATE_PATTERN = re.compile(r"has only one value")
_LITERAL_EQ_PATTERN = re.compile(
    r'==\s*(-?\d+L?|"[^"]*")\s*(?:$|;|\s)|(-?\d+L?|"[^"]*")\s*=='
)


def is_overfit_prone_invariant(text: str) -> bool:
    """True if this invariant asserts equality/membership against a
    specific observed literal (enumeration or literal-equality) rather than
    a relation between two program values -- see the module-level comment
    for why these are excluded from the RQ5 comparison entirely."""
    return bool(
        _ENUM_PATTERN.search(text)
        or _DEGENERATE_PATTERN.search(text)
        or _LITERAL_EQ_PATTERN.search(text)
    )


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
    `before` that isn't overfit-prone (see is_overfit_prone_invariant).
    verdict is HELD if still present in `after` at the same ppt, else
    FALSIFIED."""
    rows = []
    for ppt, invs in before.items():
        after_invs = after.get(ppt, set())
        for inv in invs:
            if is_overfit_prone_invariant(inv):
                continue
            verdict = "HELD" if inv in after_invs else "FALSIFIED"
            rows.append((ppt, inv, verdict))
    return rows


def load_and_diff(before_path: Path, after_path: Path) -> list[tuple[str, str, str]]:
    before = parse_daikon_invariants(before_path.read_text(errors="replace"))
    after = parse_daikon_invariants(after_path.read_text(errors="replace"))
    return diff_invariants(before, after)
