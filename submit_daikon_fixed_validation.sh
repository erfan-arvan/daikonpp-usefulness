#!/bin/bash -l
#SBATCH --job-name=daikon-fixval
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=01:00:00
#SBATCH --mem=200G

# validate_daikon_fixed.py: one array task per bug in BUGS (default: the
# three Cli bugs). Reads the saved checker results (normal + null) read-only
# and writes $VALIDATION_OUT_ROOT/<P>_<B>/ (default
# $ROOT/outputs_daikon_fixed_validation). Use the same DAIKON_JAR that
# inferred invA. A task's exit code is the script's.
#
#   sbatch --array=0-2 --export=ALL,DAIKON_JAR=$PWD/tools/daikon.jar submit_daikon_fixed_validation.sh
#   sbatch --array=0-0 --export=ALL,BUGS="Math_106",DAIKON_JAR=$PWD/tools/patched/daikon.jar submit_daikon_fixed_validation.sh

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
export DAIKON_JAR="${DAIKON_JAR:-/project/mjk76/ea442/usefulness/tools/daikon.jar}"
[[ -f "$DAIKON_JAR" ]] || { echo "ERROR: DAIKON_JAR not found: $DAIKON_JAR"; exit 1; }
echo "DAIKON_JAR: $DAIKON_JAR"
command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

CHECKER_OUT_ROOT="${CHECKER_OUT_ROOT:-$ROOT/outputs_daikon_checker}"
VALIDATION_OUT_ROOT="${VALIDATION_OUT_ROOT:-$ROOT/outputs_daikon_fixed_validation}"
read -r -a BUG_LIST <<< "${BUGS:-Cli_31 Cli_34 Cli_39}"
i="${SLURM_ARRAY_TASK_ID:?run as an array job}"
(( i < ${#BUG_LIST[@]} )) || { echo "ERROR: task $i but only ${#BUG_LIST[@]} bug(s) in BUGS"; exit 1; }
PROJECT="${BUG_LIST[$i]%_*}"
BUG_ID="${BUG_LIST[$i]##*_}"

echo ">>> fixed-version validation: project=$PROJECT bug=$BUG_ID"
set +e
python3 "$ROOT/validate_daikon_fixed.py" "$PROJECT" "$BUG_ID" \
  --checker-root "$CHECKER_OUT_ROOT" --out-root "$VALIDATION_OUT_ROOT"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  echo ">>> FAILED: project=$PROJECT bug=$BUG_ID (rc=$rc)"
else
  echo ">>> OK: project=$PROJECT bug=$BUG_ID"
fi
exit $rc
