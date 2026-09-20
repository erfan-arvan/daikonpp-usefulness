# Usefulness (RQ5) experiments — HPC edition, Oca + Daikon

## What this reproduces

This is the "usefulness" experiment from the Oca paper (`daikonpp-paper.pdf`,
section 4.5, RQ5): "Are invariants inferred by Oca more useful than those
inferred by Daikon when used as executable properties to expose buggy
behavior?" This folder has a paired pipeline for **both** tools, run on the
same `bugs.csv`, so the RQ5 numbers are directly comparable.

## Part 1: Oca

The procedure, per Defects4J bug, is two phases:

- **Phase A ("without_test")** — infer invariants on the *buggy* program
  version with the bug-revealing ("triggering") test(s) disabled, so
  inference never sees the failing input. This models a developer who
  hasn't yet written/found a failing test. Invariants that hold here become
  the candidate properties. Real LLM calls happen in this phase, and the
  prompts/responses are recorded into a per-bug cassette directory.
- **Phase B ("with_test")** — re-inject the *same* candidate invariants
  (replayed from the Phase A cassette, so no new LLM calls and the same
  candidates are proposed) against the *original, unmodified* test suite
  (bug-revealing test present). Any invariant marked `FALSIFIED` in this
  phase was violated by the bug-triggering input — i.e., Oca's invariants
  exposed that bug.

## What changed vs. your old `daikonppTests` scripts

Your old scripts (`run_daikonpp_defects4j_with_test.{sh,py}` /
`_without_test.{sh,py}` / `run_sampled_defects4j_batch.py`) implement the
same two-phase idea, but against an older daikonplusplus CLI/config surface.
I checked the current `App.java` / `DpConfig.java` in this repo and found:

1. **`DP_REGISTRY_IN` / `DP_REGISTRY_READONLY` no longer exist.** The old
   `with_test` script tried to make Phase B "replay-only" by pointing at
   Phase A's registry file with those two env vars. `DpConfig` has no such
   keys today, so that mechanism silently did nothing on the current build.
   The tool's *actual* replay mechanism is `DP_LLM_CASSETTES` +
   `DP_DISABLE_REAL_LLM` (see `LlmInvariantGenerator.buildLlmFromEnv`):
   cassette keys are a hash of the LLM system/user prompt, which only
   depends on the **main** source and program point — not the test source —
   so Phase A and Phase B get byte-identical LLM proposals even though the
   test file differs between the two runs. The new scripts here use that
   instead.
2. **The `RemoveMethod` / `RewriteMethodViaLLM` tool referenced by the old
   scripts isn't in the zip** (only its expected `.class` path is
   referenced) — its source was apparently never checked in. The new
   `lib_defects4j.disable_test_method()` reimplements "comment out this one
   test method" as a small regex + brace-matching pass, so there's nothing
   extra to build (no `javaparser-core-*.jar`, no `tools/` dir).
3. **`--external-project`, `--project-root`, `--main-src`, `--test-src`,
   `--runner-script`, and `DP_EXTERNAL_COMPILE_CP` /
   `DP_COMPILE_MAIN_SCRIPT` / `DP_COMPILE_TEST_SCRIPT`** are unchanged and
   still work exactly as your old scripts used them.
4. Turned on `DP_TEST_FILTER=1` in both phases, which enables the
   test-driven side-effect isolation (delta debugging, paper section 3.3.4)
   that your old scripts left off by default.
5. **`defects4j compile`/`defects4j test` silently swallow the real
   ant/maven build+test output on success.** Confirmed by reading
   `Utils::exec_cmd` in the Defects4J source: it captures the build tool's
   output via backticks and only prints it if the command fails, or if
   `D4J_DEBUG` is set. Both scripts now set `D4J_DEBUG=1` globally so that
   output is never swallowed.
6. **The real test-run log was one level further hidden than that.**
   daikonplusplus's external-project mode runs the test suite by handing
   your runner script to `JavaRunner.runExternalScript`, which reads that
   script's merged stdout+stderr and writes it ONLY to
   `<working-copy>/daikonpp-run.log` — never to daikonplusplus's own
   console output. Since the old scripts (and my first pass) set
   `DP_KEEP_WORK=0`, that whole working copy — log included — got deleted
   before anyone could look at it. Fixed by pointing `DP_WORKDIR` at a
   location under this script's own output directory, forcing
   `DP_KEEP_WORK=1` so daikonplusplus doesn't delete it itself, copying
   every `daikonpp-run*.log` out to `<label>_run_logs/` after the run, and
   only then deleting the working copy myself. Verified end-to-end (see
   "Verification performed" below) — the real JUnit/ant output now lands in
   a file you can open.
