"""
Summarise logs/latency.jsonl written by bot.py.

    uv run python latency_report.py            # all turns
    uv run python latency_report.py --last 10  # most recent 10 turns

Prints user->bot latency (median, p90, max) and where the time went,
grouped by contribution label (VAD wait, STT, LLM first token, TTS, ...).
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="logs/latency.jsonl")
    ap.add_argument("--last", type=int, default=0, help="only the most recent N turns")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"No {path} yet. Talk to the bot first.")
        return

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.last:
        rows = rows[-args.last :]
    if not rows:
        print("No turns logged.")
        return

    totals = [r["breakdown"]["total_secs"] for r in rows]
    parts: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        for c in r["breakdown"].get("contributions", []):
            parts[f'{c["label"]}  [{c["owner"]}]'].append(c["duration_secs"])

    print(f"Turns: {len(rows)}   (models: {rows[-1]['stt']} | {rows[-1]['llm']} | {rows[-1]['tts']})")
    print(
        f"User->bot latency   median {statistics.median(totals):.2f}s   "
        f"p90 {pct(totals, 90):.2f}s   max {max(totals):.2f}s"
    )
    if parts:
        print("\nWhere the time goes (median per turn it appeared in):")
        for label, vals in sorted(parts.items(), key=lambda kv: -statistics.median(kv[1])):
            print(f"  {statistics.median(vals):6.2f}s  {label}  (n={len(vals)})")


if __name__ == "__main__":
    main()
