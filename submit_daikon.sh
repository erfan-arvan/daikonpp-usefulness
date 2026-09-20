#!/bin/bash -l
#SBATCH --job-name=usefulness-daikon
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=2-00:00:00
#SBATCH --mem=64G

set -euo pipefail

module load Java/23.0.2
echo "JAVA: $(which java)"

# `git clone`/`init` probes this /mmfs1-backed filesystem for executable-bit
# reliability and writes its own `filemode = true` into EACH new repo's
# LOCAL .git/config, overriding a global `core.fileMode false` -- see
# submit.sh for the full diagnosis (confirmed directly on a fresh
# commons-collections clone). defects4j's own internal `git clone && git
# checkout` hits this too, so set it here the same way.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.fileMode
export GIT_CONFIG_VALUE_0=false

export ROOT="$PWD"

# If setup.sh built things for you, source its env file (DAIKON_JAR,
# defects4j on PATH, etc.) instead of hardcoding the path below:
[[ -f "$ROOT/usefulness_env.sh" ]] && source "$ROOT/usefulness_env.sh"

# REQUIRED if not already set by usefulness_env.sh above: point this at your
# built daikon.jar.
export DAIKON_JAR="${DAIKON_JAR:-/project/mjk76/ea442/tools/daikon.jar}"
[[ -f "$DAIKON_JAR" ]] || { echo "ERROR: DAIKON_JAR not found: $DAIKON_JAR"; exit 1; }

# Defects4J's own docs say v2.x needs Java 8, which may not be the JDK you
# module-loaded above for Daikon/Chicory. If so, uncomment and point this at
# a Java 8 install; leave unset if one JDK works for both on your cluster.
# export D4J_JAVA_HOME=/path/to/jdk8

command -v defects4j >/dev/null || { echo "ERROR: defects4j not on PATH"; exit 1; }

BUGS_CSV="$ROOT/bugs.csv"   # SAME csv used for the Oca run, for a paired comparison
LINE_NO=$(( SLURM_ARRAY_TASK_ID + 2 ))  # +2: skip header, 1-index
ROW=$(awk -F, -v n="$LINE_NO" 'NR==n {print $1","$2}' "$BUGS_CSV")
PROJECT="${ROW%%,*}"
BUG_ID="${ROW##*,}"

echo ">>> Running Daikon usefulness experiment for $PROJECT-$BUG_ID"
python3 "$ROOT/run_daikon_usefulness_bug.py" "$PROJECT" "$BUG_ID"

# --- Alternative: run the whole CSV sequentially in a single job (no array) ---
# python3 "$ROOT/run_daikon_usefulness_batch.py" "$ROOT/bugs.csv" --skip-existing
