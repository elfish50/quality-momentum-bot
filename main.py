"""
QUALITY MOMENTUM SCANNER — Main Runner
Bollinger Band 3rd Touch Breakout | Berkshire Quality Screen | Momentum Math

TWO-PASS PIPELINE:
  Pass 1 — BB pre-filter (price only, concurrent, no Finnhub)
            Scans full Nasdaq + NYSE universe (~5000-8000 tickers)
            Keeps only tickers with a valid BB 3rd touch setup
            Typical result: 10-40 candidates

  Pass 2 — Deep analysis (shortlist only)
            Runs full analyze_ticker() with Finnhub fundamentals
            Applies quality screen + signal scoring
            Outputs final BUY signals with full trade details

Usage:
  python3.11 main.py                          # full scan (24h cached universe)
  python3.11 main.py --refresh                # force refresh universe from Alpaca
  python3.11 main.py --tickers AAPL MSFT COST # scan specific tickers only
  python3.11 main.py --top 20                 # show top N signals
  python3.11 main.py --min-score 60           # filter by minimum signal score
  python3.11 main.py --workers 30             # tune concurrency for Pass 1
"""

import os
import sys
import time
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from universe import get_universe
from strategy import (
    get_price_data,
    compute_bollinger,
    find_lower_band_touches,
    analyze_ticker,
)

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")

# ── Rate limit settings ───────────────────────────────────────────────────────
PASS1_WORKERS = 20    # parallel Alpaca fetches — Alpaca handles ~200 req/min
PASS2_SLEEP   = 1.1   # sec between Finnhub calls (free tier = 60 req/min)


# ── Pass 1: fast BB pre-filter (no Finnhub) ───────────────────────────────────

def _bb_prefilter(ticker: str) -> dict | None:
    """
    Price data only — checks for a valid BB 3rd touch pattern.
    Returns lightweight dict if pattern found, else None.
    """
    try:
        df = get_price_data(ticker)
        if df is None or len(df) < 60:
            return None

        df      = compute_bollinger(df)
        touches = find_lower_band_touches(df, lookback=60)

        if len(touches) < 3:
            return None

        # Last touch must be within 5 bars
        last_touch_idx = touches[-1]
        bars_since     = len(df) - 1 - df.index.get_loc(last_touch_idx)
        if bars_since > 5:
            return None

        # Touch span check — not all clumped in same day
        last_3     = touches[-3:]
        touch_span = df.index.get_loc(last_3[-1]) - df.index.get_loc(last_3[0])
        if touch_span < 5:
            return None

        current = df.iloc[-1]
        rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50.0

        return {
            "ticker":    ticker,
            "close":     float(current["Close"]),
            "rsi":       rsi,
            "n_touches": len(touches),
        }

    except Exception:
        return None


