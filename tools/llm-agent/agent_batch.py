"""Run agent_site.py over a list of sites, one at a time, with a total spending cap.

    python agent_batch.py sites/smoke.txt --headed
    python agent_batch.py sites/yes-11-03.txt --out runs-llm-yes-11-03 --max-cost 5
    python agent_batch.py sites/yes-11-03.txt --only winvests.online,bitanalysis.co --headed

A site list is a text file with one domain or URL per line ('#' starts a comment). Each site runs in
its own process (its own Chrome) and inherits this terminal, so the live step log and the keyboard
commands (p / r / n <text> / s) work as in agent_site.py. Sites that already have a result.json in the
output folder are skipped, so a stopped batch continues where it left off.

Writes <out>/summary.csv after every site. Stops before the next site once the total cost of the
batch (all result.json files in <out>) reaches --max-cost.
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = sys.executable

SUMMARY_COLUMNS = ['domain', 'level', 'agent_level', 'wallet_count', 'wallets', 'seconds', 'steps', 'cost_usd',
                   'cost_usd_own_calc', 'prompt_tokens', 'cached_tokens', 'completion_tokens', 'mail_reads',
                   'prompt_version', 'stop_reason', 'blockers', 'payment_options_seen', 'claimed_not_in_page',
                   'wallets_off_site', 'notes']


def read_sites(path):
    sites = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            sites.append(line)
    return sites


def folder_name(site):
    netloc = re.sub(r'^https?://', '', site).split('/')[0].lower()
    netloc = netloc[4:] if netloc.startswith('www.') else netloc
    return re.sub(r'[^A-Za-z0-9.-]+', '_', netloc)


def summary_row(r):
    t = r.get('tokens') or {}
    return {
        'domain': r['domain'], 'level': r['level'], 'agent_level': r['agent_level'],
        'wallet_count': len(r['wallets']), 'wallets': ' '.join(w['address'] for w in r['wallets']),
        'seconds': r['seconds'], 'steps': r['steps'], 'cost_usd': r['cost_usd'],
        'cost_usd_own_calc': r['cost_usd_own_calc'], 'prompt_tokens': t.get('prompt'),
        'cached_tokens': t.get('prompt_cached'), 'completion_tokens': t.get('completion'),
        'mail_reads': r['mail_reads'], 'prompt_version': r['prompt_version'], 'stop_reason': r['stop_reason'],
        'blockers': ' | '.join(r['blockers']), 'payment_options_seen': ' | '.join(r['payment_options_seen']),
        'claimed_not_in_page': ' '.join(w['address'] for w in r['wallets_claimed_not_in_page']),
        'wallets_off_site': ' '.join(f"{w['address']}@{w['page_url']}" for w in r.get('wallets_off_site', [])),
        'notes': r['notes'],
    }


def load_results(out, order):
    results = {}
    for site in order:
        f = out / folder_name(site) / 'result.json'
        if f.exists():
            results[site] = json.loads(f.read_text(encoding='utf-8'))
    return results


def write_summary(out, results):
    with open(out / 'summary.csv', 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_COLUMNS)
        w.writeheader()
        for r in results.values():
            w.writerow(summary_row(r))


def cost_of(r):
    return r.get('cost_usd') or r.get('cost_usd_own_calc') or 0.0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('sites', help='text file, one domain or URL per line')
    p.add_argument('--out', default=None, help='output folder (default runs-llm-<sites file name>)')
    p.add_argument('--only', default='', help='comma-separated domains to run from the list')
    p.add_argument('--max-cost', type=float, default=5.0, help='USD for the whole batch (James: $5)')
    args, passthrough = p.parse_known_args()   # anything else goes to agent_site.py (--headed, --chrome, ...)

    sites = read_sites(args.sites)
    if args.only:
        wanted = {folder_name(s) for s in args.only.split(',')}
        sites = [s for s in sites if folder_name(s) in wanted]
    out = Path(args.out) if args.out else HERE / f"runs-llm-{Path(args.sites).stem}"
    out.mkdir(parents=True, exist_ok=True)

    results = load_results(out, sites)
    spent = sum(cost_of(r) for r in results.values())
    todo = [s for s in sites if s not in results]
    print(f"{len(sites)} sites, {len(results)} already done (${spent:.3f}), {len(todo)} to run; cap ${args.max_cost:.2f}; "
          f"output {out}", flush=True)

    for i, site in enumerate(todo, 1):
        if spent >= args.max_cost:
            print(f"\nCost cap reached: ${spent:.3f} of ${args.max_cost:.2f}. Stopping before {site}.", flush=True)
            break
        print(f"\n######## site {i}/{len(todo)}: {site}   (batch so far ${spent:.3f})", flush=True)
        rc = subprocess.call([PYTHON, str(HERE / 'agent_site.py'), site, '--out-dir', str(out), '--interactive', *passthrough])
        results = load_results(out, sites)
        if site in results:
            spent = sum(cost_of(r) for r in results.values())
        else:
            print(f"!! {site}: no result.json (exit code {rc})", flush=True)
        write_summary(out, results)

    write_summary(out, results)
    levels = {}
    for r in results.values():
        levels[r['level']] = levels.get(r['level'], 0) + 1
    wallets = sum(len(r['wallets']) for r in results.values())
    print(f"\n=== {len(results)} sites done: {levels} | {wallets} wallets | ${spent:.3f} total | {out / 'summary.csv'}", flush=True)


if __name__ == '__main__':
    main()
