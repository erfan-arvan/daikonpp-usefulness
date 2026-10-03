#!/bin/bash -l
#SBATCH --job-name=oca-token-cost
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=02:00:00
#SBATCH --mem=32G

# Oca LLM token-cost DRY RUN (DP_LLM_DRY_RUN=true, daikonplusplus >= 0ea6324)
# on the LATEST FIXED version of each project in PROJECTS (default: the six
# RQ5 projects); one array task per project:
#   sbatch --array=0-5 submit_oca_token_cost.sh
# Every LLM prompt is built exactly as in the fixed-version runs
# (run_oca_fixed.py: few-shot, METHOD_BODY,SCOPE,CLASS_DOC, gpt-4.1-mini,
# maxK 5) and tokenized, but nothing is sent; output tokens come from the
# same cassettes (outputs_usefulness/_cassettes/<PROJECT>), and the run stops
# before injection. Results: outputs_token_cost/<PROJECT>_<N>f/tokencost.log
# (summary) and daikonpp_dry_run_tokens.tsv (per prompt); summarize with
#   python3 summarize_oca_token_cost.py
read -r -a PROJECTS <<< "${PROJECTS:-Cli Codec Collections Gson JxPath Math}"

set -euo pipefail

export MAVEN_HOME="/scratch/mjk76/$USER/apache-maven-3.9.9"
export PATH="$MAVEN_HOME/bin:$PATH"
module load Java/23.0.2

export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

# ---- Dry run: count prompt tokens, read output tokens from the cassettes ----
export DP_LLM_DRY_RUN=true
export DP_LLM_PRICE_INPUT_PER_M="${DP_LLM_PRICE_INPUT_PER_M:-0.40}"
export DP_LLM_PRICE_OUTPUT_PER_M="${DP_LLM_PRICE_OUTPUT_PER_M:-1.60}"

# ---- Same prompt-affecting settings as the real runs ----
export DP_OPENAI_MODEL=gpt-4.1-mini
# (run_usefulness_bug.phase() sets DP_PROMPT_STRATEGY=fewshot and
# DP_CONTEXTS=METHOD_BODY,SCOPE,CLASS_DOC itself, and points DP_LLM_CASSETTES
# at outputs_usefulness/_cassettes/<PROJECT>.)

# ---- Same relaxed settings as submit_oca_fixed_relaxed.sh (post-LLM only;
# they do not change the prompts, kept for a consistent configuration) ----
export DP_QUALITY_FILTER_SELF_COMPARISON=false
export DP_QUALITY_FILTER_UNKNOWN_IDENTIFIER=false
export DP_QUALITY_FILTER_REQUIRE_RESULT_AT_EXIT=false
export DP_QUALITY_FILTER_REQUIRE_IN_SCOPE_NAME=false
export DP_QUALITY_FILTER_MAX_LENGTH=true
export DP_AUTOFILTER_MAX_MODIFY_PASSES=200
export DP_AUTOFILTER_MAX_EXTRA_PASSES=150
export DP_TEST_FILTER=false
export DP_KEEP_WORK=true

export ROOT="$PWD"
export DPP_DIR="$ROOT/daikonplusplus"
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

PROJECT="${PROJECTS[$SLURM_ARRAY_TASK_ID]}"

# Private daikonplusplus clone per project, taken from a checkout that has
# the dry-run feature (default: the prompt study's token-cost clone).
TOKEN_COST_DPP_SRC="${TOKEN_COST_DPP_SRC:-/scratch/mjk76/$USER/daikonplusplus-token-cost}"
export DPP_DIR="${BUILD_ROOT:-$ROOT}/daikonplusplus-tokencost-$PROJECT"
if [[ ! -d "$DPP_DIR" ]]; then
  git clone "$TOKEN_COST_DPP_SRC" "$DPP_DIR"
  chmod +x "$DPP_DIR/gradlew"
fi
if ! git -C "$DPP_DIR" merge-base --is-ancestor 0ea6324 HEAD 2>/dev/null; then
  echo "ERROR: $DPP_DIR ($(git -C "$DPP_DIR" log --oneline -1)) lacks 0ea6324 (dry-run token cost)"
  exit 1
fi
echo ">>> Oca commit: $(git -C "$DPP_DIR" log -1 --format='%h %s')"
export GRADLE_USER_HOME="${BUILD_ROOT:-$ROOT}/gradle-home-tokencost-$PROJECT"
mkdir -p "$GRADLE_USER_HOME"

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

echo ">>> Oca token-cost dry run on the latest fixed version of $PROJECT"
python3 "$ROOT/run_oca_fixed.py" "$PROJECT" --out-root outputs_token_cost --label tokencost
