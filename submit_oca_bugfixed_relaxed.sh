#!/bin/bash -l
#SBATCH --job-name=oca-bugfixed-relaxed
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=16:00:00
#SBATCH --mem=128G

# Relaxed Oca (same settings as submit_oca_relaxed.sh, same Oca commit) on
# the FIXED version of each bug in BUGS (default: Collections 24-28),
# replaying the RQ5 cassettes; one array task per bug, e.g.
# `sbatch --array=0-4 submit_oca_bugfixed_relaxed.sh`. A BUGS entry is a bug
# id of PROJECT or <Project>_<id>, e.g.
# `sbatch --array=0-1 --export=ALL,BUGS="Codec_15 Math_105" submit_oca_bugfixed_relaxed.sh`. The relaxed catches of
# each bug are then checked against it with check_oca_catches_on_fixed.py.
# Results: outputs_oca_bugfixed_relaxed/<PROJECT>_<N>f/ with label "bugfixed"
# (checkout defects4j/<PROJECT>-<N>f_bugfixed), so nothing from the
# latest-fixed runs (label "fixed") is touched. Working copies are kept
# (DP_KEEP_WORK=true).
PROJECT="${PROJECT:-Collections}"
read -r -a BUGS <<< "${BUGS:-24 25 26 27 28}"

set -euo pipefail

export MAVEN_HOME="/scratch/mjk76/$USER/apache-maven-3.9.9"
export PATH="$MAVEN_HOME/bin:$PATH"
module load Java/23.0.2

export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

OPENAI_KEY_FILE="/project/mjk76/ea442/OPENAI_API_KEY.txt"
[[ -f "$OPENAI_KEY_FILE" ]] || { echo "ERROR: OpenAI key file not found: $OPENAI_KEY_FILE"; exit 1; }
export OPENAI_API_KEY="$(xargs < "$OPENAI_KEY_FILE")"
[[ -n "$OPENAI_API_KEY" ]] || { echo "ERROR: $OPENAI_KEY_FILE is empty"; exit 1; }

export DP_OPENAI_MODEL=gpt-4.1-mini
export DP_AUTOFILTER_MAX_EXTRA_PASSES=150

# ---- Relaxed-Oca settings (as in submit_oca_relaxed.sh) ----
export DP_QUALITY_FILTER_SELF_COMPARISON=false
export DP_QUALITY_FILTER_UNKNOWN_IDENTIFIER=false
export DP_QUALITY_FILTER_REQUIRE_RESULT_AT_EXIT=false
export DP_QUALITY_FILTER_REQUIRE_IN_SCOPE_NAME=false
# true by default: Collections' latest fixed version hung at injection
# without it. Pass DP_QUALITY_FILTER_MAX_LENGTH=false to match the buggy runs.
export DP_QUALITY_FILTER_MAX_LENGTH="${DP_QUALITY_FILTER_MAX_LENGTH:-true}"
echo ">>> DP_QUALITY_FILTER_MAX_LENGTH=$DP_QUALITY_FILTER_MAX_LENGTH"
export DP_AUTOFILTER_MAX_MODIFY_PASSES=200
export DP_TEST_FILTER=false

# Keep Oca's working copies (instrumented project + defects4j checkout).
export DP_KEEP_WORK=true

export ROOT="$PWD"
export DPP_DIR="$ROOT/daikonplusplus"
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

BUG="${BUGS[$SLURM_ARRAY_TASK_ID]}"
if [[ "$BUG" == *_* ]]; then
  PROJECT="${BUG%_*}"
  BUG="${BUG##*_}"
fi

# Private daikonplusplus clone and Gradle home per bug, named so they never
# collide with the other task copies. A new clone is taken from the
# canonical checkout ($DPP_DIR after usefulness_env.sh).
CANONICAL_DPP_DIR="$DPP_DIR"
export DPP_DIR="${BUILD_ROOT:-$ROOT}/daikonplusplus-bugfixed-relaxed-$PROJECT-$BUG"
if [[ ! -d "$DPP_DIR" ]]; then
  git clone "$CANONICAL_DPP_DIR" "$DPP_DIR"
  chmod +x "$DPP_DIR/gradlew"
fi
# Same Oca as the relaxed reruns: 26ac9a3 (per-method guard injection).
if ! git -C "$DPP_DIR" merge-base --is-ancestor 26ac9a3 HEAD 2>/dev/null; then
  echo "ERROR: $DPP_DIR ($(git -C "$DPP_DIR" log --oneline -1)) lacks 26ac9a3"
  exit 1
fi
echo ">>> Oca commit: $(git -C "$DPP_DIR" log -1 --format='%h %s')"
export GRADLE_USER_HOME="${BUILD_ROOT:-$ROOT}/gradle-home-bugfixed-relaxed-$PROJECT-$BUG"
mkdir -p "$GRADLE_USER_HOME"

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

echo ">>> Relaxed Oca on the fixed version of $PROJECT-$BUG"
python3 "$ROOT/run_oca_fixed.py" "$PROJECT" --bug "$BUG" --out-root outputs_oca_bugfixed_relaxed --label bugfixed
