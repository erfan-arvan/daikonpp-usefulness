#!/bin/bash -l
#SBATCH --job-name=daikon-strict-batch
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=72:00:00
#SBATCH --mem=200G

# run_daikon_strict_batch_bug.py: one array task per bug in BUGS (required).
# Runs checker -> null trace -> fixed validation -> strict attribution,
# resuming completed stages, with --cleanup-traces always on: after all four
# stages succeed the bug's traces are deleted and its results moved to
# $RESULTS_ROOT (default $ROOT/outputs_daikon_strict_batch). Failed or
# incomplete bugs keep their traces under $WORK_ROOT. Exit code 75 = deferred
# (not enough free disk); resubmit the same command later -- completed bugs
# are skipped. Cap concurrency with %N in --array.
#
#   sbatch --array=0-7%4 --time=24:00:00 --export=ALL,BUGS="Math_105 Math_104 ...",DAIKON_JAR=$PWD/tools/patched/daikon.jar submit_daikon_strict_batch.sh

set -euo pipefail

module load Java/23.0.2
echo "JAVA: $(which java)"

export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

export ROOT="$PWD"
_submitted_daikon_jar="${DAIKON_JAR:-}"
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"
[[ -n "$_submitted_daikon_jar" ]] && export DAIKON_JAR="$_submitted_daikon_jar"
export DAIKON_JAR="${DAIKON_JAR:-$ROOT/tools/patched/daikon.jar}"
[[ -f "$DAIKON_JAR" ]] || { echo "ERROR: DAIKON_JAR not found: $DAIKON_JAR"; exit 1; }
echo "DAIKON_JAR: $DAIKON_JAR"
command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

WORK_ROOT="${WORK_ROOT:-/scratch/mjk76/$USER/usefullness/daikon_strict_batch}"
RESULTS_ROOT="${RESULTS_ROOT:-$ROOT/outputs_daikon_strict_batch}"
export DAIKON_WORK_ROOT="${DAIKON_WORK_ROOT:-/scratch/mjk76/$USER/usefullness/defects4j}"
mkdir -p "$WORK_ROOT" "$RESULTS_ROOT" "$DAIKON_WORK_ROOT"
echo "WORK_ROOT: $WORK_ROOT  RESULTS_ROOT: $RESULTS_ROOT  DAIKON_WORK_ROOT: $DAIKON_WORK_ROOT"

read -r -a BUG_LIST <<< "${BUGS:?set BUGS=\"P_B P_B ...\"}"
i="${SLURM_ARRAY_TASK_ID:?run as an array job}"
(( i < ${#BUG_LIST[@]} )) || { echo "ERROR: task $i but only ${#BUG_LIST[@]} bug(s) in BUGS"; exit 1; }
PROJECT="${BUG_LIST[$i]%_*}"
BUG_ID="${BUG_LIST[$i]##*_}"

echo ">>> strict batch: project=$PROJECT bug=$BUG_ID"
df -h "$WORK_ROOT" "$DAIKON_WORK_ROOT" "$RESULTS_ROOT" || true
set +e
# DAIKON_KEEP_WORK=true (sbatch --export): keep the defects4j checkouts and
# all traces (no --cleanup-traces); results then stay under $WORK_ROOT.
CLEANUP=--cleanup-traces
if [[ "${DAIKON_KEEP_WORK:-}" =~ ^(1|true|yes)$ ]]; then CLEANUP=; echo ">>> DAIKON_KEEP_WORK set: keeping checkouts and traces"; fi
python3 "$ROOT/run_daikon_strict_batch_bug.py" "$PROJECT" "$BUG_ID" --work-root "$WORK_ROOT" \
  --results-root "$RESULTS_ROOT" $CLEANUP
rc=$?
set -e
if [[ $rc -eq 75 ]]; then
  echo ">>> DEFERRED: project=$PROJECT bug=$BUG_ID (insufficient disk; resubmit later)"
elif [[ $rc -ne 0 ]]; then
  echo ">>> FAILED: project=$PROJECT bug=$BUG_ID (rc=$rc; traces kept under $WORK_ROOT)"
else
  echo ">>> OK: project=$PROJECT bug=$BUG_ID"
fi
exit $rc
