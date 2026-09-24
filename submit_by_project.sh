#!/bin/bash -l
#SBATCH --job-name=usefulness-proj
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=72:00:00
#SBATCH --mem=128G

# One array task per PROJECT SHARD (not per bug): each task runs its slice
# of that project's bugs SEQUENTIALLY in a single process via
# run_usefulness_batch.py, so different bug IDs within the SAME shard never
# race on the same shared LLM cassette dir (outputs_usefulness/_cassettes/
# <project>/) or the same project's build/checkout state. Different
# PROJECTS -- and different SHARDS of a large project -- still run in true
# parallel, one per array task.
#
# A project with more than SHARD_THRESHOLD bugs is split into 2 shards
# (first half / second half, in bugs_all.csv order) so it fits within one
# job's walltime; smaller projects get a single shard covering everything.
# Splitting a project's bugs across shards that run concurrently DOES mean
# those shards write to the SAME shared cassette dir at the same time --
# see the --shard docstring in run_usefulness_batch.py for why that's safe
# for the common case (each cassette entry is its own file, keyed by a
# prompt hash) but not risk-free for the rare case of two shards resolving
# the exact same prompt at once.
#
# --time=72:00:00 -- verify this against your QOS's actual max walltime
# (`sacctmgr show qos standard format=MaxWall`) if a shard still doesn't
# finish in time; a job that hits the time limit is killed, not paused,
# though --skip-existing below means resubmitting the same shard afterward
# picks up where it left off rather than redoing finished bugs.

set -euo pipefail

export MAVEN_HOME="/scratch/mjk76/$USER/apache-maven-3.9.9"
export PATH="$MAVEN_HOME/bin:$PATH"

module load Java/23.0.2

echo "JAVA: $(which java)"
echo "MVN: $(which mvn)"

export ACCOUNT="mjk76"

# See submit.sh for the full diagnosis of why this is necessary.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

OPENAI_KEY_FILE="/project/mjk76/ea442/OPENAI_API_KEY.txt"
[[ -f "$OPENAI_KEY_FILE" ]] || { echo "ERROR: OpenAI key file not found: $OPENAI_KEY_FILE"; exit 1; }
export OPENAI_API_KEY="$(xargs < "$OPENAI_KEY_FILE")"
[[ -n "$OPENAI_API_KEY" ]] || { echo "ERROR: $OPENAI_KEY_FILE is empty"; exit 1; }

# DpConfig defaults DP_OPENAI_MODEL to "gpt-4.1" if unset -- far more
# expensive than needed for this scale of experiment. NOTE: the LLM
# cassette cache key is a hash of (system, user) prompt content only (see
# daikonplusplus's Cassette.key()) -- it does NOT depend on which model
# generated the cached response. So this only affects NEW cache-miss
# prompts going forward; any prompt already recorded (under gpt-4.1, from
# before this was added) keeps being replayed as-is on a cache hit,
# regardless of this setting.
export DP_OPENAI_MODEL=gpt-4.1-mini

export ROOT="$PWD"
export DPP_DIR="$ROOT/daikonplusplus"

[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

# Same per-task build isolation as submit.sh (see there for the full
# diagnosis of the Gradle journal-lock and exec-bit issues this avoids),
# just keyed by array task id as before -- each task here still means one
# PROJECT now, not one bug, but the isolation need (private checkout +
# private Gradle home) is identical either way.
if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
  CANONICAL_DPP_DIR="$DPP_DIR"
  export DPP_DIR="${BUILD_ROOT:-$ROOT}/daikonplusplus-proj-${SLURM_ARRAY_TASK_ID}"
  if [[ ! -d "$DPP_DIR" ]]; then
    echo ">>> Cloning a private daikonplusplus copy for this array task -> $DPP_DIR"
    git clone "$CANONICAL_DPP_DIR" "$DPP_DIR"
    chmod +x "$DPP_DIR/gradlew"
  fi

  export GRADLE_USER_HOME="${BUILD_ROOT:-$ROOT}/gradle-home-proj-${SLURM_ARRAY_TASK_ID}"
  mkdir -p "$GRADLE_USER_HOME"
fi

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

# BUGS_CSV holds every project's bugs together (e.g. bugs_all.csv, generated
# by gen_bugs_all_csv.py). Override with
# `sbatch --export=ALL,BUGS_CSV=/path/to/other.csv ...`.
BUGS_CSV="${BUGS_CSV:-$ROOT/bugs_all.csv}"

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

# Closure's LLM-generated invariants include a large volume of JsonML/Node API
# hallucinations (LLM confuses Closure Compiler's JsonML class with its Node
# class) spread across 150+ distinct source files. The autofilter's default
# budget (10 modify passes + 20 restore-only passes) isn't nearly enough to
# work through that many broken files -- confirmed empirically: a real run
# was still discovering brand-new files needing restoration at pass 30/30.
# Widen both budgets for Closure only; every other project keeps daikonplusplus's
# defaults (DpConfig.autofilterMaxModifyPasses/autofilterMaxExtraPasses).
if [[ "$PROJECT" == "Closure" ]]; then
  export DP_AUTOFILTER_MAX_MODIFY_PASSES=150
  export DP_AUTOFILTER_MAX_EXTRA_PASSES=150
fi

if [[ "$NUM_SHARDS" -gt 1 ]]; then
  echo ">>> Running usefulness experiment for project=$PROJECT shard=$SHARD/$NUM_SHARDS (sequential batch)"
  python3 "$ROOT/run_usefulness_batch.py" "$BUGS_CSV" --project "$PROJECT" --shard "$SHARD/$NUM_SHARDS" --skip-existing
else
  echo ">>> Running usefulness experiment for project=$PROJECT (sequential batch)"
  python3 "$ROOT/run_usefulness_batch.py" "$BUGS_CSV" --project "$PROJECT" --skip-existing
fi
