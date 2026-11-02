"""Crawler run over a batch of sites from a HYIP label CSV (vm-results/hyip-yes-*.csv).

Runs crawler_script.py --url on the first 30 sites whose HYIP column starts with YES (one after
another, each in crawler-test/runs-<CSV date>/<domain>/), shows the crawler's output live, reads
the outcome the same way run_regression.py does, and writes summary.csv in the run folder after
every site, so a stopped run keeps what it finished and --resume carries on from there.

    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv            # first 30 YES sites
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --resume   # carry on after a stop
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --list     # show the 30 sites, run nothing
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --report   # results of the finished run
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --only rakizfx.com,fxsh6.com
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --limit 0  # every YES site
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --skip 30 --limit 0 --out ..\\runs-2026-10-28-rest
"""
import argparse
import collections
import csv
import os
import re
import sys

from run_regression import HERE, LEVELS, outcome, run_site

FIELDS = ['domain', 'level', 'wallet_count', 'wallets', 'reason', 'seconds', 'label']


def load_batch(path):
    """[(domain, label)] for the YES rows of a hyip-yes CSV, in file order, without repeats."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    sites, seen = [], set()
    for r in rows:
        domain, label = (r.get('domain') or '').strip(), (r.get('HYIP') or '').strip()
        if domain and label.upper().startswith('YES') and domain not in seen:
            seen.add(domain)
            sites.append((domain, label))
    return sites


def read_summary(path):
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_summary(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Crawler run over the YES sites of a hyip-yes CSV")
    parser.add_argument('csv', help="label CSV with domain and HYIP columns")
    parser.add_argument('--out', help="run folder (default crawler-test/runs-<date in the CSV name>)")
    parser.add_argument('--skip', type=int, default=0, help="leave out the first N YES sites (e.g. 30 for the rest after a first batch)")
    parser.add_argument('--limit', type=int, default=30, help="only the first N YES sites after --skip (default 30, 0 for all)")
    parser.add_argument('--only', help="comma-separated domains from the CSV")
    parser.add_argument('--resume', action='store_true', help="skip domains already in the folder's summary.csv")
    parser.add_argument('--show-browser', action='store_true', help="run Chrome with a visible window")
    parser.add_argument('--list', action='store_true', help="list the chosen sites and exit")
    parser.add_argument('--report', action='store_true', help="print the run folder's summary.csv and exit")
    args = parser.parse_args()

    sites = load_batch(args.csv)
    if args.only:
        wanted = {d.strip() for d in args.only.split(',') if d.strip()}
        unknown = wanted - {d for d, _ in sites}
        if unknown:
            sys.exit(f"Not a YES row in {args.csv}: {', '.join(sorted(unknown))}")
        sites = [s for s in sites if s[0] in wanted]
    sites = sites[args.skip:]
    if args.limit:
        sites = sites[:args.limit]

    if args.out:
        out = os.path.abspath(args.out)
    else:
        date = re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(args.csv))
        out = os.path.join(HERE, '..', 'runs-' + (date.group(0) if date else 'batch'))
    out = os.path.normpath(out)
    summary_file = os.path.join(out, 'summary.csv')

    if args.report:
        rows = read_summary(summary_file)
        if not rows:
            sys.exit(f"No results in {summary_file}")
        report(rows, summary_file)
        return
    rows = read_summary(summary_file) if args.resume else []
    done = {r['domain'] for r in rows}
    todo = [s for s in sites if s[0] not in done]
    if args.list:
        for i, (domain, label) in enumerate(todo, start=1):
            print(f"{i:3}  {domain}" + (f"  [{label}]" if label != 'YES' else ''))
        print(f"\n{len(todo)} sites to run into {out}" + (f" ({len(done)} already in summary.csv)" if done else ''))
        return
    if not args.resume and os.path.exists(summary_file):
        sys.exit(f"{summary_file} exists; use --resume to carry on, or --out for a new folder")

    os.makedirs(out, exist_ok=True)
    try:
        for i, (domain, label) in enumerate(todo, start=1):
            url = f"https://{domain}/"
            print(f"\n===== [{i}/{len(todo)}] {domain} ({url}) =====", flush=True)
            lines, seconds = run_site(domain, url, args.show_browser, runs=out)
            level, wallets, reason = outcome(lines)
            rows.append({'domain': domain, 'level': level, 'wallet_count': len(wallets), 'wallets': ' '.join(wallets),
                         'reason': reason, 'seconds': int(seconds), 'label': label})
            write_summary(summary_file, rows)
            print(f"----- {domain}: {level}, {len(wallets)} wallet-like strings, {int(seconds)} s" + (f" ({reason})" if reason else ''))
    except KeyboardInterrupt:
        print("\nStopped; summary.csv has the sites finished so far (rerun with --resume to carry on).")

    report(rows, summary_file)


def report(rows, summary_file):
    """Every site, best level first, then the count per level and the wallets found."""
    print(f"\n{'domain':45} {'level':13} {'wallets':>7} {'secs':>5}  reason")
    for r in sorted(rows, key=lambda r: -LEVELS.index(r['level'])):
        print(f"{r['domain'][:45]:45} {r['level']:13} {r['wallet_count']:>7} {r['seconds']:>5}  {r['reason']}")
    counts = collections.Counter(r['level'] for r in rows)
    print(f"\n{len(rows)} sites in {summary_file}")
    for level in reversed(LEVELS):
        if counts[level]:
            print(f"  {level:13} {counts[level]}")
    for r in rows:
        if r['level'] == 'WALLET':
            print(f"  {r['domain']}: {r['wallets']}")


if __name__ == '__main__':
    main()
