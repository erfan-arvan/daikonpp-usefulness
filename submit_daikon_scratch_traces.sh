#!/bin/bash -l
#SBATCH --job-name=usefulness-daikon-sctr
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

# Like submit_daikon_scratch.sh (checkout + in-progress trace on /scratch),
# and additionally keeps each bug's finished traces and .inv files under
# DAIKON_TRACE_ROOT/<PROJECT>_<BUG>/ on /scratch. Only the small final
# results (daikon_outcomes.jsonl, invariantsA.txt, invariantsFull.txt) are
# written to ROOT/outputs_usefulness on /project. Traces a bug already has in
# ROOT/outputs_usefulness from an earlier run are reused where they are.
#
# Submit from the same directory, with the same BUGS_CSV and --array
# semantics as submit_daikon.sh. Never give this job and another Daikon job
# the same rows at the same time.
export DAIKON_WORK_ROOT="${DAIKON_WORK_ROOT:-/scratch/mjk76/$USER/usefullness/defects4j}"
export DAIKON_TRACE_ROOT="${DAIKON_TRACE_ROOT:-/scratch/mjk76/$USER/usefullness/traces}"
mkdir -p "$DAIKON_WORK_ROOT" "$DAIKON_TRACE_ROOT"
exec bash "$PWD/submit_daikon.sh"