7. **JDK split.** Defects4J's own install docs (README, "Perl dependencies"
   section) say Defects4J 2.x requires Java 8; daikonplusplus's README says
   17+. A single `module load` may not satisfy both. Both scripts now
   accept `D4J_JAVA_HOME` (pins the JDK for every `defects4j` call) and
   `DPP_JAVA_HOME` (pins the JDK for the daikonplusplus JVM itself),
   independently. Leave both unset to use whatever's on `PATH`, as before,
   if your cluster's one `module load` JDK works for both.

### Oca files

- `lib_defects4j.py` — shared helpers (bug-id parsing, triggering-test
  extraction from `defects4j info`, the test-method disabler, plus a couple
  of helpers shared with the Daikon pipeline below).
- `run_usefulness_bug.py <PROJECT> <BUG_ID>` — runs both phases for one bug.
  Writes to `outputs_usefulness/<PROJECT>_<BUG_ID>/`:
  `without_test.log`, `with_test.log`, `daikonpp_outcomes_without_test.jsonl`,
  `daikonpp_outcomes_with_test.jsonl`, and `cassettes/`.
- `run_usefulness_batch.py bugs.csv [--skip-existing]` — runs
  `run_usefulness_bug.py` over every row of a `project,bug_id` CSV
  sequentially, logging to `outputs_usefulness/batch_logs/`.
- `submit.sh` — SLURM job template for the NJIT HPC, modeled on your
  existing `promptstudy/submit.sh`. Supports either a Slurm job array (one
  bug per task, recommended — Defects4J/Oca runs can take a long time per
  bug) or a single sequential job over the whole CSV.

## Part 2: Daikon

Daikon has no equivalent of Oca's runtime invariant probes — it just infers
likely invariants offline from an execution trace. To get an equivalent
"was this invariant violated by the bug?" signal, this pipeline uses
**trace diffing**, and Chicory's ability to instrument classes without
touching any source file (unlike Oca, no test-method disabling is needed —
we just don't invoke the triggering test in Phase A):

- **Phase A ("without_test")**: a small custom JUnit4 runner
  (`DaikonTestRunner.java`) drives Chicory over every test in the project
  EXCEPT the triggering test method(s), producing `traceA.dtrace.gz`. Daikon
  infers invariants from that trace alone → `invariantsA.txt`. This is the
  candidate property set, exactly mirroring Oca's without-test phase.
- **Phase B ("bug_trace")**: the same runner drives Chicory over ONLY the
  triggering test method(s) (same unmodified checkout), producing
  `bug.dtrace.gz`. Daikon then infers invariants from `[traceA, bug.dtrace.gz]`
  merged → `invariantsAB.txt`.
- **Diff**: any invariant present in `invariantsA` but missing from
  `invariantsAB` was contradicted by a sample from the triggering test —
  Daikon simply won't report an invariant any observed execution violates.
  That's the Daikon-side analog of Oca's `FALSIFIED` verdict. (You confirmed
  this trace-diff design is what you want for RQ5 — the more
  faithful-but-heavier alternative would be mechanically translating each
  Daikon invariant into a Java assertion and checking it directly the way
  Oca does; that's a much bigger build and isn't done here.)

Both phases run against ppt-select-pattern derived automatically from the
project's own top-level package (matching how Oca scans the whole main
source tree, not just the bug-modified classes), and omit
`junit.|org.junit.|sun.|java.|com.sun.proxy` the same way your old
`run_daikon_defects4j.sh` did.

`DaikonTestRunner.java` detects JUnit3 (`junit.framework.TestCase`
subclasses, run via reflection + `TestResult`) vs. JUnit4 (run via
`Request`/`Filter`) per class, so both old (e.g. early `Math` bugs) and
modern Defects4J projects are handled without configuration.

Two real bugs found and fixed by actually building `daikon.jar` from source
(`codespecs/daikon`, version 5.9.1) and running it, not by guessing at
Chicory's/Daikon's CLI:

- **`daikon.Chicory` has no `-o` flag.** It's `--dtrace-file=<name>`. Using
  `-o` fails loudly (usage error) — an earlier draft of this script had this
  wrong.
