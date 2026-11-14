"""Crawler vs LLM agent on the same sites, per site and in totals.

    python compare.py runs-llm-yes-11-03 \
        --crawler ../runs-2026-11-03-rows2-201/summary.csv \
        --labels ../../vm-results/confirmed-2026-11-03-to-11-06.csv

Starts from the agent's sites, shows every exclusion (no crawler result, crawler page did not load,
agent run unfinished), then compares site by site: how far each got on the same level scale, and the
wallets each found. Totals: better / same / worse, new wallets, cost per site and per new wallet.
Writes <agent folder>/compare.csv.
"""
import argparse
import csv
import json
from pathlib import Path

# One scale for both. NO_SELF_SERVE (agent: lead form / invite-only) sits with NO_SIGNUP; the
# crawler's CLASSIFIED_B (stopped before sign-up by its HYIP check) sits just above ERROR.
RANK = {'ERROR': 0, 'UNFINISHED': 0, 'CLASSIFIED_B': 1, 'NO_SIGNUP': 2, 'NO_SELF_SERVE': 2, 'BLOCKED': 3,
        'SIGNUP_FOUND': 4, 'SIGNED_UP': 5, 'LOGGED_IN': 6, 'WALLET': 7}
DID_NOT_LOAD = ('ERR_CONNECTION_REFUSED', 'ERR_NAME_NOT_RESOLVED', 'ERR_CONNECTION_TIMED_OUT', 'start page did not load')


def base(domain):
    d = (domain or '').strip().lower()
    for prefix in ('https://', 'http://'):
        d = d[len(prefix):] if d.startswith(prefix) else d
    d = d.split('/')[0]
    return d[4:] if d.startswith('www.') else d


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('agent_dir', help='batch output folder (has <domain>/result.json)')
    p.add_argument('--crawler', required=True, nargs='+',
                   help="crawler summary.csv file(s) (domain, level, wallets, reason ...); later files win")
    p.add_argument('--labels', default=None, help="CSV with 'domain' and 'HYIP' columns (James's labels)")
    args = p.parse_args()

    agent_dir = Path(args.agent_dir)
    agent = {}
    for f in sorted(agent_dir.glob('*/result.json')):
        r = json.loads(f.read_text(encoding='utf-8'))
        agent[base(r['domain'])] = r
    crawler = {}
    for f in args.crawler:
        crawler.update({base(r['domain']): r for r in csv.DictReader(open(f, encoding='utf-8-sig'))})
    labels = {}
    if args.labels:
        labels = {base(r['domain']): (r.get('HYIP') or '').strip() for r in csv.DictReader(open(args.labels, encoding='utf-8-sig'))}

    rows, excluded = [], []
    for d, a in agent.items():
        c = crawler.get(d)
        if c is None:
            excluded.append((d, 'no crawler result for this site'))
            continue
        crawler_loaded = not any(s in (c.get('reason') or '') for s in DID_NOT_LOAD)
        cw = set((c.get('wallets') or '').replace(',', ' ').split())
        aw = {w['address'] for w in a['wallets']}
        ra, rc = RANK.get(a['level'], 0), RANK.get(c['level'], 0)
        rows.append({
            'domain': d, 'label': labels.get(d, ''),
            'crawler_level': c['level'] + ('' if crawler_loaded else ' (did not load)'),
            'agent_level': a['level'],
            'change': 'better' if ra > rc else 'worse' if ra < rc else 'same',
            'crawler_wallets': len(cw), 'agent_wallets': len(aw),
            'new_wallets': ' '.join(sorted(aw - cw)), 'missed_wallets': ' '.join(sorted(cw - aw)),
            'agent_cost_usd': a.get('cost_usd') or a.get('cost_usd_own_calc') or 0.0,
            'agent_seconds': a['seconds'], 'crawler_seconds': c.get('seconds', ''),
            'agent_steps': a['steps'], 'blockers': ' | '.join(a['blockers']), 'stop_reason': a['stop_reason'],
        })

    print(f"Agent results: {len(agent)} sites")
    for d, why in excluded:
        print(f"  excluded {d}: {why}")
    print(f"Compared: {len(rows)} = {len(agent)} - {len(excluded)} excluded\n")
    print(f"{'domain':42} {'label':6} {'crawler':28} {'agent':14} {'change':7} {'wallets c/a':11} {'new':4} {'$':>7}")
    for r in sorted(rows, key=lambda r: (r['change'] != 'better', r['change'] != 'worse', r['domain'])):
        print(f"{r['domain'][:42]:42} {r['label'][:6]:6} {r['crawler_level'][:28]:28} {r['agent_level']:14} {r['change']:7} "
              f"{r['crawler_wallets']:>4}/{r['agent_wallets']:<6} {len(r['new_wallets'].split()):>4} {r['agent_cost_usd']:>7.3f}")

    n = len(rows) or 1
    better = sum(r['change'] == 'better' for r in rows)
    same = sum(r['change'] == 'same' for r in rows)
    worse = sum(r['change'] == 'worse' for r in rows)
    new_w = sum(len(r['new_wallets'].split()) for r in rows)
    missed = sum(len(r['missed_wallets'].split()) for r in rows)
    cost = sum(r['agent_cost_usd'] for r in rows)
    wallet_sites_c = sum(r['crawler_level'].startswith('WALLET') for r in rows)
    wallet_sites_a = sum(r['agent_level'] == 'WALLET' for r in rows)
    print(f"\nbetter {better} + same {same} + worse {worse} = {len(rows)}")
    print(f"WALLET sites: crawler {wallet_sites_c}, agent {wallet_sites_a}")
    print(f"wallets: {new_w} new (agent only), {missed} found only by the crawler")
    print(f"agent cost: ${cost:.3f} total, ${cost / n:.3f} per site"
          + (f", ${cost / new_w:.3f} per new wallet" if new_w else ", no new wallets"))

    out = agent_dir / 'compare.csv'
    if rows:
        with open(out, 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"written: {out}")


if __name__ == '__main__':
    main()
