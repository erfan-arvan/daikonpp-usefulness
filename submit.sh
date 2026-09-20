#!/bin/bash -l
#SBATCH --job-name=usefulness
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=40:00:00
#SBATCH --mem=128G

set -euo pipefail

export MAVEN_HOME="/scratch/mjk76/$USER/apache-maven-3.9.9"
export PATH="$MAVEN_HOME/bin:$PATH"

module load Java/23.0.2

echo "JAVA: $(which java)"
echo "MVN: $(which mvn)"

export ACCOUNT="mjk76"

# Key file: a plain-text file containing just the key (no "export ...", no
# quotes) at /project/mjk76/ea442/OPENAI_API_KEY.txt. Adjust the path below
# if you move it. `xargs` trims any surrounding whitespace/newline.
OPENAI_KEY_FILE="/project/mjk76/ea442/OPENAI_API_KEY.txt"
[[ -f "$OPENAI_KEY_FILE" ]] || { echo "ERROR: OpenAI key file not found: $OPENAI_KEY_FILE"; exit 1; }
export OPENAI_API_KEY="$(xargs < "$OPENAI_KEY_FILE")"
[[ -n "$OPENAI_API_KEY" ]] || { echo "ERROR: $OPENAI_KEY_FILE is empty"; exit 1; }

export ROOT="$PWD"
export DPP_DIR="$ROOT/daikonplusplus"

# If setup.sh built things for you, source its env file (defects4j on PATH,
# etc.) instead of relying on this job's own module loads / PATH:
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

# Give each array task its own private copy of daikonplusplus. run_usefulness_bug.py
# rebuilds the jar with `./gradlew shadowJar` on every invocation (no isolation
# built in), so N array tasks racing against the SAME checkout's build/ directory
# and jar file corrupt each other's builds -- confirmed directly: 11 of 15
# concurrent array tasks failed within 1-2 minutes when they all pointed at one
# shared checkout. A local `git clone` off the canonical checkout (same
# filesystem, no network round trip) per task avoids this entirely.
if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
  CANONICAL_DPP_DIR="$DPP_DIR"
  export DPP_DIR="${BUILD_ROOT:-$ROOT}/daikonplusplus-task-${SLURM_ARRAY_TASK_ID}"
  if [[ ! -d "$DPP_DIR" ]]; then
    echo ">>> Cloning a private daikonplusplus copy for this array task -> $DPP_DIR"
    git clone "$CANONICAL_DPP_DIR" "$DPP_DIR"
  fi
fi

# Defects4J's own docs say v2.x needs Java 8, which may not be the JDK you
# module-loaded above for daikonplusplus. If so, uncomment and point this at
# a Java 8 install; leave unset if one JDK works for both on your cluster.
# export D4J_JAVA_HOME=/path/to/jdk8

# defects4j must be on PATH (its own installer adds a shell profile line;
# source it here if sbatch doesn't inherit your interactive shell config)
command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

# One bug per array task (recommended): edit --array below to match the
# number of rows in bugs.csv (0-indexed, inclusive), e.g. --array=0-99
BUGS_CSV="$ROOT/bugs.csv"
LINE_NO=$(( SLURM_ARRAY_TASK_ID + 2 ))  # +2: skip header, 1-index sed/awk
ROW=$(awk -F, -v n="$LINE_NO" 'NR==n {print $1","$2}' "$BUGS_CSV")
PROJECT="${ROW%%,*}"
BUG_ID="${ROW##*,}"

echo ">>> Running usefulness experiment for $PROJECT-$BUG_ID"
python3 "$ROOT/run_usefulness_bug.py" "$PROJECT" "$BUG_ID"

# --- Alternative: run the whole CSV sequentially in a single job (no array) ---
# python3 "$ROOT/run_usefulness_batch.py" "$ROOT/bugs.csv" --skip-existing
