"""Regression run of the crawler over a fixed set of sites.

Runs crawler_script.py --url on each site in sites.csv (one after another, each in
crawler-test/runs/<name>/), shows the crawler's output live, reads the outcome from that
output, and at the end prints a table plus the changes against the site's best known result
and against the previous run. Each run is saved as results/<YYYY-MM-DD_HHMM>.csv.

    python run_regression.py                      # every "reachable" site
    python run_regression.py --show-browser       # same, with Chrome windows
    python run_regression.py --only hubguardfxz,duduinvest
    python run_regression.py --all                # the "unreachable" ones too
    python run_regression.py --report             # only re-print the last run's summary

Outcome levels, best first: WALLET (logged in and wallet-like strings seen), LOGGED_IN,
SIGNED_UP (account made, not logged in), SIGNUP_FOUND (sign-up page found, sign-up failed),
BLOCKED (captcha or bot check on the sign-up page), NO_SIGNUP (no sign-up page found),
ERROR (crawler crashed, site unreachable, or run cut off).
"""
import argparse
import ast
import csv
import datetime
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CRAWLER = os.path.join(HERE, '..', 'authentication_crawling', 'crawler_script.py')
RUNS = os.path.join(HERE, '..', 'runs')
RESULTS = os.path.join(HERE, 'results')
SITES = os.path.join(HERE, 'sites.csv')
SITE_TIMEOUT = 15 * 60  # hard cap per site on top of the crawler's own limit

LEVELS = ['ERROR', 'NO_SIGNUP', 'BLOCKED', 'SIGNUP_FOUND', 'SIGNED_UP', 'LOGGED_IN', 'WALLET']
WALLET_LIST_RE = re.compile(r"wallet-like strings (?:on the page as served: )?(\[[^\]]*\])")


def outcome(lines):
    """(level, wallets, reason) read from one crawler run's output."""
    text = '\n'.join(lines)
    wallets = []
    for m in WALLET_LIST_RE.finditer(text):
        try:
            wallets += [w for w in ast.literal_eval(m.group(1)) if w not in wallets]
        except (ValueError, SyntaxError):
            pass
    processor = re.search(r"payment processor page on ([^,;\s]+)(?:, payment ID (\S+))?", text)

    def last(pattern):
        found = re.findall(pattern, text)
        return found[-1] if found else None

    if re.search(r" - INFO - Success: ", text):
        level = 'WALLET' if wallets else 'LOGGED_IN'
        reason = '' if wallets else 'no wallet-like strings on the deposit pages'
    elif 'Sign-up succeeded with' in text:
        # "Sign-up submitted ... no sign of success" (result unclear) does not count: on
        # aegisinvestmentgroup.com and nexgencrypto.live nothing was created.
        level = 'SIGNED_UP'
        reason = (last(r"Login result: failed - (.*)") or last(r"(Account created, waiting on e-mail verification)")
                  or last(r"(Could not find a login URL)") or 'not logged in')
    elif re.search(r"Captcha check on [^:]+:\s*.*skipping the site", text):
        level, reason = 'BLOCKED', last(r"Captcha check on \S+: (.*?), skipping the site")
    elif 'Sign-up form chosen' in text or 'Registration URL is' in text or 'Captcha check on' in text:
        level = 'SIGNUP_FOUND'
        error = last(r"ERROR - (An error occurred.*)")
        if error and error.rstrip().endswith('Message:'):
            error = 'no <form> or password field appeared within 5 s'  # the empty TimeoutException message
        reason = (('rejected: ' + last(r"Sign-up result: rejected - (.*)")) if last(r"Sign-up result: rejected - (.*)")
                  else ('sign-up result unclear; ' + (last(r"Login result: failed - (.*)") or 'not logged in')) if 'Sign-up submitted with' in text
                  else error or last(r"(No sign-up form found.*)") or 'sign-up not completed')
    elif 'No page with two or more password fields found' in text:
        level = 'NO_SIGNUP'
        reason = last(r"ERROR - An error occurred while fetching \S+: Message: (.*)") or 'no sign-up page found'
    else:
        level, reason = 'ERROR', last(r"ERROR - (.*)") or 'no result line'
    if processor:
        reason = (reason + '; ' if reason else '') + f"processor {processor.group(1)}" + (f" id {processor.group(2)}" if processor.group(2) else '')
    if 'Timeout exceeded for' in text:
        reason = (reason + '; ' if reason else '') + 'hit the 5-minute limit'
    relogins = len(re.findall(r"logging in again as", text))
    if relogins:
        reason = (reason + '; ' if reason else '') + f"re-logged in {relogins}x"
    return level, wallets, (reason or '')[:160]


def run_site(name, url, show_browser):
    """Run the crawler on one site, echoing its output; return (output lines, seconds)."""
    folder = os.path.join(RUNS, name)
    os.makedirs(folder, exist_ok=True)
    cmd = [sys.executable, os.path.abspath(CRAWLER), '--url', url] + (['--show-browser'] if show_browser else [])
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    start = time.time()
    proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace', env=env)
    killer = threading.Timer(SITE_TIMEOUT, proc.kill)
    killer.start()
    lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip('\n')
            print(line, flush=True)
            lines.append(line)
        proc.wait()
    finally:
        killer.cancel()
        if proc.poll() is None:
            proc.kill()
    if time.time() - start >= SITE_TIMEOUT:
        lines.append(f"ERROR - regression runner stopped the site after {SITE_TIMEOUT} s")
    return lines, time.time() - start


