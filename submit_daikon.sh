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

# One array task per PROJECT SHARD (not per bug) -- mirrors
# submit_by_project.sh's approach on the Oca side. Unlike Oca, Daikon needs
# no per-task private clone/build isolation: each bug does its own
# independent `defects4j checkout` into its own bug-specific work dir
# (defects4j/<PROJECT>-<bug>b_daikon/), so different bugs of the same
# project -- or different shards of a large project -- never share any
# build state to race on. Per-project batching here is purely to keep the
# job count and log layout consistent with the Oca side, not for
# correctness.
#
# Each project is split into ceil(bug_count / BUGS_PER_SHARD) shards (in
# the CSV's row order), so job count and per-shard wall time scale with how
# many bugs a project actually has. Default BUGS_PER_SHARD=1 (one bug per
# array task) -- see the full rationale below where it's set: a single
# bug's own runtime can be extremely variable (confirmed: Math/Collections
# produced 40,000-97,000 candidate invariants per bug vs. Cli's much
# smaller numbers) or even exceed this cluster's 72h QOS ceiling outright,
# and batching multiple bugs per shard would let one such bug starve every
# other bug queued behind it in that same shard.

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
# so both tools are compared on an IDENTICAL bug set. Override with
# `sbatch --export=ALL,BUGS_CSV=/path/to/other.csv ...`.
BUGS_CSV="${BUGS_CSV:-$ROOT/bugs_last10.csv}"

# Target number of bugs per shard. Default 1 (one bug per array task): a
# single bug's Chicory/Daikon run can itself be extremely slow on a large
# enough project (observed: even 72h wasn't enough for a netty-scale
# codebase) or simply exceed this cluster's hard 72h QOS ceiling
# (`sacctmgr show qos standard format=MaxWall`) outright. With more than
# one bug per shard, a single stuck/huge bug would starve every OTHER bug
# queued behind it in that same shard out of its own 72h entirely -- at
# BUGS_PER_SHARD=1, a slow bug only ever costs itself. Override with
# `sbatch --export=ALL,BUGS_PER_SHARD=N ...` if you want coarser batching.
BUGS_PER_SHARD="${BUGS_PER_SHARD:-1}"

# Build the ordered (project,shard,num_shards) list this array indexes
# into. Recomputed by every task from BUGS_CSV -- cheap (hundreds of rows)
# and keeps this in sync with BUGS_CSV without a separate generated file
# to keep up to date.
SHARD_LIST=$(mktemp)
trap 'rm -f "$SHARD_LIST"' EXIT
for p in $(awk -F, 'NR>1 {print $1}' "$BUGS_CSV" | sort -u); do
  count=$(awk -F, -v p="$p" 'NR>1 && $1==p' "$BUGS_CSV" | wc -l)
  # ceil(count / BUGS_PER_SHARD), minimum 1
  num_shards=$(( (count + BUGS_PER_SHARD - 1) / BUGS_PER_SHARD ))
  [[ "$num_shards" -lt 1 ]] && num_shards=1
  for ((i=0; i<num_shards; i++)); do
    echo "$p,$i,$num_shards" >> "$SHARD_LIST"
  done
done

ROW=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$SHARD_LIST")
[[ -n "$ROW" ]] || { echo "ERROR: no shard found at index $SLURM_ARRAY_TASK_ID in $SHARD_LIST (built from $BUGS_CSV)"; exit 1; }
PROJECT="${ROW%%,*}"
REST="${ROW#*,}"
SHARD="${REST%%,*}"
NUM_SHARDS="${REST#*,}"

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
if [[ "$NUM_SHARDS" -gt 1 ]]; then
  echo ">>> Running Daikon usefulness experiment for project=$PROJECT shard=$SHARD/$NUM_SHARDS (sequential batch)"
  timeout --signal=TERM --kill-after=5m 71h \
    python3 "$ROOT/run_daikon_usefulness_batch.py" "$BUGS_CSV" --project "$PROJECT" --shard "$SHARD/$NUM_SHARDS" --skip-existing
else
  echo ">>> Running Daikon usefulness experiment for project=$PROJECT (sequential batch)"
  timeout --signal=TERM --kill-after=5m 71h \
    python3 "$ROOT/run_daikon_usefulness_batch.py" "$BUGS_CSV" --project "$PROJECT" --skip-existing
fi
rc=$?
set -e
if [[ $rc -eq 124 || $rc -eq 137 ]]; then
  echo ">>> TIMEOUT: project=$PROJECT shard=$SHARD/$NUM_SHARDS killed after 71h (rc=$rc; 137 means it needed the --kill-after SIGKILL)"
fi
exit $rc
