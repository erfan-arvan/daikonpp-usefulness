#!/bin/bash -l
#SBATCH --job-name=usefulness-proj
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=7-00:00:00
#SBATCH --mem=128G

# One array task per PROJECT (not per bug): each task runs ALL of that
# project's bugs SEQUENTIALLY in a single process via run_usefulness_batch.py,
# so different bug IDs of the SAME project never race on the same shared LLM
# cassette dir (outputs_usefulness/_cassettes/<project>/) or the same
# project's build/checkout state. Different PROJECTS still run in true
# parallel, one per array task -- e.g. --array=0-14 for 15 projects, instead
# of --array=0-775 for every individual bug.
#
# --time=7-00:00:00 is a GUESS -- a project with 100+ bugs run sequentially
# can plausibly take days, not hours. Check your QOS's actual max walltime
# (`sacctmgr show qos standard format=MaxWall` or similar) and raise/lower
# this to match; a job that hits the time limit mid-project is killed, not
# paused, though --skip-existing below means resubmitting the same project
# afterward picks up where it left off rather than redoing finished bugs.

set -euo pipefail

export MAVEN_HOME="/scratch/mjk76/$USER/apache-maven-3.9.9"
export PATH="$MAVEN_HOME/bin:$PATH"

module load Java/23.0.2

echo "JAVA: $(which java)"
echo "MVN: $(which mvn)"

export ACCOUNT="mjk76"

# See submit.sh for the full diagnosis of why this is necessary.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

OPENAI_KEY_FILE="/project/mjk76/ea442/OPENAI_API_KEY.txt"
[[ -f "$OPENAI_KEY_FILE" ]] || { echo "ERROR: OpenAI key file not found: $OPENAI_KEY_FILE"; exit 1; }
export OPENAI_API_KEY="$(xargs < "$OPENAI_KEY_FILE")"
[[ -n "$OPENAI_API_KEY" ]] || { echo "ERROR: $OPENAI_KEY_FILE is empty"; exit 1; }

export ROOT="$PWD"
export DPP_DIR="$ROOT/daikonplusplus"

[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

# Same per-task build isolation as submit.sh (see there for the full
# diagnosis of the Gradle journal-lock and exec-bit issues this avoids),
# just keyed by array task id as before -- each task here still means one
# PROJECT now, not one bug, but the isolation need (private checkout +
# private Gradle home) is identical either way.
if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
  CANONICAL_DPP_DIR="$DPP_DIR"
  export DPP_DIR="${BUILD_ROOT:-$ROOT}/daikonplusplus-proj-${SLURM_ARRAY_TASK_ID}"
  if [[ ! -d "$DPP_DIR" ]]; then
    echo ">>> Cloning a private daikonplusplus copy for this array task -> $DPP_DIR"
    git clone "$CANONICAL_DPP_DIR" "$DPP_DIR"
    chmod +x "$DPP_DIR/gradlew"
  fi

  export GRADLE_USER_HOME="${BUILD_ROOT:-$ROOT}/gradle-home-proj-${SLURM_ARRAY_TASK_ID}"
  mkdir -p "$GRADLE_USER_HOME"
fi

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

# BUGS_CSV holds every project's bugs together (e.g. bugs_all.csv, generated
# by gen_bugs_all_csv.py). The Nth unique project name (sorted, 0-indexed)
# in that CSV is this array task's project -- run_usefulness_batch.py's own
# --project flag then filters BUGS_CSV down to just that project's rows.
# Override with `sbatch --export=ALL,BUGS_CSV=/path/to/other.csv ...`.
BUGS_CSV="${BUGS_CSV:-$ROOT/bugs_all.csv}"
PROJECT=$(awk -F, 'NR>1 {print $1}' "$BUGS_CSV" | sort -u | sed -n "$((SLURM_ARRAY_TASK_ID + 1))p")
[[ -n "$PROJECT" ]] || { echo "ERROR: no project found at index $SLURM_ARRAY_TASK_ID in $BUGS_CSV"; exit 1; }

echo ">>> Running usefulness experiment for project=$PROJECT (sequential batch)"
python3 "$ROOT/run_usefulness_batch.py" "$BUGS_CSV" --project "$PROJECT" --skip-existing
