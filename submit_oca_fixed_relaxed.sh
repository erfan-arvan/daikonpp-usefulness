#!/bin/bash -l
#SBATCH --job-name=oca-fixed-relaxed
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

# Relaxed Oca (same settings as submit_oca_relaxed.sh) on the LATEST FIXED
# version of each project in PROJECTS (default: the six RQ5 table projects);
# one array task per project, e.g. `sbatch --array=0-5 submit_oca_fixed_relaxed.sh`.
# Results: outputs_expressiveness_relaxed/<PROJECT>_<N>f/ -- the original
# fixed-version results in outputs_expressiveness/ are never touched. Oca's
# working copies are kept (DP_KEEP_WORK=true): <out>/dp_workdir_fixed and
# defects4j/<PROJECT>-<N>f_fixed.
read -r -a PROJECTS <<< "${PROJECTS:-Cli Codec Collections Gson JxPath Math}"

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
export DP_QUALITY_FILTER_MAX_LENGTH=false
export DP_AUTOFILTER_MAX_MODIFY_PASSES=200
export DP_TEST_FILTER=false

# Keep Oca's working copies (instrumented project + defects4j checkout).
export DP_KEEP_WORK=true

export ROOT="$PWD"
export DPP_DIR="$ROOT/daikonplusplus"
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

PROJECT="${PROJECTS[$SLURM_ARRAY_TASK_ID]}"

# Private daikonplusplus clone and Gradle home per project, named so they
# never collide with the RQ5 / relaxed / fixed task copies. A new clone is
# taken from the canonical checkout ($DPP_DIR after usefulness_env.sh).
CANONICAL_DPP_DIR="$DPP_DIR"
export DPP_DIR="${BUILD_ROOT:-$ROOT}/daikonplusplus-fixed-relaxed-$PROJECT"
if [[ ! -d "$DPP_DIR" ]]; then
  git clone "$CANONICAL_DPP_DIR" "$DPP_DIR"
  chmod +x "$DPP_DIR/gradlew"
fi
if ! git -C "$DPP_DIR" merge-base --is-ancestor 2d7ca57 HEAD 2>/dev/null; then
  echo "ERROR: $DPP_DIR ($(git -C "$DPP_DIR" log --oneline -1)) lacks 2d7ca57 (relaxed flags)"
  exit 1
fi
echo ">>> Oca commit: $(git -C "$DPP_DIR" log -1 --format='%h %s')"
export GRADLE_USER_HOME="${BUILD_ROOT:-$ROOT}/gradle-home-fixed-relaxed-$PROJECT"
mkdir -p "$GRADLE_USER_HOME"

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

echo ">>> Relaxed Oca on the latest fixed version of $PROJECT"
python3 "$ROOT/run_oca_fixed.py" "$PROJECT" --out-root outputs_expressiveness_relaxed
