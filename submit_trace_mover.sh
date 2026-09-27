#!/bin/bash -l
#SBATCH --job-name=usefulness-trace-mover
#SBATCH --output=%x.%j.out
#SBATCH --error=%x.%j.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=72:00:00
#SBATCH --mem=2G

# Runs move_traces_to_scratch.py every 30 minutes for up to 72h, moving
# finished Daikon traces/.inv files from outputs_usefulness on /project to
# /scratch and leaving symlinks behind. Submit from the usefulness directory:
#   sbatch submit_trace_mover.sh
set -euo pipefail
export ROOT="$PWD"
python3 "$ROOT/move_traces_to_scratch.py" --root "$ROOT" --loop "${MOVER_INTERVAL_MIN:-30}"
