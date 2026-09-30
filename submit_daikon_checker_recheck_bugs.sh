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


# recheck_daikon_checker.py (checker step ONLY, on saved traces / invA /
# invariantsA.txt; no Chicory, no inference, no defects4j) for any bugs:
# BUGS="<P>_<B> ...", two array tasks per bug: 2k = normal, 2k+1 = null.
# Use the SAME DAIKON_JAR (and DAIKON_EXTRA_CONFIG) that inferred invA.
#
#   sbatch --array=0-3 --export=ALL,BUGS="Math_106 Math_98",DAIKON_JAR=$PWD/tools/patched/daikon.jar \
#     submit_daikon_checker_recheck_bugs.sh

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

read -r -a BUG_LIST <<< "${BUGS:?set BUGS=\"<P>_<B> ...\"}"
i="${SLURM_ARRAY_TASK_ID:?run as an array job}"
k=$((i / 2))
(( k < ${#BUG_LIST[@]} )) || { echo "ERROR: task $i but only ${#BUG_LIST[@]} bug(s) in BUGS"; exit 1; }
PROJECT="${BUG_LIST[$k]%_*}"
BUG_ID="${BUG_LIST[$k]##*_}"
MODE=normal
NULL_FLAG=()
(( i % 2 == 1 )) && { MODE=null; NULL_FLAG=(--null); }

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
