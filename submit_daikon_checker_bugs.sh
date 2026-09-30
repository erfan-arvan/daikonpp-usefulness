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

# Like submit_daikon_checker.sh, for any bugs: BUGS="<P>_<B> <P>_<B> ...".
# Each bug gets two array tasks: 2k = normal, 2k+1 = --null. Submit with
# --array=0-(2*N-1), e.g. two bugs:
#
#   sbatch --array=0-3 --time=04:00:00 \
#     --export=ALL,BUGS="Codec_18 Gson_18",DAIKON_JAR=$PWD/tools/daikon.jar submit_daikon_checker_bugs.sh
#
# Results: $CHECKER_OUT_ROOT/{normal,null}/<P>_<B>/ (default
# $ROOT/outputs_daikon_checker). A task's exit code is the script's.

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
[[ "$(realpath -m "$CHECKER_OUT_ROOT")" != "$(realpath -m "$ROOT/outputs_usefulness")" ]] \
  || { echo "ERROR: CHECKER_OUT_ROOT must not be outputs_usefulness"; exit 1; }

read -r -a BUG_LIST <<< "${BUGS:?set BUGS=\"<P>_<B> ...\"}"
i="${SLURM_ARRAY_TASK_ID:?run as an array job}"
k=$((i / 2))
(( k < ${#BUG_LIST[@]} )) || { echo "ERROR: task $i but only ${#BUG_LIST[@]} bug(s) in BUGS"; exit 1; }
PROJECT="${BUG_LIST[$k]%_*}"
BUG_ID="${BUG_LIST[$k]##*_}"
MODE=normal
NULL_FLAG=()
(( i % 2 == 1 )) && { MODE=null; NULL_FLAG=(--null); }

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
