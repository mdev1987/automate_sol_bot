"""Offline trade-log analysis: does the score predict profit, and is the
exit leaving money on the table?

Usage:
    uv run python scripts/analyze_trades.py [path/to/trade_log.csv]

Reads the append-only ``trade_log.csv`` and prints, for every closed trade:

* overall stats (trades / win rate / net PnL / avg ROI)
* a score-bucket table  (count, avg ROI, median ROI, win rate)
* per-exit-reason stats (count, avg/median final ROI, peak unrealized ROI,
  and avg "money left on the table" = peak - final)

Rows written before the ``max_roi_pct`` column existed are skipped for the
peak/exit-quality numbers (the column shows as ``-``).
"""

from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "trade_log.csv"

SCORE_BANDS = (
    (0, 30, "0-30"),
    (30, 50, "30-50"),
    (50, 60, "50-60"),
    (60, 70, "60-70"),
    (70, 999, "70+"),
)


def band_for(score: float) -> str:
    for lo, hi, name in SCORE_BANDS:
        if lo <= score < hi:
            return name
    return "70+"


def fnum(row: dict, key: str) -> float:
    val = row.get(key, "")
    try:
        return float(val)
    except (TypeError, ValueError):
        return float("nan")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    if not rows:
        print(f"no trades in {path}")
        return

    has_peak = "max_roi_pct" in rows[0]
    n = len(rows)

    wins = sum(1 for r in rows if fnum(r, "pnl_usdc") > 0)
    losses = sum(1 for r in rows if fnum(r, "pnl_usdc") < 0)
    pnl = sum(fnum(r, "pnl_usdc") for r in rows)
    avg_roi = statistics.mean(fnum(r, "roi_pct") for r in rows)
    med_roi = statistics.median(fnum(r, "roi_pct") for r in rows)

    buckets: dict = defaultdict(lambda: {"rois": [], "wins": 0})
    exits: dict = defaultdict(lambda: {"rois": [], "peaks": [], "holds": []})
    for r in rows:
        b = band_for(fnum(r, "score"))
        buckets[b]["rois"].append(fnum(r, "roi_pct"))
        buckets[b]["wins"] += 1 if fnum(r, "pnl_usdc") > 0 else 0
        ex = exits[r.get("reason", "?")]
        ex["rois"].append(fnum(r, "roi_pct"))
        ex["holds"].append(fnum(r, "hold_s"))
        if has_peak:
            peak = fnum(r, "max_roi_pct")
            if peak == peak:  # not NaN
                ex["peaks"].append(peak)

    print(f"=== {path} — {n} trades ===")
    print(f"trades={n} wins={wins} losses={losses} "
          f"win_rate={wins / n * 100:.1f}% net_pnl=${pnl:.2f} "
          f"avg_roi={avg_roi:+.1f}% median_roi={med_roi:+.1f}%")
    print()

    print("--- score buckets (does score predict profit?) ---")
    print(f"{'band':<8}{'n':>5}{'avg ROI':>10}{'med ROI':>10}{'win%':>7}")
    for _lo, _hi, name in SCORE_BANDS:
        d = buckets.get(name)
        if not d:
            continue
        rois = d["rois"]
        wr = d["wins"] / len(rois) * 100
        print(f"{name:<8}{len(rois):>5}{statistics.mean(rois):>+9.1f}%"
              f"{statistics.median(rois):>+9.1f}%{wr:>6.0f}%")
    print()

    print("--- per-exit-reason ---")
    print(f"{'reason':<8}{'n':>5}{'avg ROI':>10}{'med ROI':>10}"
          f"{'peak ROI':>10}{'left':>10}  avg hold")
    for reason in sorted(exits):
        d = exits[reason]
        rois = d["rois"]
        avg = statistics.mean(rois)
        med = statistics.median(rois)
        hold = statistics.mean(d["holds"])
        if d["peaks"]:
            peak = statistics.mean(d["peaks"])
            left = peak - avg
            print(f"{reason:<8}{len(rois):>5}{avg:>+9.1f}%{med:>+9.1f}%"
                  f"{peak:>+9.0f}%{left:>+9.0f}%  {hold:.0f}s")
        else:
            print(f"{reason:<8}{len(rois):>5}{avg:>+9.1f}%{med:>+9.1f}%"
                  f"{'  -':>10}{'  -':>10}  {hold:.0f}s")
    print()
    print("'peak ROI' = avg max unrealized gain during hold; "
          "'left' = avg peak - final ROI (money left on the table).")


if __name__ == "__main__":
    main()
