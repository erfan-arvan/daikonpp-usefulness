#!/usr/bin/env python3
"""Move finished Daikon traces and .inv files off /project while tasks run.

Daikon tasks started without DAIKON_TRACE_ROOT (submit_daikon.sh,
submit_daikon_scratch.sh) keep traceA/traceFull (tens of GB each) and
invA/invFull in outputs_usefulness/<PROJECT>_<BUG>/ until the bug finishes.
This moves each such file to <trace-root>/<PROJECT>_<BUG>/ and leaves a
symlink at the original path, so the running task keeps reading it through
the link; nothing is recomputed.

Safety:
  - Only final names are moved (traceA.dtrace.gz, traceFull.dtrace.gz,
    invA.inv.gz, invFull.inv.gz). The pipeline writes those names only by
    atomic rename once the file is complete; in-progress ".partial-*" and
    "*.tmp.inv.gz" files are never touched.
  - The copy goes to a temp name on scratch and is renamed into place only
    after its size matches. The original is then atomically replaced by the
    symlink, but only if its inode, size and mtime are unchanged since the
    copy began; otherwise it is skipped this pass. A process that already
    has the original open keeps reading it until it closes it.
  - Scratch copies of bugs whose daikon_outcomes.jsonl exists and that no
    longer have a link in outputs_usefulness are deleted (the task removed
    its links on success).

Usage:
    python3 move_traces_to_scratch.py [--root DIR] [--trace-root DIR] [--dry-run]
    python3 move_traces_to_scratch.py --loop 30     # repeat every 30 minutes
"""
from __future__ import annotations

import argparse
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

NAMES = ("traceA.dtrace.gz", "traceFull.dtrace.gz", "invA.inv.gz", "invFull.inv.gz")


def log(msg: str):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def move_one(src: Path, dest: Path, dry_run: bool) -> int:
    st = src.stat()
    gb = st.st_size / 2**30
    if dry_run:
        log(f"WOULD MOVE {gb:7.1f} GB  {src} -> {dest}")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(".moving-" + dest.name)
    tmp.unlink(missing_ok=True)
    log(f"copying {gb:7.1f} GB  {src} -> {dest}")
    shutil.copyfile(src, tmp)
    if tmp.stat().st_size != st.st_size:
        tmp.unlink(missing_ok=True)
        log(f"SKIP {src}: copy size mismatch")
        return 0
    now = src.stat() if src.exists() and not src.is_symlink() else None
    if now is None or (now.st_ino, now.st_size, now.st_mtime_ns) != (st.st_ino, st.st_size, st.st_mtime_ns):
        tmp.unlink(missing_ok=True)
        log(f"SKIP {src}: changed or removed during copy")
        return 0
    os.replace(tmp, dest)
    link_tmp = src.with_name(".link-" + src.name)
    link_tmp.unlink(missing_ok=True)
    link_tmp.symlink_to(dest)
    os.replace(link_tmp, src)
    log(f"moved {gb:7.1f} GB  {src} -> symlink")
    return st.st_size


def cleanup_done(outputs: Path, trace_root: Path, dry_run: bool) -> int:
    freed = 0
    if not trace_root.is_dir():
        return 0
    for d in sorted(p for p in trace_root.iterdir() if p.is_dir()):
        out_dir = outputs / d.name
        if not (out_dir / "daikon_outcomes.jsonl").exists():
            continue
        if any((out_dir / n).is_symlink() for n in NAMES):
            continue
        for f in d.iterdir():
            if f.name in NAMES or f.name.startswith(".moving-"):
                freed += f.stat().st_size
                if dry_run:
                    log(f"WOULD DELETE (bug done) {f}")
                else:
                    f.unlink()
        if not dry_run:
            try:
                d.rmdir()
            except OSError:
                pass
    return freed


def one_pass(root: Path, trace_root: Path, dry_run: bool):
    outputs = root / "outputs_usefulness"
    moved = 0
    for out_dir in sorted(p for p in outputs.iterdir() if p.is_dir() and not p.name.startswith(("_", "batch_logs"))):
        if (out_dir / "daikon_outcomes.jsonl").exists():
            continue  # finished bug: nothing running reads these
        for name in NAMES:
            src = out_dir / name
            if src.is_symlink() or not src.is_file():
                continue
            try:
                moved += move_one(src, trace_root / out_dir.name / name, dry_run)
            except OSError as e:
                log(f"SKIP {src}: {e}")
    freed = cleanup_done(outputs, trace_root, dry_run)
    log(f"pass done: moved {moved / 2**30:.1f} GB off {root}, deleted {freed / 2**30:.1f} GB of finished bugs' copies")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("ROOT", os.getcwd()))
    ap.add_argument("--trace-root", default=f"/scratch/mjk76/{os.environ.get('USER', '')}/usefullness/traces")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", type=float, default=0, help="repeat every N minutes (0 = one pass)")
    args = ap.parse_args()
    # Absolute, so the symlinks left in outputs_usefulness resolve from anywhere.
    root, trace_root = Path(args.root).resolve(), Path(args.trace_root).resolve()
    while True:
        one_pass(root, trace_root, args.dry_run)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