def load_sites():
    with open(SITES, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def previous_results(exclude=None):
    """{name: row} of the newest results file (other than `exclude`), and its file name."""
    if not os.path.isdir(RESULTS):
        return {}, None
    files = sorted(f for f in os.listdir(RESULTS) if f.endswith('.csv') and f != exclude)
    if not files:
        return {}, None
    with open(os.path.join(RESULTS, files[-1]), newline='', encoding='utf-8') as f:
        return {r['name']: r for r in csv.DictReader(f)}, files[-1]


def rank(level):
    return LEVELS.index(level) if level in LEVELS else -1


def report(rows, sites, results_file):
    known = {s['name']: s for s in sites}
    prev, prev_file = previous_results(exclude=os.path.basename(results_file) if results_file else None)
    print('\n' + '=' * 100)
    print(f"REGRESSION SUMMARY  ({len(rows)} sites)" + (f"   compared with {prev_file}" if prev_file else ''))
    print('=' * 100)
    print(f"{'site':22} {'result':12} {'wallets':>7}  {'best known':12} {'last run':12}  reason")
    worse, better = [], []
    for r in rows:
        best = known.get(r['name'], {}).get('best_known', '')
        last = prev.get(r['name'], {}).get('level', '')
        mark = ''
        if best and rank(r['level']) < rank(best):
            mark, _ = 'WORSE than best', worse.append(r['name'])
        elif best and rank(r['level']) > rank(best):
            mark, _ = 'NEW BEST', better.append(r['name'])
        if last and rank(r['level']) < rank(last) and r['name'] not in worse:
            mark, _ = 'WORSE than last run', worse.append(r['name'])
        print(f"{r['name'][:22]:22} {r['level']:12} {r['wallet_count']:>7}  {best:12} {last:12}  {r['reason'][:70]}"
              + (f"   <-- {mark}" if mark else ''))
    counts = {lvl: sum(1 for r in rows if r['level'] == lvl) for lvl in LEVELS}
    logged = counts['WALLET'] + counts['LOGGED_IN']
    print('-' * 100)
    print(f"wallets: {counts['WALLET']}   logged in (incl. wallets): {logged}   signed up only: {counts['SIGNED_UP']}   "
          f"sign-up failed: {counts['SIGNUP_FOUND']}   blocked: {counts['BLOCKED']}   no sign-up page: {counts['NO_SIGNUP']}   "
          f"error: {counts['ERROR']}")
    print(f"total wallet-like strings: {sum(int(r['wallet_count']) for r in rows)}")
    if better:
        print("NEW BEST:", ', '.join(better))
    if worse:
        print("REGRESSIONS:", ', '.join(worse))
    if results_file:
        print(f"saved: {results_file}")


def main():
    parser = argparse.ArgumentParser(description="Crawler regression run over sites.csv")
    parser.add_argument('--only', help="comma-separated site names from sites.csv")
    parser.add_argument('--all', action='store_true', help="include the 'unreachable' group")
    parser.add_argument('--show-browser', action='store_true', help="run Chrome with a visible window")
    parser.add_argument('--report', action='store_true', help="re-print the last run's summary and exit")
    args = parser.parse_args()
    sites = load_sites()

    if args.report:
        prev, prev_file = previous_results()
        if not prev:
            print("No results yet.")
            return
        report(list(prev.values()), sites, os.path.join(RESULTS, prev_file))
        return

    if args.only:
        wanted = {n.strip() for n in args.only.split(',') if n.strip()}
        unknown = wanted - {s['name'] for s in sites}
        if unknown:
            sys.exit(f"Not in sites.csv: {', '.join(sorted(unknown))}")
        chosen = [s for s in sites if s['name'] in wanted]
    else:
        chosen = [s for s in sites if args.all or s['group'] == 'reachable']

    os.makedirs(RESULTS, exist_ok=True)
    results_file = os.path.join(RESULTS, datetime.datetime.now().strftime('%Y-%m-%d_%H%M') + '.csv')
    rows = []
    try:
        for i, s in enumerate(chosen, start=1):
            print(f"\n===== [{i}/{len(chosen)}] {s['name']} ({s['url']}) =====", flush=True)
            lines, seconds = run_site(s['name'], s['url'], args.show_browser)
            level, wallets, reason = outcome(lines)
            rows.append({'name': s['name'], 'url': s['url'], 'level': level, 'wallet_count': len(wallets),
                         'wallets': ' '.join(wallets), 'reason': reason, 'seconds': int(seconds)})
            print(f"----- {s['name']}: {level}, {len(wallets)} wallet-like strings, {int(seconds)} s" + (f" ({reason})" if reason else ''))
    except KeyboardInterrupt:
        print("\nStopped; saving the sites finished so far.")
    finally:
        if rows:
            with open(results_file, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=['name', 'url', 'level', 'wallet_count', 'wallets', 'reason', 'seconds'])
                w.writeheader()
                w.writerows(rows)
            report(rows, sites, results_file)


if __name__ == '__main__':
    main()
