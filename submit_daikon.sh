#!/bin/bash -l
#SBATCH --job-name=usefulness-daikon-proj
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=72:00:00
#SBATCH --mem=64G

# One array task per BUG -- array index N runs the N-th row of BUGS_CSV
# directly (1 row = 1 task, no per-project grouping). This matters for
# concurrency-throttled submissions (`sbatch --array=0-144%K`): SLURM always
# fills in array indices in ascending order, so whichever K bugs occupy the
# FIRST K rows of BUGS_CSV are exactly what runs first. Point BUGS_CSV at a
# CSV built by gen_bugs_interleaved.py (rank 1 -- the highest bug_id, "last"
# bug -- of every project first, then every project's rank 2, and so on) to
# get "the last bug of every project runs first, then the second-last of
# every project" instead of accidentally front-loading one entire project
# (e.g. all 10 of Closure's bugs, among the largest/slowest in the set) into
# that first wave just because a plain per-project-grouped CSV happened to
# list that project first alphabetically.
#
# A single bug's own runtime can be extremely variable (confirmed:
# Math/Collections produced 40,000-97,000 candidate invariants per bug vs.
# Cli's much smaller numbers) or even exceed this cluster's 72h QOS ceiling
# outright -- one task per bug means a slow bug only ever costs itself, never
# starves any other bug's own walltime budget.

set -euo pipefail

module load Java/23.0.2
echo "JAVA: $(which java)"

# See submit.sh (Oca side) for the full diagnosis of why this is necessary
# on this cluster's /mmfs1-backed filesystem.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

export ROOT="$PWD"

# If setup.sh built things for you, source its env file (DAIKON_JAR,
# defects4j on PATH, etc.) instead of hardcoding the path below:
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

# REQUIRED if not already set by usefulness_env.sh above: point this at your
# built daikon.jar.
export DAIKON_JAR="${DAIKON_JAR:-/project/mjk76/ea442/usefulness/tools/daikon.jar}"
[[ -f "$DAIKON_JAR" ]] || { echo "ERROR: DAIKON_JAR not found: $DAIKON_JAR"; exit 1; }

# Defects4J's own docs say v2.x needs Java 8, which may not be the JDK you
# module-loaded above for Daikon/Chicory. If so, uncomment and point this at
# a Java 8 install; leave unset if one JDK works for both on your cluster.
# export D4J_JAVA_HOME=/path/to/jdk8

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

# SAME csv as the Oca run (bugs_last10.csv, not the original full bugs.csv)
# so both tools are compared on an IDENTICAL bug set. Point this at a
# gen_bugs_interleaved.py output to control which bugs run first under a
# concurrency-throttled array (see the note above). Override with
# `sbatch --export=ALL,BUGS_CSV=/path/to/other.csv ...`.
BUGS_CSV="${BUGS_CSV:-$ROOT/bugs_last10.csv}"

# Row (SLURM_ARRAY_TASK_ID + 2) of BUGS_CSV: +1 for 1-indexed sed, +1 more to
# skip the header row. Strip any trailing \r unconditionally -- a
# CRLF-terminated CSV (e.g. from Python's csv module, whose own default
# lineterminator is "\r\n" per the CSV spec) leaves one on BUG_ID that this
# parsing wouldn't otherwise catch, and --bug then fails to match anything
# (confirmed: this is exactly what happened on the first real submission,
# every single task failing with "no row for project=X bug='Y\r'").
ROW=$(sed -n "$((SLURM_ARRAY_TASK_ID + 2))p" "$BUGS_CSV" | tr -d '\r')
[[ -n "$ROW" ]] || { echo "ERROR: no row at index $SLURM_ARRAY_TASK_ID in $BUGS_CSV"; exit 1; }
PROJECT="${ROW%%,*}"
BUG_ID="${ROW#*,}"

# Internal soft timeout, kept under this cluster's hard 72h QOS ceiling
# (`sacctmgr show qos standard format=MaxWall` = 3-00:00:00) so a stuck or
# genuinely-too-large bug gets killed cleanly by us (clear log line, no
# ambiguity about mid-write state) instead of an abrupt SLURM SIGKILL at
# the exact walltime limit. --kill-after gives it a few minutes to react to
# SIGTERM before a hard SIGKILL if it's wedged. A bug that hits this is
# simply left incomplete (no daikon_outcomes.jsonl) -- the same "not ready
# yet" state the progress-check scripts already handle, self-healing on a
# future rerun since run_daikon_usefulness_bug.py clears any half-finished
# work_dir at the start of its own next attempt.
set +e
echo ">>> Running Daikon usefulness experiment for project=$PROJECT bug=$BUG_ID"
timeout --signal=TERM --kill-after=5m 71h \
  python3 "$ROOT/run_daikon_usefulness_batch.py" "$BUGS_CSV" --project "$PROJECT" --bug "$BUG_ID" --skip-existing
rc=$?
set -e
if [[ $rc -eq 124 || $rc -eq 137 ]]; then
  echo ">>> TIMEOUT: project=$PROJECT bug=$BUG_ID killed after 71h (rc=$rc; 137 means it needed the --kill-after SIGKILL)"
fi
exit $rc