- **`--dtrace-file` silently produces no file at all when given an absolute
  path** — no error, exit code 0, just nothing written. Confirmed
  repeatedly against the real jar. `run_chicory()` now always passes a bare
  relative filename and moves the result into place itself.

Also fixed: the invariant-diff parser's first version dropped the very
first program point block in a `daikon.PrintInvariants` file, because that
block (unlike every other one) has no `===...===` separator line before it
in real output. Confirmed against real output and fixed by classifying any
line containing `:::` as a ppt header, independent of separator lines.

### Daikon files

- `DaikonTestRunner.java` — the custom JUnit3/JUnit4 runner described
  above. Each CLI arg is `pkg.Class` (run whole class), `pkg.Class::method`
  (run one test), or `pkg.Class::!m1,!m2` (run the class excluding those
  methods). Compiled on the fly against the project's own `cp.test`
  classpath, so no extra JUnit jar needs to be supplied separately.
- `daikon_diff_invariants.py` — parses `daikon.PrintInvariants` text output
  and computes the before/after diff described above.
- `run_daikon_usefulness_bug.py <PROJECT> <BUG_ID>` — runs both Chicory
  phases + both Daikon inference runs for one bug. Writes to
  `outputs_usefulness/<PROJECT>_<BUG_ID>/`: `traceA.dtrace.gz`,
  `traceBug.dtrace.gz`, `invA.inv.gz`, `invAB.inv.gz`, `invariantsA.txt`,
  `invariantsAB.txt`, and `daikon_outcomes.jsonl` (same
  `{"invariant", "verdict"}` shape as Oca's outcomes, so `analyze_usefulness.py`
  handles both uniformly).
- `run_daikon_usefulness_batch.py bugs.csv [--skip-existing]` — CSV batch
  driver, same shape as the Oca one.
- `submit_daikon.sh` — SLURM job template, same array-job pattern as
  `submit.sh`.

## Part 3: `setup.sh` — getting Daikon, daikonplusplus, and Defects4J onto the HPC

None of the three tools this pipeline drives were assumed to already be
installed. `setup.sh [install_root]` builds/clones all three:

```bash
./setup.sh /project/mjk76/ea442/usefullness
```

- **Daikon**: clones `codespecs/daikon` and runs `make daikon.jar`. I built
  this exact recipe from source myself (see "Verification performed" below)
  — it works, but needs `rsync` and `make` on the machine (both near-certain
  to already be present on the HPC; `setup.sh` warns if not found rather
  than silently failing later).
- **daikonplusplus**: clones it (default URL points at
  `erfan-arvan/daikonplusplus` — override with `DAIKONPP_GIT_URL` if that's
  wrong) and runs `./gradlew clean shadowJar`, auto-detecting your `java
  -version` to satisfy its Gradle toolchain (which otherwise demands exactly
  JDK 17 and refuses to auto-download a toolchain on an offline/sandboxed
  build node — I hit this myself building it here, fixed by passing
  `DP_JAVA_VERSION` to match whatever JDK is actually on `PATH`).
- **Defects4J**: clones `rjust/defects4j`, runs `cpanm --installdeps .`,
  then `./init.sh` (downloads project repos + Major + test-gen libs from
  defects4j.org — this step needs outbound network access to
  `defects4j.org`; I could not reach that domain from my own sandbox to
  test this specific step end-to-end, see below).

`setup.sh` writes `usefulness_env.sh` with `ROOT`, `DPP_DIR`, `DAIKON_JAR`,
and `PATH`/`PERL5LIB` for the installed Defects4J — source it (or fold its
`export` lines into `submit.sh`/`submit_daikon.sh`) before running anything
else. It also documents `D4J_JAVA_HOME`/`DPP_JAVA_HOME` there as commented-
out placeholders in case your cluster needs the JDK split described above.

## Analyzing both tools together

```bash
python3 analyze_usefulness.py outputs_usefulness
```

Since both pipelines write into the same `outputs_usefulness/<PROJECT>_<BUG_ID>/`
directories (Oca's files alongside Daikon's), this single script now prints
per-project + aggregate Bug Exposure Rate and Invariant Violation Count for
**both** tools, plus a final side-by-side RQ5 comparison table (paper
section 4.5.5), as long as you've run both pipelines over the same bugs.

## Setting this up in `/project/mjk76/ea442/usefullness`

```bash
cd /project/mjk76/ea442/usefullness
cp <these files> .
git clone <your daikonplusplus repo url> daikonplusplus   # only needed for the Oca side
# defects4j must be on PATH, with its own jar already built
```

