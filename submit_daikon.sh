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
# A project with more than SHARD_THRESHOLD bugs is split into 2 shards
# (first half / second half, in the CSV's row order) so it fits within one
# job's walltime; smaller projects get a single shard covering everything.

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

# A project with more than SHARD_THRESHOLD bugs gets 2 shards (first half /
# second half); override with `sbatch --export=ALL,SHARD_THRESHOLD=N ...`.
SHARD_THRESHOLD="${SHARD_THRESHOLD:-50}"

# Build the ordered (project,shard,num_shards) list this array indexes
# into. Recomputed by every task from BUGS_CSV -- cheap (hundreds of rows)
# and keeps this in sync with BUGS_CSV without a separate generated file
# to keep up to date.
SHARD_LIST=$(mktemp)
trap 'rm -f "$SHARD_LIST"' EXIT
for p in $(awk -F, 'NR>1 {print $1}' "$BUGS_CSV" | sort -u); do
  count=$(awk -F, -v p="$p" 'NR>1 && $1==p' "$BUGS_CSV" | wc -l)
  if [[ "$count" -gt "$SHARD_THRESHOLD" ]]; then
    echo "$p,0,2" >> "$SHARD_LIST"
    echo "$p,1,2" >> "$SHARD_LIST"
  else
    echo "$p,0,1" >> "$SHARD_LIST"
  fi
done

ROW=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$SHARD_LIST")
[[ -n "$ROW" ]] || { echo "ERROR: no shard found at index $SLURM_ARRAY_TASK_ID in $SHARD_LIST (built from $BUGS_CSV)"; exit 1; }
PROJECT="${ROW%%,*}"
REST="${ROW#*,}"
SHARD="${REST%%,*}"
NUM_SHARDS="${REST#*,}"

if [[ "$NUM_SHARDS" -gt 1 ]]; then
  echo ">>> Running Daikon usefulness experiment for project=$PROJECT shard=$SHARD/$NUM_SHARDS (sequential batch)"
  python3 "$ROOT/run_daikon_usefulness_batch.py" "$BUGS_CSV" --project "$PROJECT" --shard "$SHARD/$NUM_SHARDS" --skip-existing
else
  echo ">>> Running Daikon usefulness experiment for project=$PROJECT (sequential batch)"
  python3 "$ROOT/run_daikon_usefulness_batch.py" "$BUGS_CSV" --project "$PROJECT" --skip-existing
fi
