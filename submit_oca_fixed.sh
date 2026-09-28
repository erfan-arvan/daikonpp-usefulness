#!/bin/bash -l
#SBATCH --job-name=oca-fixed
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

# RQ3 (expressiveness): Oca on the latest fixed version of each project.
# One array task per project; the index selects from PROJECTS below
# (0-14, e.g. `sbatch --array=0-14 submit_oca_fixed.sh`) -- the same 15
# projects as the RQ5 usefulness runs. Same environment
# as submit.sh (the RQ5 Oca runs).
PROJECTS=(Cli Closure Codec Collections Compress Csv Gson JacksonCore JacksonDatabind JacksonXml Jsoup JxPath Lang Math Time)

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
export DP_AUTOFILTER_MAX_MODIFY_PASSES=150
export DP_AUTOFILTER_MAX_EXTRA_PASSES=150

export ROOT="$PWD"
export DPP_DIR="$ROOT/daikonplusplus"
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

PROJECT="${PROJECTS[$SLURM_ARRAY_TASK_ID]}"

# Private daikonplusplus clone and Gradle home per task (see submit.sh);
# named per project so they never collide with the RQ5 task-N copies.
CANONICAL_DPP_DIR="$DPP_DIR"
export DPP_DIR="${BUILD_ROOT:-$ROOT}/daikonplusplus-fixed-$PROJECT"
if [[ ! -d "$DPP_DIR" ]]; then
  git clone "$CANONICAL_DPP_DIR" "$DPP_DIR"
  chmod +x "$DPP_DIR/gradlew"
fi
export GRADLE_USER_HOME="${BUILD_ROOT:-$ROOT}/gradle-home-fixed-$PROJECT"
mkdir -p "$GRADLE_USER_HOME"

# Same exclusion as the RQ5 JacksonCore runs: this test spins forever under
# Oca's instrumentation (passes in 2s on a plain checkout).
if [[ "$PROJECT" == JacksonCore ]]; then
  export USEFULNESS_EXCLUDE_TESTS="com.fasterxml.jackson.core.filter.AsyncTokenFilterTest::testSkipChildrenFailOnSplit"
fi

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

echo ">>> Oca on the latest fixed version of $PROJECT"
python3 "$ROOT/run_oca_fixed.py" "$PROJECT"
