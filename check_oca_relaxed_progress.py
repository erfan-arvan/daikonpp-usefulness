#!/usr/bin/env python3
"""Progress of the relaxed-Oca runs (submit_oca_relaxed.sh) over the latest
5 / latest 10 bugs of the projects they cover.

done        = outputs_usefulness_relaxed/<P>_<B>/RUN_COMPLETE exists
in progress = the folder exists without RUN_COMPLETE (running, or failed)
"""
from pathlib import Path

from check_daikon_catches import bugs_by_project

PROJECTS = ['Cli', 'Codec', 'Collections', 'Csv', 'Gson', 'JacksonDatabind', 'JacksonXml', 'JxPath', 'Math']
OUT = Path('outputs_usefulness_relaxed')


def main():
    bp = bugs_by_project('bugs_last10.csv,bugs.csv', 10)
    print(f'{"project":<16}{"latest5":>9}{"latest10":>10}  in progress / not started (latest 10)')
    tot_done = tot_all = 0
    for p in PROJECTS:
        ids = bp.get(p, [])
        done = [(OUT / f'{p}_{b}' / 'RUN_COMPLETE').exists() for b in ids]
        started = [b for b in ids if (OUT / f'{p}_{b}').exists() and not (OUT / f'{p}_{b}' / 'RUN_COMPLETE').exists()]
        missing = [b for b in ids if not (OUT / f'{p}_{b}').exists()]
        tot_done += sum(done)
        tot_all += len(ids)
        print(f'{p:<16}{sum(done[:5]):>5}/{min(5, len(ids))}{sum(done):>7}/{len(ids)}  '
              f'{",".join(started) or "-"} / {",".join(missing) or "-"}')
    print(f'{"TOTAL":<16}{"":>9}{tot_done:>7}/{tot_all}')


if __name__ == '__main__':
    main()
