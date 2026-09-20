#!/bin/bash -l
#SBATCH --job-name=usefulness-setup
#SBATCH --output=%x.%j.out
#SBATCH --error=%x.%j.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --time=02:00:00
#SBATCH --mem=16G

set -euo pipefail

module load Java/23.0.2
echo "JAVA: $(which java)"

cd "$SLURM_SUBMIT_DIR"
bash setup.sh "$SLURM_SUBMIT_DIR"
