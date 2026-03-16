import argparse, sys
from dotenv import load_dotenv
load_dotenv()
from universe import load_universe
from screener import get_priority_tickers
from scanner import run_scan, format_alert, format_summary

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--tickers', nargs='+')
    p.add_argument('--refresh', action='store_true')
    p.add_argument('--min-score', type=float, default=0)
    p.add_argument('--max', type=int, default=200)
    p.add_argument('--full', action='store_true')
    return p.parse_args()

def main():
    args = parse_args()
    print('Quality Momentum Scanner')
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
        print(f'Mode: targeted - {len(tickers)} tickers')
    elif args.full:
        u = load_universe(force_refresh=args.refresh)
        tickers = u.get('ALL', [])
        print(f'Mode: full - {len(tickers)} tickers')
    else:
        u = load_universe(force_refresh=args.refresh)
        all_tickers = u.get('ALL', [])
        print('Mode: smart - fetching priority tickers...')
        priority = get_priority_tickers(universe=all_tickers)
        rest = [t for t in all_tickers if t not in set(priority)]
        fill = max(0, args.max - len(priority))
        tickers = priority + rest[:fill]
        print(f'Priority: {len(priority)} | Fill: {fill} | Total: {len(tickers)}')
    if not tickers:
        print('ERROR: no tickers')
        sys.exit(1)
    scan_list = tickers if args.full else tickers[:args.max]
    alerts, elapsed = run_scan(scan_list)
    if args.min_score > 0:
        alerts = [a for a in alerts if a['signal_score'] >= args.min_score]
    print(format_summary(alerts, elapsed, len(scan_list)))
    if alerts:
        for sig in alerts:
            print(format_alert(sig))
    else:
        print('No signals today.')

if __name__ == '__main__':
    main()
