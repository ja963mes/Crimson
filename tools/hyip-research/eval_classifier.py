"""Offline check of authentication_crawling/hyip_classifier.py.

1. Research set: classify() over features.json (every domain labelled HYIP or one of the seven
   other-scam families) against James's labels. Prints the confusion matrix and the disagreements,
   and how the label-template conflicts in report_data.json (48 HYIP-labelled lead funnels, 12
   other labels with the HYIP template) come out.
2. The 44 sites crawled Nov 1 (vm-results/ab-categories-2026-11-01.csv): the research features plus
   what the crawl saved (pages after login, deposit pages) and logged (sign-up form, login, lead
   form, off-site sign-up, deposit plan controls, wallets), against the manual A/B pass.

    python eval_classifier.py            # both
    python eval_classifier.py --list     # also list every research disagreement
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'authentication_crawling'))
import hyip_classifier as hc  # noqa: E402

FAMS = hc.OTHER_FAMILIES
AB_CSV = os.path.join(ROOT, 'vm-results', 'ab-categories-2026-11-01.csv')
RUN_DIRS = [os.path.join(HERE, '..', 'runs-2026-10-27-rest'), os.path.join(HERE, '..', 'runs-2026-10-28-rest')]


def research_evidence(d, v):
    ev = hc.SiteEvidence('https://' + d)
    for k, src in v['f'].items():
        ev.add(k, 'research ' + src)
    # features.json has no page text for the patterns added since (prop firm, subscription, hash
    # rate, plan cards); the page title is the one piece of text it kept.
    for k in hc.offer_feats(v.get('title') or ''):
        ev.add(k, 'research title')
    ev.read = 1
    return ev


def research(F, show_all):
    conf = json.load(open(os.path.join(HERE, 'report_data.json'), encoding='utf-8'))['conflicts']
    lookalike = {c[0] for c in conf if c[2].startswith('labelled HYIP')}
    template = {c[0] for c in conf if c[2].startswith('labelled non-HYIP')}
    m = collections.Counter()
    by_step = collections.Counter()
    fam_hits = collections.Counter()
    wrong = []
    for d, v in F.items():
        if v['family'] != 'HYIP' and v['family'] not in FAMS:
            continue
        r = hc.classify(research_evidence(d, v))
        truth = 'A' if v['family'] == 'HYIP' else 'B'
        live = 'live' if v['live_ok'] else 'OCR only'
        m[(truth, r['verdict'], live)] += 1
        if not v['live_ok']:
            continue
        by_step[(truth, r['step'], r['verdict'])] += 1
        if truth == 'B' and r['verdict'] == 'B':
            fam_hits[(v['family'], r['family'] == v['family'])] += 1
        if truth != r['verdict'] and v['live_ok']:
            wrong.append((d, v['group'], live, r, d in lookalike, d in template))

    print("== 1. Research set (live pages): James's label (rows) vs verdict (columns)")
    n_live = sum(c for (t, p, live), c in m.items() if live == 'live')
    agree = m[('A', 'A', 'live')] + m[('B', 'B', 'live')]
    print(f"  label HYIP : A {m[('A', 'A', 'live')]:4}  B {m[('A', 'B', 'live')]:4}")
    print(f"  label other: A {m[('B', 'A', 'live')]:4}  B {m[('B', 'B', 'live')]:4}      agreement {agree / n_live:.1%} of {n_live}")
    print(f"  ({sum(c for (t, p, live), c in m.items() if live != 'live')} more had no live page in the research, only the "
          f"detection-time OCR; the crawler would say 'none (unreachable)' for them, so they are left out)")
    print("  Since Nov 2 a YES needs a real plan offer (plan cards, deposit-page plan choices, payout-wallet sign-up,\n"
          "  HYIP script). features.json has no page text and no crawl, so plan cards and deposit plans cannot show\n"
          "  up here and most HYIPs read as 'no offer'. Judge the classifier on crawl runs instead (regression/rescore.py).")
    print("  by step:")
    for (truth, step, verdict), c in sorted(by_step.items()):
        print(f"    label {'HYIP ' if truth == 'A' else 'other'}  {step:8} -> {verdict}: {c}")
    ok = sum(c for (f, hit), c in fam_hits.items() if hit)
    print(f"  family right on other-labelled B verdicts: {ok} of {sum(fam_hits.values())}")
    for fam in FAMS:
        print(f"    {fam:20} {fam_hits[(fam, True)]:3} right, {fam_hits[(fam, False)]:3} other family")
    la = [w for w in wrong if w[4]]
    te = [w for w in wrong if w[5]]
    print(f"  label-template conflicts: {len(la)} of {len(lookalike)} HYIP-labelled lead funnels -> B; "
          f"{len(te)} of {len(template)} other labels with the HYIP template -> A")
    rest = [w for w in wrong if not w[4] and not w[5]]
    print(f"  other disagreements: {len(rest)} "
          f"({sum(1 for w in rest if w[1] == 'HYIP')} HYIP-labelled -> B, {sum(1 for w in rest if w[1] != 'HYIP')} other -> A)")
    why = collections.Counter((w[1] == 'HYIP', w[3]['step'], w[3]['reasons'][0].split(';')[0][:70]) for w in rest)
    for (hy, step, reason), c in sorted(why.items(), key=lambda x: -x[1]):
        print(f"    {c:4}  {'HYIP -> B ' if hy else 'other -> A'}  {step:7} {reason}")
    if show_all:
        for d, group, live, r, *_ in sorted(rest, key=lambda w: (w[1] != 'HYIP', w[3]['step'], w[0])):
            print(f"    {d:45} {group[:28]:28} -> {r['verdict']} {r['step']:7} | {'; '.join(r['reasons'])[:200]}")


def crawl_evidence(ev, folder):
    """Add what one crawl run saved and logged."""
    log = os.path.join(folder, 'logs', 'logfile.log')
    text = open(log, encoding='utf-8', errors='replace').read() if os.path.exists(log) else ''
    if 'Sign-up form chosen' in text:
        ev.add('crawl_signup_form', 'crawl log')
    if re.search(r" - INFO - Success: ", text) or 'Already logged in' in text or 'Login result: success' in text:
        ev.add('crawl_logged_in', 'crawl log')
    if 'Agent-gated sign-up' in text:
        ev.add('crawl_lead_form', 'crawl log')
    if 'Off-site sign-up:' in text:
        ev.add('crawl_offsite_signup', 'crawl log')
    for m in re.finditer(r"Deposit page (\S+): form #.*?; (plan \w+ '[^']*' \((\d+) options\)|no plan control)", text):
        ev.add('crawl_deposit_form', m.group(1))
        if m.group(3):
            ev.add('crawl_deposit_plans', m.group(1))
    wallets = []
    for m in re.finditer(r"wallet-like strings (?:on the page as served: )?(\[[^\]]*\])", text):
        wallets += re.findall(r"'([^']+)'", m.group(1))
    ev.add_wallets(sorted(set(wallets)), 'crawl log')
    pages = sorted(glob.glob(os.path.join(folder, '*', 'page_*.html'))) + sorted(glob.glob(os.path.join(folder, '*', 'deposit_page_*_0.html')))
    for p in pages:
        raw = open(p, encoding='utf-8', errors='replace').read()
        url = f"https://{ev.domain}/saved/{os.path.basename(p)[:70]}"
        ev.add_page(raw, url, kind='account', count=False)
        if os.path.basename(p).startswith('page_'):
            links = re.findall(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', raw, flags=re.S | re.I)
            ev.add_backoffice([(h, re.sub(r'<[^>]+>|\s+', ' ', t)) for h, t in links], url)
    return bool(text)


def crawled_44(F):
    rows = list(csv.DictReader(open(AB_CSV, encoding='utf-8')))
    print(f"\n== 2. The {len(rows)} sites crawled Nov 1, manual A/B (ab-categories-2026-11-01.csv) vs verdict")
    agree, diff = 0, []
    counts = collections.Counter()
    for r in rows:
        d = r['domain']
        key = next((k for k in (d, d.removeprefix('www.'), 'www.' + d) if k in F), None)
        ev = research_evidence(key, F[key]) if key else hc.SiteEvidence('https://' + d)
        folder = next((os.path.join(rd, d) for rd in RUN_DIRS if os.path.isdir(os.path.join(rd, d))), None)
        if folder:
            crawl_evidence(ev, folder)
        v = hc.classify(ev)
        counts[v['verdict']] += 1
        if v['verdict'] == r['my_category']:
            agree += 1
        else:
            diff.append((d, r, v))
    print(f"  verdicts: A {counts['A']}  B {counts['B']}  none {counts['none']}   (manual: 33 A / 11 B)")
    print(f"  agree with the manual pass: {agree} of {len(rows)}")
    for d, r, v in diff:
        print(f"  {d}: manual {r['my_category']} ({r['family']}), verdict {v['verdict']} ({v['family_name']}, {v['step']})")
        print(f"      manual note: {r['note'] or '-'}")
        print(f"      reasons: {'; '.join(v['reasons'])}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--list', action='store_true', help="list every research disagreement")
    args = parser.parse_args()
    F = json.load(open(os.path.join(HERE, 'features.json'), encoding='utf-8'))
    research(F, args.list)
    crawled_44(F)


if __name__ == '__main__':
    main()
