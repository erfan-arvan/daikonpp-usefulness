#!/bin/bash -l
#SBATCH --job-name=rq5-table
#SBATCH --output=%x.%j.out
#SBATCH --error=%x.%j.err
#SBATCH --partition=general
#SBATCH --qos=standard
#SBATCH --account=mjk76
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=01:00:00
#SBATCH --mem=32G

# Runs bug_results_table.py (per-bug RQ5 table + latest-5/latest-10
# summaries) as a job instead of on the login node. Submit from the
# usefullness directory; the table is in rq5-table.<jobid>.out and
# rq5_bug_results.csv.
set -euo pipefail
python3 "$PWD/bug_results_table.py" "$@"
