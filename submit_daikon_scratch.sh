#!/bin/bash -l
#SBATCH --job-name=usefulness-daikon-scratch
#SBATCH --output=%x.%A_%a.out
#SBATCH --error=%x.%A_%a.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=72:00:00
#SBATCH --mem=96G

# Same Daikon pipeline as submit_daikon.sh (submit from the same directory,
# with the same BUGS_CSV and --array semantics), except each bug's defects4j
# checkout and Chicory's in-progress trace live under DAIKON_WORK_ROOT on
# /scratch instead of ROOT/defects4j on /project. Finished traces and results
# still go to ROOT/outputs_usefulness, so check_daikon_progress.py and
# --skip-existing see both jobs' work.
#
# Never give this job and a submit_daikon.sh job the same rows at the same
# time: both would write the same outputs_usefulness/<PROJECT>_<BUG>/.
export DAIKON_WORK_ROOT="${DAIKON_WORK_ROOT:-/scratch/mjk76/$USER/usefullness/defects4j}"
mkdir -p "$DAIKON_WORK_ROOT"
exec bash "$PWD/submit_daikon.sh"
