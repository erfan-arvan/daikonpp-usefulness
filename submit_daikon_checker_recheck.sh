#!/bin/bash -l
#SBATCH --job-name=daikon-recheck
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=04:00:00
#SBATCH --mem=200G
#SBATCH --array=0-5

# recheck_daikon_checker.py (checker step ONLY, on saved traces / invA /
# invariantsA.txt; no Chicory, no inference, no defects4j) for the same six
# runs as submit_daikon_checker.sh:
#   0 Cli_31 normal   1 Cli_31 null
#   2 Cli_34 normal   3 Cli_34 null
#   4 Cli_39 normal   5 Cli_39 null
# Use the SAME DAIKON_JAR that inferred invA. A task's exit code is the
# script's: nonzero on any failure.
#
#   sbatch submit_daikon_checker_recheck.sh
#   sbatch --export=ALL,DAIKON_JAR=$PWD/tools/daikon.jar submit_daikon_checker_recheck.sh

set -euo pipefail

module load Java/23.0.2
echo "JAVA: $(which java)"

export ROOT="$PWD"
_submitted_daikon_jar="${DAIKON_JAR:-}"
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"
[[ -n "$_submitted_daikon_jar" ]] && export DAIKON_JAR="$_submitted_daikon_jar"
export DAIKON_JAR="${DAIKON_JAR:-/project/mjk76/ea442/usefulness/tools/daikon.jar}"
[[ -f "$DAIKON_JAR" ]] || { echo "ERROR: DAIKON_JAR not found: $DAIKON_JAR"; exit 1; }
echo "DAIKON_JAR: $DAIKON_JAR"

CHECKER_OUT_ROOT="${CHECKER_OUT_ROOT:-$ROOT/outputs_daikon_checker}"

BUGS=(31 31 34 34 39 39)
MODES=(normal null normal null normal null)
i="${SLURM_ARRAY_TASK_ID:?run as an array job}"
(( i >= 0 && i < ${#BUGS[@]} )) || { echo "ERROR: no task $i"; exit 1; }
PROJECT=Cli
BUG_ID="${BUGS[$i]}"
MODE="${MODES[$i]}"
NULL_FLAG=()
[[ "$MODE" == null ]] && NULL_FLAG=(--null)

echo ">>> daikon recheck: project=$PROJECT bug=$BUG_ID mode=$MODE out=$CHECKER_OUT_ROOT"
set +e
python3 "$ROOT/recheck_daikon_checker.py" "$PROJECT" "$BUG_ID" "${NULL_FLAG[@]}" --out-root "$CHECKER_OUT_ROOT"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  echo ">>> FAILED: project=$PROJECT bug=$BUG_ID mode=$MODE (rc=$rc)"
else
  echo ">>> OK: project=$PROJECT bug=$BUG_ID mode=$MODE"
fi
exit $rc
