"""Shared helpers for the Oca (daikonplusplus) usefulness (RQ5) experiments.

Ported from the old daikonppTests scripts, updated for the current
daikonplusplus CLI/config surface (no more DP_REGISTRY_IN / DP_REGISTRY_READONLY,
which no longer exist) and rewritten to disable the bug-revealing test method(s)
with a small regex/brace-matching pass instead of the missing RemoveMethod /
RewriteMethodViaLLM JavaParser tool (whose sources were never included in the
scripts bundle).
"""
from __future__ import annotations

import collections
import re
import subprocess
import sys
from pathlib import Path

# Tracks whatever child run() currently has blocked on, so a caller's own
# SIGTERM handler (e.g. run_daikon_usefulness_bug.py's) can kill it
# explicitly before cleaning up its cwd. Without this, a signal that
# interrupts subprocess.run()'s wait only unwinds the PYTHON stack (via
# sys.exit() in that handler) -- the child process itself (e.g. a `java
# daikon.Chicory` run mid-write of a multi-GB temp trace file into that same
# cwd) is never told to stop, so it's orphaned: it keeps running, and keeps
# writing into a directory whose entry the handler may already have deleted,
# silently wasting disk space until SLURM eventually reaps the whole cgroup.
_current_subprocess: subprocess.Popen | None = None


def kill_current_subprocess():
    """Called from a SIGTERM handler, before removing this subprocess's cwd,
    to make sure nothing is still running (and thus still writing) there."""
    global _current_subprocess
    if _current_subprocess is not None and _current_subprocess.poll() is None:
        print(f"[INFO] killing in-flight subprocess (pid={_current_subprocess.pid})", flush=True)
        _current_subprocess.kill()
        _current_subprocess.wait()


def run(cmd, cwd=None, env=None, check=True, keep_tail=0):
    """Runs cmd with its output streamed to our stdout as usual. With
    keep_tail=N, the last N lines of its combined stdout+stderr are also kept
    and attached to a raised CalledProcessError as `.output`, so a caller can
    tell WHY it failed (e.g. a corrupt trace vs. an OutOfMemoryError)."""
    global _current_subprocess
    print("+", " ".join(str(c) for c in cmd), flush=True)
    tail: collections.deque[str] = collections.deque(maxlen=keep_tail or 1)
    if keep_tail:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
    else:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env)
    _current_subprocess = proc
    try:
        if keep_tail:
            for line in proc.stdout:
                sys.stdout.write(line)
                tail.append(line)
            sys.stdout.flush()
        returncode = proc.wait()
    finally:
        _current_subprocess = None
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, output="".join(tail) if keep_tail else None)
    return subprocess.CompletedProcess(cmd, returncode)


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


def _matching_brace(text: str, open_idx: int) -> int:
    """Index of the `}` closing the `{` at open_idx, ignoring braces inside
    string/char literals and comments. Counting raw characters (the earlier
    version) broke on Closure's CodePrinterTest, whose test bodies are full
    of JavaScript strings like "function f() {": the walk overshot the
    method, commented out the rest of the file, and javac failed with
    "reached end of file while parsing" (Closure-173)."""
    depth = 0
    i, n = open_idx, len(text)
    while i < n:
        c = text[i]
        if text.startswith('"""', i):  # text block
            j = text.find('"""', i + 3)
            i = n if j < 0 else j + 3
            continue
        if c == '"' or c == "'":
            i += 1
            while i < n and text[i] != c and text[i] != "\n":
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"no matching '}}' for '{{' at offset {open_idx}")


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
    end = _matching_brace(text, brace_start) + 1

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


_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)


def derive_package_pattern(main_src_dir: str) -> str:
    """Returns a Chicory --ppt-select-pattern matching exactly the packages
    declared under main_src_dir (and their subpackages), e.g.
    "^(?:org\\.apache\\.commons\\.lang3)\\.".

    Every .java file is read in full. An earlier version only looked at each
    file's first 5 lines, but Apache-style sources open with a ~16-line
    license header, so it never saw the `package` line and silently fell back
    to ".*" -- confirmed on 29 of 46 runs (every Commons project, Gson, Time,
    Closure). Chicory then traced third-party libraries and DaikonTestRunner
    itself (default package, not covered by the omit pattern), inflating
    traces to tens of GB and polluting the Phase A/B diff with runner state.

    It also used only the first two package segments ("com.google"), which
    for Closure matches Guava/protobuf classes too. Instead, this keeps the
    minimal set of declared packages that covers all others (e.g.
    com.google.javascript.jscomp, com.google.javascript.rhino,
    com.google.debugging.sourcemap). The pattern is anchored with ^ because
    Chicory applies it with Matcher.find() to the class, method and ppt name.

    Raises instead of falling back to ".*" if no package is found.
    """
    packages: set[str] = set()
    for java_file in Path(main_src_dir).rglob("*.java"):
        try:
            text = java_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _PACKAGE_RE.search(text)
        if m:
            packages.add(m.group(1))

    if not packages:
        raise RuntimeError(f"no `package` declarations found under {main_src_dir}")

    roots: list[str] = []
    for pkg in sorted(packages):
        if not any(pkg == r or pkg.startswith(r + ".") for r in roots):
            roots.append(pkg)
    return "^(?:" + "|".join(re.escape(r) for r in roots) + r")\."


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