Create `bugs.csv` (which bugs to run — used by BOTH pipelines, this
replaces the old `defects4j/sampled_defects4j_bugs.csv`):

```csv
project,bug_id
Lang,1
Lang,3
Math,12
Closure,5
```

`defects4j pids` / `defects4j bids -p <project>` (or the old
`count_defects4j_bugs.sh` in your scripts bundle) will tell you what's
available if you want to resample.

Then, for each tool, either:

```bash
# one job, all bugs, sequential
sbatch submit.sh            # Oca — uses the "alternative" line at the bottom
sbatch submit_daikon.sh     # Daikon — same

# or, recommended: one job per bug via a job array
N=$(($(wc -l < bugs.csv) - 1))   # rows minus header
sbatch --array=0-$((N-1)) submit.sh
sbatch --array=0-$((N-1)) submit_daikon.sh
```

Set `OPENAI_API_KEY` before submitting the Oca job (or source a protected
key file from within `submit.sh`, same as your `promptstudy/submit.sh`
already inlines one — I'd suggest moving it to `~/.openai_key` instead of
committing it to a script). The Daikon job needs no API key, just
`DAIKON_JAR`.

Both pipelines write into the same `outputs_usefulness/` tree, so you can
run them in either order, or in parallel Slurm jobs — they only share
read access to `bugs.csv` and each does its own `defects4j checkout` into
a tool-specific working directory (`..._daikon` suffix for Daikon's), so
they don't collide.

## Verification performed so far

This sandbox has no route to `defects4j.org` (outbound network here is
allowlisted and that domain isn't on it) or to an OpenAI key, so I could not
run either pipeline against a real Defects4J checkout end-to-end. Within
that constraint, I did as much real verification as was reachable, rather
than only unit-testing pieces in isolation:

- **Built a real `daikon.jar` from source** (`codespecs/daikon` @ 5.9.1) and
  used it to find and fix the two Chicory bugs described above (`-o` isn't
  a flag; `--dtrace-file` silently no-ops on an absolute path), and the
  invariant-parser bug (dropped the first ppt block). This is also the
  exact recipe `setup.sh` now automates.
- **Ran a real Chicory→Daikon→PrintInvariants→diff cycle** on a hand-written
  Java program with a deliberately introduced bug (a new argument value a
  small trace never saw), and confirmed the diff correctly marks the
  invariants that value violates as `FALSIFIED` and the one it doesn't as
  `HELD`.
- **Ran `run_daikon_usefulness_bug.py` itself, unmodified, end-to-end**
  against a mock `defects4j` (checkout/compile/test/export/info stubs
  driving a real two-class fixture project with a real triggering test) and
  the real `daikon.jar` above. It produced the exact expected
  `daikon_outcomes.jsonl` (2 `FALSIFIED`, rest `HELD`, matching the
  hand-verified case).
- **Built the real `daikonplusplus.jar`** in this repo (had to work around
  its Gradle toolchain wanting exactly JDK 17 when only 21 was available —
  same fix now in `setup.sh`) and **ran `run_usefulness_bug.py` itself,
  unmodified, end-to-end** against the same style of mock `defects4j`. This
  is what caught the `daikonpp-run.log`-gets-deleted-before-anyone-reads-it
  bug: the first run raised `IllegalStateException: credential is required`
  from the OpenAI client (Phase A does need a real key, as documented) and
  my log-copy step correctly reported no log was found; with a dummy
  `OPENAI_API_KEY` (individual LLM calls then fail and are caught
  per-point, so the run completes with zero proposed invariants — pure
  plumbing test) both phases ran to completion and the real
  `defects4j test` / JUnit output ("JUnit version 4.13.2", "OK (3 tests)")
  was captured in `<label>_run_logs/daikonpp-run.log`, confirming the fix.

What I still could not verify, because it requires things unreachable from
here: an actual `defects4j.org` project checkout (real project source,
real triggering tests, real build quirks per project), a real OpenAI key
(so Phase A's LLM-proposed invariants are actually real, not zero), and
`setup.sh`'s Defects4J `init.sh` step specifically (needs `defects4j.org`).
Run a single-bug smoke test of each
(`run_usefulness_bug.py Lang 1` and `run_daikon_usefulness_bug.py Lang 1`)
once `setup.sh` has run, before kicking off the full `bugs.csv` batch, and
tell me what breaks if anything does.
