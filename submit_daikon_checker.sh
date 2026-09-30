#!/bin/bash -l
#SBATCH --job-name=daikon-checker
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
#SBATCH --array=0-5

# run_daikon_checker_bug.py on Cli_31, Cli_34, Cli_39, each in normal and
# --null mode (one array task each):
#   0 Cli_31 normal   1 Cli_31 null
#   2 Cli_34 normal   3 Cli_34 null
#   4 Cli_39 normal   5 Cli_39 null
# Results go to $CHECKER_OUT_ROOT/{normal,null}/<P>_<B>/ (default
# $ROOT/outputs_daikon_checker); outputs_usefulness/ is never written.
# Same memory/time/JDK as submit_daikon.sh. A task's exit code is the
# script's: nonzero on any failure (SLURM shows the task FAILED).
#
#   sbatch submit_daikon_checker.sh
#   sbatch --export=ALL,DAIKON_JAR=$PWD/tools/patched/daikon.jar submit_daikon_checker.sh

set -euo pipefail

module load Java/23.0.2
echo "JAVA: $(which java)"

export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

export ROOT="$PWD"

# As submit_daikon.sh: keep a DAIKON_JAR given at submission over the one
# usefulness_env.sh sets.
_submitted_daikon_jar="${DAIKON_JAR:-}"
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"
[[ -n "$_submitted_daikon_jar" ]] && export DAIKON_JAR="$_submitted_daikon_jar"
export DAIKON_JAR="${DAIKON_JAR:-/project/mjk76/ea442/usefulness/tools/daikon.jar}"
[[ -f "$DAIKON_JAR" ]] || { echo "ERROR: DAIKON_JAR not found: $DAIKON_JAR"; exit 1; }
echo "DAIKON_JAR: $DAIKON_JAR"

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

CHECKER_OUT_ROOT="${CHECKER_OUT_ROOT:-$ROOT/outputs_daikon_checker}"
[[ "$(realpath -m "$CHECKER_OUT_ROOT")" != "$(realpath -m "$ROOT/outputs_usefulness")" ]] \
  || { echo "ERROR: CHECKER_OUT_ROOT must not be outputs_usefulness"; exit 1; }

BUGS=(31 31 34 34 39 39)
MODES=(normal null normal null normal null)
i="${SLURM_ARRAY_TASK_ID:?run as an array job}"
(( i >= 0 && i < ${#BUGS[@]} )) || { echo "ERROR: no task $i"; exit 1; }
PROJECT=Cli
BUG_ID="${BUGS[$i]}"
MODE="${MODES[$i]}"
NULL_FLAG=()
[[ "$MODE" == null ]] && NULL_FLAG=(--null)

echo ">>> daikon checker: project=$PROJECT bug=$BUG_ID mode=$MODE out=$CHECKER_OUT_ROOT"
set +e
timeout --signal=TERM --kill-after=5m $((71 * 3600))s \
  python3 "$ROOT/run_daikon_checker_bug.py" "$PROJECT" "$BUG_ID" "${NULL_FLAG[@]}" --out-root "$CHECKER_OUT_ROOT"
rc=$?
set -e
if [[ $rc -eq 124 || $rc -eq 137 ]]; then
  echo ">>> TIMEOUT: project=$PROJECT bug=$BUG_ID mode=$MODE (rc=$rc)"
elif [[ $rc -ne 0 ]]; then
  echo ">>> FAILED: project=$PROJECT bug=$BUG_ID mode=$MODE (rc=$rc)"
else
  echo ">>> OK: project=$PROJECT bug=$BUG_ID mode=$MODE"
fi
exit $rc
