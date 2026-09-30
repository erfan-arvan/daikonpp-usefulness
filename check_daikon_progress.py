#!/usr/bin/env python3
"""Daikon progress per project over the latest 5 and latest 10 bugs.

"Latest" includes each project's newest bug: bugs_last10.csv leaves it out
(it is in bugs.csv), so both files are read -- see
check_daikon_catches.bugs_by_project; DROPPED_PROJECTS are left out. A bug counts as done when
outputs_usefulness/<Project>_<Bug>/daikon_outcomes.jsonl exists.
"""
import argparse
from pathlib import Path

from check_daikon_catches import DROPPED_PROJECTS, bugs_by_project

ap = argparse.ArgumentParser()
ap.add_argument('--include-dropped', action='store_true', help=f'also include {", ".join(DROPPED_PROJECTS)}')
exclude = () if ap.parse_args().include_dropped else DROPPED_PROJECTS
bp = bugs_by_project('bugs_last10.csv,bugs.csv', 10, exclude)
print(f'excluded projects: {", ".join(exclude) or "none"}')
print(f'{"project":<16}{"latest5":>9}{"latest10":>10}')
for p in sorted(bp):
    done = [(Path('outputs_usefulness') / f'{p}_{b}' / 'daikon_outcomes.jsonl').exists() for b in bp[p]]
    print(f'{p:<16}{sum(done[:5]):>5}/{min(5, len(done))}{sum(done):>7}/{len(done)}')
