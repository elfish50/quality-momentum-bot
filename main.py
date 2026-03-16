"""
main.py — CLI runner for Quality Momentum Scanner
Works with the existing bot.py / scanner.py / universe.py architecture.

Usage:
  python3.11 main.py                           # full scan (Nasdaq + NYSE + SP500)
  python3.11 main.py --tickers COST AAPL MSFT  # specific tickers
  python3.11 main.py --sp500                   # SP500 only
  python3.11 main.py --refresh                 # force refresh universe cache
  python3.11 main.py --min-score 60            # only show signals >= 60
"""

import argparse
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from universe import load_universe, get_all_tickers
from scanner  import run_scan, format_alert, format_summary


def parse_args():
    p = argparse.ArgumentParser(description="BB 3rd Touch Breakout Scanner")
    p.add_argument("--tickers",   nargs="+",            help="Scan specific tickers only")
    p.add_argument("--sp500",     action="store_true",  help="Scan SP500 only")
    p.add_argument("--refresh",   action="store_true",  help="Force refresh universe cache")
    p.add_argument("--min-score", type=float, default=0, help="Min signal score (default 0)")
    p.add_argument("--max",       type=int,  default=120, help="Max tickers to scan (default 120)")
    return p.parse_args()


def main():
    args = parse_args()

    print(f"\n  Quality Momentum Scanner")
    print(f"  {datetime.now().strftime('%A %d %B %Y — %H:%M')}")
    print(f"  Strategy: BB 3rd Touch Breakout + Berkshire Quality Screen")
    print(f"  {'='*50}")

    # Build ticker list
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
        print(f"\n  Mode: targeted — {len(tickers)} tickers")
    elif args.sp500:
        u       = load_universe(force_refresh=args.refresh)
        tickers = u.get("SP500", [])[:args.max]
        print(f"\n  Mode: SP500 — {len(tickers)} tickers")
    else:
        u       = load_universe(force_refresh=args.refresh)
        tickers = u.get("ALL", [])[:args.max]
        print(f"\n  Mode: full universe — {len(tickers)} tickers")

    if not tickers:
        print("  ERROR: no tickers loaded. Check your internet connection.")
        sys.exit(1)

    # Run scan
    alerts, elapsed = run_scan(tickers)

    # Filter by min score
    if args.min_score > 0:
        before = len(alerts)
        alerts = [a for a in alerts if a["signal_score"] >= args.min_score]
        print(f"\n  Score filter >= {args.min_score}: {before} -> {len(alerts)} signals")

    # Print summary
    print("\n" + format_summary(alerts, elapsed, len(tickers)))

    # Print each signal
    if alerts:
        print("\n" + "="*36)
        print("FULL SIGNAL DETAILS")
        print("="*36)
        for sig in alerts:
            print("\n" + format_alert(sig))
    else:
        print("\n  No signals today. Market may be in uptrend (no BB lower touches).")
        print("  Try: python3.11 main.py --tickers COST AAPL MSFT XOM NVDA AMZN")


if __name__ == "__main__":
    main()
