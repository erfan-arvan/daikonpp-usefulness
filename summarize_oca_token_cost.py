#!/usr/bin/env python3
"""Token / cost table of the Oca LLM dry runs (submit_oca_token_cost.sh) on
the latest fixed version of each project: reads the dry-run summary that
daikonplusplus prints into outputs_token_cost/<PROJECT>_<N>f/tokencost.log.

Usage:
    python3 summarize_oca_token_cost.py [--projects "Cli Codec ..."] [--in-price 0.40] [--out-price 1.60]
"""
import argparse
import glob
import re
from pathlib import Path

PROJECTS = "Cli Codec Collections Gson JxPath Math"
ROOT = Path("outputs_token_cost")


def last_int(pattern, text):
    m = re.findall(pattern, text)
    return int(m[-1]) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default=PROJECTS)
    ap.add_argument("--in-price", type=float, default=0.40, help="USD per 1M input tokens")
    ap.add_argument("--out-price", type=float, default=1.60, help="USD per 1M output tokens")
    args = ap.parse_args()

    print(f'{"run":<18}{"points":>8}{"prompts":>9}{"input tok":>13}{"output tok":>12}{"recorded":>10}'
          f'{"in $":>9}{"out $":>9}{"total $":>10}')
    tot = {"in": 0, "out": 0, "points": 0, "prompts": 0}
    for p in args.projects.replace(",", " ").split():
        runs = sorted(glob.glob(str(ROOT / f"{p}_*f")))
        if not runs:
            print(f"{p:<18}  no run under {ROOT}")
            continue
        d = Path(runs[-1])
        log = d / "tokencost.log"
        text = log.read_text(errors="replace") if log.is_file() else ""
        inp = last_int(r"TOTAL input tokens\s*:\s*(\d+)", text)
        out = last_int(r"TOTAL output tokens\s*:\s*(\d+)", text)
        if inp is None:
            print(f"{d.name:<18}  no dry-run summary in {log}")
            continue
        points = last_int(r"program points in scope\s*:\s*(\d+)", text) or 0
        prompts = last_int(r"prompts built\s*:\s*(\d+)", text) or 0
        recorded = last_int(r"already recorded\s*:\s*(\d+)", text)
        out = out or 0
        cin, cout = inp / 1e6 * args.in_price, out / 1e6 * args.out_price
        tot["in"] += inp; tot["out"] += out; tot["points"] += points; tot["prompts"] += prompts
        rec = f"{recorded}/{prompts}" if recorded is not None else "-"
        print(f"{d.name:<18}{points:>8}{prompts:>9}{inp:>13,}{out:>12,}{rec:>10}"
              f"{cin:>9.4f}{cout:>9.4f}{cin + cout:>10.4f}")
    cin, cout = tot["in"] / 1e6 * args.in_price, tot["out"] / 1e6 * args.out_price
    print(f'{"TOTAL":<18}{tot["points"]:>8}{tot["prompts"]:>9}{tot["in"]:>13,}{tot["out"]:>12,}{"":>10}'
          f"{cin:>9.4f}{cout:>9.4f}{cin + cout:>10.4f}")
    print(f"\nprices: ${args.in_price}/1M input, ${args.out_price}/1M output; 'recorded' = prompts with a "
          "cassette response (output tokens of the rest are estimated at the recorded mean)")


if __name__ == "__main__":
    main()