def run_pass1(tickers: list[str], workers: int = PASS1_WORKERS) -> list[dict]:
    """
    Concurrent BB pre-filter across the full universe.
    Returns shortlist of tickers with a confirmed BB setup.
    """
    total      = len(tickers)
    shortlist  = []
    done       = 0
    start_time = time.time()

    print(f"\n{'='*65}")
    print(f"  PASS 1 — BB pre-filter | {total} tickers | {workers} workers")
    print(f"{'='*65}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_bb_prefilter, t): t for t in tickers}

        for future in as_completed(futures):
            done  += 1
            result = future.result()

            if result:
                shortlist.append(result)
                print(f"  [{done:>5}/{total}] HIT  {result['ticker']:<8} "
                      f"RSI {result['rsi']:.0f} | {result['n_touches']} touches")

            elif done % 500 == 0:
                elapsed = time.time() - start_time
                rate    = done / elapsed
                eta     = (total - done) / rate if rate > 0 else 0
                print(f"  [{done:>5}/{total}] scanning...  "
                      f"{len(shortlist)} hits so far | ETA {eta:.0f}s")

    elapsed = time.time() - start_time
    print(f"\n  Pass 1 complete: {elapsed:.0f}s | "
          f"{len(shortlist)} candidates from {total} tickers")

    return shortlist


# ── Pass 2: deep quality + signal analysis ────────────────────────────────────

def run_pass2(candidates: list[dict], min_score: float = 0) -> list[dict]:
    """
    Full analyze_ticker() on shortlist — Finnhub rate limited.
    """
    total   = len(candidates)
    signals = []

    print(f"\n{'='*65}")
    print(f"  PASS 2 — Deep analysis | {total} candidates")
    print(f"  (Finnhub rate limit: 1 req/sec)")
    print(f"{'='*65}\n")

    for i, candidate in enumerate(candidates, 1):
        ticker = candidate["ticker"]
        print(f"  [{i:>3}/{total}] {ticker}...")

        result = analyze_ticker(ticker)

        if result and result["signal_score"] >= min_score:
            signals.append(result)
        elif not result:
            print(f"          → filtered out")

        if i < total:
            time.sleep(PASS2_SLEEP)

    return signals


# ── Output ────────────────────────────────────────────────────────────────────

def print_signals(signals: list[dict], top_n: int | None = None):
    if not signals:
        print("\n  No signals found today. Market may be trending or extended.")
        return

    signals.sort(key=lambda x: x["signal_score"], reverse=True)
    if top_n:
        signals = signals[:top_n]

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*65}")
    print(f"  SIGNALS — {ts} | {len(signals)} found")
    print(f"{'='*65}")

    # Summary table
    print(f"\n  {'TICKER':<7} {'SCORE':>5} {'HOLD':<22} {'PRICE':>8} "
          f"{'RSI':>5} {'VOL':>5} {'TP1%':>6} {'TP2%':>6}")
    print(f"  {'─'*72}")

    for s in signals:
        print(
            f"  {s['ticker']:<7} "
            f"{s['signal_score']:>5.1f} "
            f"{s['hold_time']:<22} "
            f"${s['price']:>7.2f} "
            f"{s['rsi']:>5.1f} "
            f"{s['vol_ratio']:>4.1f}x "
            f"{s['tp1_pct']:>+5.1f}% "
            f"{s['tp2_pct']:>+5.1f}%"
        )

    # Full trade details
    print(f"\n{'─'*65}")
    print("  TRADE DETAILS")
    print(f"{'─'*65}")

    for s in signals:
        stop_pct = (s["stop"] - s["price"]) / s["price"] * 100
        print(f"""
  {s['ticker']} — {s['name']}  [{s['sector']}]
  Signal {s['signal_score']:.0f} | Quality {s['quality_score']:.0f} | {s['hold_time']}
  Entry  : ${s['price']:.2f}
  Stop   : ${s['stop']:.2f}   ({stop_pct:+.1f}%)
  TP1    : ${s['tp1']:.2f}    ({s['tp1_pct']:+.1f}%)  ← middle band
  TP2    : ${s['tp2']:.2f}    ({s['tp2_pct']:+.1f}%)  ← upper band
  Size   : {s['shares']} shares | ${s['position_val']:,.0f} ({s['pct_account']:.1f}% acct) | Risk ${s['risk_dollars']:.0f}
  BB     : lower ${s['bb_lower']:.2f} | mid ${s['bb_mid']:.2f} | upper ${s['bb_upper']:.2f} | width {s['bb_width']:.1f}%
  Fundm  : ROE {s['roe']:.1f}% | GM {s['gross_margin']:.1f}% | EPS Gr {s['eps_growth']:.1f}% | D/E {s['debt_equity']:.2f} | PE {s['pe_ratio']:.1f}x""")
        if s.get("quality_notes"):
            print(f"  Weak   : {', '.join(s['quality_notes'])}")

    # Save CSV
    df    = pd.DataFrame(signals)
    fname = f"signals_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    df.to_csv(fname, index=False)
    print(f"\n  Saved → {fname}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="BB 3rd Touch Breakout Scanner")
    p.add_argument("--refresh",    action="store_true",  help="Force refresh universe cache")
    p.add_argument("--tickers",    nargs="+",             help="Scan specific tickers only")
    p.add_argument("--top",        type=int,  default=None, help="Show top N signals")
    p.add_argument("--min-score",  type=float, default=0,   help="Min signal score (default 0)")
    p.add_argument("--workers",    type=int,  default=PASS1_WORKERS, help="Pass 1 workers")
    return p.parse_args()


def main():
    args = parse_args()

    # Env check
    missing = [k for k, v in {
        "ALPACA_KEY": ALPACA_KEY,
        "ALPACA_SECRET": ALPACA_SECRET,
        "FINNHUB_KEY": FINNHUB_KEY,
    }.items() if not v]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    print(f"\n  Quality Momentum Scanner")
    print(f"  {datetime.now().strftime('%A %d %B %Y — %H:%M')}")

    # ── Universe ──────────────────────────────────────────────────────────────
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
        print(f"\n  Mode: targeted ({len(tickers)} tickers)")
    else:
        tickers = get_universe(force_refresh=args.refresh)
        print(f"\n  Mode: full universe ({len(tickers)} tickers)")

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    if len(tickers) <= 20:
        print("\n  Small list — skipping Pass 1, running full analysis directly")
        candidates = [{"ticker": t} for t in tickers]
    else:
        candidates = run_pass1(tickers, workers=args.workers)

    if not candidates:
        print("\n  No BB 3rd touch patterns found in universe today.")
        sys.exit(0)

    # ── Pass 2 ────────────────────────────────────────────────────────────────
    signals = run_pass2(candidates, min_score=args.min_score)

    # ── Output ────────────────────────────────────────────────────────────────
    print_signals(signals, top_n=args.top)


if __name__ == "__main__":
    main()
