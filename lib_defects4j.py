"""Shared helpers for the Oca (daikonplusplus) usefulness (RQ5) experiments.

Ported from the old daikonppTests scripts, updated for the current
daikonplusplus CLI/config surface (no more DP_REGISTRY_IN / DP_REGISTRY_READONLY,
which no longer exist) and rewritten to disable the bug-revealing test method(s)
with a small regex/brace-matching pass instead of the missing RemoveMethod /
RewriteMethodViaLLM JavaParser tool (whose sources were never included in the
scripts bundle).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def run(cmd, cwd=None, env=None, check=True):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, env=env, check=check)


def capture(cmd, cwd=None, env=None, timeout=300):
    return subprocess.check_output(cmd, cwd=cwd, env=env, text=True, timeout=timeout)


def _ensure_svn_stub() -> Path:
    """Ensures a minimal `svn` stub exists and returns its directory.

    defects4j's own Utils::print_env (invoked at the start of every
    `defects4j` command whenever D4J_DEBUG is set -- which this pipeline
    requires globally, to capture real build/test logs) does:
    `print_entry("SVN version", \\`svn --version --quiet\\`)`.

    When svn isn't installed at all, Perl's backtick call in LIST context
    (this is a function-call argument, hence list context) returns an EMPTY
    LIST when there is no stdout to capture -- not an empty string -- so
    print_entry receives only 1 argument instead of 2, and its own
    `@_ >= 2 || die "Invalid number of arguments!"` check crashes. This
    happens for EVERY project's every defects4j command on a host with no
    svn, including git-based ones (confirmed against Cli, which uses
    Vcs::Git) -- it is not specific to svn-based projects like Chart.

    A one-line stub that prints anything and exits 0 is enough to keep this
    diagnostic version check from crashing; it provides no real svn
    functionality, so it does NOT make svn-based projects (Chart) actually
    checkoutable -- exclude those from bugs.csv instead.
    """
    stub_dir = Path(__file__).resolve().parent / ".svn-stub"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "svn"
    if not stub.exists():
        stub.write_text(
            "#!/bin/sh\n"
            "echo 'svn, version 1.14.1 (stub -- real svn not installed on this host)'\n"
        )
        stub.chmod(0o755)
    return stub_dir


def d4j_env(base_env: dict | None = None) -> dict:
    """Returns an environment dict for running `defects4j` subcommands.

    Defects4J's own install docs (README, "Perl dependencies" section) state
    that Defects4J 2.x requires Java 8 -- some Ant/Major-compiler machinery
    in the framework, and some very old subject programs, do not build
    correctly under a newer JDK. This is a DIFFERENT JDK requirement than
    daikonplusplus (17+) or Daikon (built/tested here under 21), so the two
    cannot always share a single `module load` on the HPC side.

    If D4J_JAVA_HOME is set, this pins JAVA_HOME/PATH to it for the returned
    env, without touching the ambient JAVA_HOME used for daikonplusplus/Daikon
    calls elsewhere in the same script. If unset, defects4j runs under
    whatever JDK is already on PATH -- fine for projects that tolerate a
    newer JDK, but not guaranteed for all of them.

    Also prepends a stub `svn` to PATH -- see _ensure_svn_stub().
    """
    import os

    env = dict(base_env if base_env is not None else os.environ)
    d4j_home = env.get("D4J_JAVA_HOME")
    if d4j_home:
        env["JAVA_HOME"] = d4j_home
        env["PATH"] = f"{d4j_home}/bin:" + env.get("PATH", "")
    env["PATH"] = f"{_ensure_svn_stub()}:" + env.get("PATH", "")
    return env


def resolve_bug_ids(project: str, spec: str) -> list[str]:
    if spec == "latest":
        out = capture(["defects4j", "bids", "-p", project])
        ids = sorted((l.strip() for l in out.splitlines() if l.strip()), key=int)
        return [ids[-1]]
    if spec.isdigit():
        return [spec]
    if re.match(r"^\d+-\d+$", spec):
        a, b = map(int, spec.split("-"))
        return [str(i) for i in range(a, b + 1)]
    return [s.strip() for s in spec.split(",") if s.strip()]


def parse_triggering_tests(info_text: str) -> list[tuple[str, str]]:
    """Parse `defects4j info -p <proj> -b <id>` output for the bug-revealing
    ("triggering") tests, returned as (test_class_fqn, test_method) pairs."""
    lines = info_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "Root cause in triggering tests:":
            start = i + 1
            break
    if start is None:
        return []

    tests: list[tuple[str, str]] = []
    for line in lines[start:]:
        s = line.strip()
        if not s:
            continue
        if re.match(r"^-{5,}$", s):
            if tests:
                break
            continue
        m = re.match(r"^-\s+([A-Za-z0-9_.$]+)::([A-Za-z0-9_.$]+)", s)
        if m:
            tests.append((m.group(1), m.group(2)))
            continue
        if s.startswith("-->"):
            continue
        if tests:
            break

    seen = set()
    out = []
    for cls, meth in tests:
        key = (cls, meth)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def find_test_file(work_dir: str, test_src_rel: str, test_class_fqn: str) -> str | None:
    rel_path = Path(test_src_rel) / Path(*test_class_fqn.split("."))
    candidate = Path(work_dir) / rel_path.with_suffix(".java")
    if candidate.exists():
        return str(candidate)

    simple = test_class_fqn.split(".")[-1]
    matches = list(Path(work_dir).rglob(f"{simple}.java"))
    if not matches:
        return None

    test_root = Path(work_dir) / test_src_rel
    for p in matches:
        try:
            p.relative_to(test_root)
            return str(p)
        except ValueError:
            pass
    return str(matches[0])


_METHOD_PATTERN_TEMPLATE = (
    r"(?m)^([ \t]*)"
    r"((?:@[\w.]+(?:\([^)]*\))?\s*(?:\r?\n[ \t]*)?)*"
    r"(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?"
    r"[\w<>\[\],. ]+?\s+{name}\s*\([^)]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{{)"
)


def disable_test_method(file_path: str, method_name: str) -> bool:
    """Comment out a named test method (annotations, signature, and body) in
    a Java source file, in place. Returns True if a method was found and
    disabled, False otherwise.

    This intentionally avoids requiring JavaParser/RemoveMethod.class: it
    locates the method by name via a signature regex, then walks braces from
    the first `{` to find the matching `}`, and comments out that whole span.
    Assumes test method names are unique within the file (true for the
    JUnit-style Defects4J tests this targets).
    """
    text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(_METHOD_PATTERN_TEMPLATE.format(name=re.escape(method_name)))
    m = pattern.search(text)
    if not m:
        return False

    start = m.start()
    brace_start = text.index("{", m.end() - 1)
    depth = 0
    i = brace_start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = i + 1

    method_text = text[start:end]
    commented = "\n".join(
        ("// [usefulness-disabled] " + line) for line in method_text.splitlines()
    )
    new_text = text[:start] + commented + text[end:]
    Path(file_path).write_text(new_text, encoding="utf-8")
    return True


def force_rel_under(work_dir: str, p: str) -> str:
    import os

    p = p.strip()
    if os.path.isabs(p):
        return os.path.relpath(p, start=work_dir)
    return p


_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;")


def derive_package_pattern(main_src_dir: str, depth: int = 2, sample: int = 25) -> str:
    """Scans up to `sample` .java files under main_src_dir for `package ...;`
    declarations and returns a Chicory --ppt-select-pattern covering the
    most common top-`depth`-segment package prefix (e.g. "org.apache.commons.*").
    Falls back to matching everything if no package statements are found.
    """
    from collections import Counter

    counts: Counter[str] = Counter()
    n = 0
    for java_file in Path(main_src_dir).rglob("*.java"):
        if n >= sample:
            break
        try:
            text = java_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines()[:5]:
            m = _PACKAGE_RE.match(line)
            if m:
                parts = m.group(1).split(".")
                prefix = ".".join(parts[:depth])
                counts[prefix] += 1
                n += 1
                break

    if not counts:
        return ".*"
    prefix = counts.most_common(1)[0][0]
    return re.escape(prefix) + r"\..*"


def list_test_classes(bin_tests_dir: str) -> list[str]:
    """Returns fully-qualified test class names by walking compiled .class
    files under bin_tests_dir, skipping inner/anonymous classes."""
    base = Path(bin_tests_dir)
    out = []
    for class_file in base.rglob("*.class"):
        if "$" in class_file.name:
            continue
        rel = class_file.relative_to(base).with_suffix("")
        out.append(".".join(rel.parts))
    return sorted(out)
