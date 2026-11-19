#!/usr/bin/env python3
"""submit_scambusters.py -- submit this VM's HYIP verdicts to the ScamBusters Google Form.

Runs on the Crimson VMs as the last step of crimson-crawler.service, after build_wallets.py. It
reads what the pass already wrote next to the crawler:

  * hyip_evidence_<domain>.json  -- one per site crawled, written by log_hyip_verdict();
                                    {"domain", "verdict": "A" | "B" | "none", ...}
  * wallets.csv                  -- one row per (domain, address) from build_wallets.py, with the
                                    form option for the address in "suggested_type"

and submits every verdict-A site, with up to three wallets per submission, to the same form the
batches were sent to by hand (build_scambusters_batch.py builds the manual pages).

WHAT IT WILL NOT SUBMIT BY ITSELF
    A wallet whose "suggested_type" is empty is one whose coin/network could not be read from the
    page text next to the address. Those were always settled by eye against the deposit screenshot
    before going in, so those addresses (only those -- the site and its other wallets are still
    sent) are written to scambusters_review.csv instead of being guessed at --
    submitting a Tron address labelled as BEP20 puts bad data in a shared team form. Pass
    --submit-all to send them anyway (the address is still correct; only the network may be wrong).

DE-DUPLICATION
    Every submission is appended to scambusters_submitted.csv next to this script, and that file
    plus any hyip-submissions-batch*.csv in --history-dir are read first. A domain already sent is
    skipped only when there is nothing new to send: no wallets at all, or only addresses this VM
    already submitted for it. The hand-made batches record the site but not its addresses, so any
    wallet found for one of those sites is sent (RESUBMIT). Failed posts are not counted as sent,
    so the next pass retries them.

    The three VMs crawl disjoint domain sets (each feeds from its own worker), so per-VM history is
    enough to stop repeats between passes. The 34 hand-made batches in vm-results/ do overlap with
    what is already in urls.txt, so ship those to --history-dir once or the first pass will
    resubmit sites sent weeks ago.

    python3 submit_scambusters.py --dry-run          # print what would be sent, post nothing
    python3 submit_scambusters.py --submit           # actually post
    python3 submit_scambusters.py --submit --submit-all
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time
from collections import OrderedDict, defaultdict

try:                                     # the crawler's HTTP client, present on the VMs
    from curl_cffi import requests as http
    _IMPERSONATE = {'impersonate': 'chrome'}
except ImportError:                      # plain requests is enough for a form POST
    import requests as http
    _IMPERSONATE = {}

HERE = os.path.dirname(os.path.abspath(__file__))

# --- The form -------------------------------------------------------------------------------
# Field IDs are the ones build_scambusters_batch.py pre-fills; that script is the source of truth
# for them. They are repeated here because this one runs on a VM, where crawler-test/ does not
# exist. If the form ever changes, change both.
FORM_ID = '<SCAMBUSTERS_FORM_ID>'
POST_URL = f'https://docs.google.com/forms/d/e/{FORM_ID}/formResponse'
VIEW_URL = f'https://docs.google.com/forms/d/e/{FORM_ID}/viewform'
RECORDED = 'Your response has been recorded'
REPORTER = '<REPORTER_NAME>'
TEAM = '<TEAM_NAME>'
E_NAME, E_TEAM, E_URL, E_HAS = 'entry.875276213', 'entry.412962342', 'entry.1657083940', 'entry.2055547632'
# (address, type, "another wallet?") for each wallet section.
# The final section ends at Submit and has no continuation field.
SLOTS = [
    ('entry.1873532104', 'entry.544785560', 'entry.86695500'),
    ('entry.697521327', 'entry.194979260', 'entry.699771877'),
    ('entry.149874880', 'entry.376372511', 'entry.2013994313'),
    ('entry.488196448', 'entry.1371270006', 'entry.1636015098'),
    ('entry.1739772075', 'entry.539561973', 'entry.302920822'),
    ('entry.1055357440', 'entry.1615734467', 'entry.1367092717'),
    ('entry.1165621432', 'entry.1701123728', 'entry.1010853385'),
    ('entry.1365485564', 'entry.2094161748', 'entry.1929787519'),
    ('entry.869050649', 'entry.324099120', 'entry.1824683539'),
    ('entry.878031999', 'entry.1211640999', 'entry.1010972670'),
    ('entry.655030584', 'entry.695406805', 'entry.644779567'),
    ('entry.1190001002', 'entry.801151560', 'entry.43047289'),
    ('entry.833047747', 'entry.1416597159', 'entry.1597865358'),
    ('entry.575248873', 'entry.864876887', 'entry.1675642471'),
    ('entry.492936210', 'entry.195594857', 'entry.1381569709'),
    ('entry.139479398', 'entry.1797015394', 'entry.287137048'),
    ('entry.985540116', 'entry.1481416730', 'entry.728559200'),
    ('entry.1409144259', 'entry.417783461', 'entry.1555785146'),
    ('entry.648532023', 'entry.1676771200', 'entry.117761167'),
    ('entry.618179007', 'entry.1414015831', 'entry.1540095334'),
    ('entry.586322776', 'entry.620710113', 'entry.1251589751'),
    ('entry.653453372', 'entry.1079379058', 'entry.20938337'),
    ('entry.1991102856', 'entry.65023672', 'entry.2136565313'),
    ('entry.1977595112', 'entry.1530409790', 'entry.1274461823'),
    ('entry.557990384', 'entry.735821422', 'entry.1498520906'),
    ('entry.1460807599', 'entry.695304425', 'entry.1082727240'),
    ('entry.2068169538', 'entry.590264443', 'entry.342267688'),
    ('entry.1548272503', 'entry.1452879095', 'entry.2143577029'),
    ('entry.1481889921', 'entry.34456929', 'entry.1559181563'),
    ('entry.1838978892', 'entry.613191902', None),
]
PER_FORM = len(SLOTS)

SUBMITTED = os.path.join(HERE, 'scambusters_submitted.csv')
REVIEW = os.path.join(HERE, 'scambusters_review.csv')
SUBMITTED_FIELDS = ['submitted_utc', 'domain', 'part', 'parts', 'resubmit', 'addresses', 'vm', 'status']


def bare(host):
    host = (host or '').strip().lower()
    host = re.sub(r'^[a-z][a-z0-9+.-]*://', '', host).split('/', 1)[0]
    return host[4:] if host.startswith('www.') else host.strip('.')


def verdict_a_domains(pattern):
    """[(domain, path)] for every hyip_evidence_*.json whose verdict is A, oldest file first."""
    out = []
    for path in sorted(glob.glob(pattern), key=os.path.getmtime):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                ev = json.load(f)
        except Exception as e:
            print(f"[sb] WARN: cannot read {path}: {e}", file=sys.stderr)
            continue
        v = ev.get('verdict')
        if isinstance(v, dict):  # the crawler writes the classifier's whole verdict dict
            v = v.get('verdict')
        if str(v or '').upper() != 'A':
            continue
        dom = bare(ev.get('domain') or ev.get('url'))
        if dom:
            out.append((dom, path))
    # one entry per domain, keeping the newest verdict for it
    seen = OrderedDict()
    for dom, path in out:
        seen[dom] = path
    return list(seen.items())


def wallets_by_domain(path):
    """{domain: [(address, suggested_type)]}, deposit-page labels first as build_wallets sorted them."""
    out = defaultdict(list)
    if not os.path.exists(path):
        return out
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            dom, addr = bare(row.get('domain')), (row.get('address') or '').strip()
            if dom and addr:
                out[dom].append((addr, (row.get('suggested_type') or '').strip()))
    return out


def history(history_dirs, submitted_path):
    """(domains, addrs, addr_known) from this VM's log and the hand-made batches.

    addr_known is the subset of domains whose ADDRESSES are on record. The manual
    hyip-submissions-batch*.csv files are "index,url,recorded,timestamp" -- they name the site but
    not what was sent with it -- so for those domains there is no way to tell a new wallet from one
    already submitted. ~1,000 of them are in the batches and already sit in urls.txt. Such a domain
    is resubmitted whenever wallets were found for it (the batches mostly went out without any); a
    repeat of the bare URL adds nothing, so that alone is skipped."""
    domains, addrs, addr_known = set(), defaultdict(set), set()
    if os.path.exists(submitted_path):
        with open(submitted_path, newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                if (row.get('status') or '').startswith('FAILED'):
                    continue              # not sent; the next pass retries it
                d = bare(row.get('domain'))
                if d:
                    domains.add(d)
                    addr_known.add(d)
                    addrs[d] |= {a for a in (row.get('addresses') or '').split() if a}
    for folder in history_dirs:
        for path in glob.glob(os.path.join(folder, 'hyip-submissions-batch*.csv')):
            with open(path, newline='', encoding='utf-8-sig') as f:
                for row in csv.reader(f):
                    for cell in row:
                        cell = cell.strip()
                        if cell.startswith('http'):
                            d = bare(cell)
                            if d:
                                domains.add(d)
    return domains, addrs, addr_known


def payload(domain, wallets, part, parts):
    """The POST body for one submission: a site and up to 30 of its wallets."""
    q = [(E_NAME, REPORTER), (E_TEAM, TEAM), (E_URL, f'https://{domain}/'),
         (E_HAS, 'Yes' if wallets else 'No')]
    for i, (addr, option) in enumerate(wallets):
        slot_addr, slot_type, slot_more = SLOTS[i]
        q += [(slot_addr, addr), (slot_type, option)]
        if slot_more is not None:
            q.append((slot_more, 'Yes' if i + 1 < len(wallets) else 'No'))
    # Each wallet is its own form section; without the section path the form drops their answers.
    q += [('fvv', '1'), ('pageHistory', ','.join(str(n) for n in range(len(wallets) + 1)))]
    return q


def fresh_fbzx():
    """The per-load token the form expects back with a response."""
    r = http.get(VIEW_URL, timeout=30, **_IMPERSONATE)
    m = re.search(r'name="fbzx" value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError(f'no fbzx on the form page (HTTP {r.status_code})')
    return m.group(1)


def main():
    ap = argparse.ArgumentParser(description="Submit this VM's verdict-A sites to the ScamBusters form")
    ap.add_argument('--evidence', default=os.path.join(HERE, 'hyip_evidence_*.json'),
                    help='glob for the crawler\'s per-site verdict files')
    ap.add_argument('--wallets', default=os.path.join(HERE, 'wallets.csv'))
    ap.add_argument('--history-dir', action='append', default=[],
                    help='folder holding hyip-submissions-batch*.csv (repeatable)')
    ap.add_argument('--submit', action='store_true', help='actually POST (otherwise this is a dry run)')
    ap.add_argument('--submit-all', action='store_true',
                    help='also submit wallets whose coin/network could not be read from the page')
    ap.add_argument('--dry-run', action='store_true', help='explicit dry run; the default anyway')
    ap.add_argument('--limit', type=int, default=0, help='at most N submissions this run (0 = no cap)')
    ap.add_argument('--pause', type=float, default=2.0, help='seconds between posts (default 2)')
    ap.add_argument('--state-dir', default=HERE,
                    help='where scambusters_submitted.csv and scambusters_review.csv live '
                         '(default: next to this script)')
    args = ap.parse_args()

    submitted_path = os.path.join(args.state_dir, os.path.basename(SUBMITTED))
    review_path = os.path.join(args.state_dir, os.path.basename(REVIEW))
    post = args.submit and not args.dry_run
    vm = os.uname().nodename if hasattr(os, 'uname') else ''
    done_domains, done_addrs, addr_known = history(args.history_dir, submitted_path)
    wallets = wallets_by_domain(args.wallets)
    sites = verdict_a_domains(args.evidence)

    queued, review, skipped = [], [], []
    for domain, _path in sites:
        found = wallets.get(domain, [])
        resubmit = domain in done_domains
        if resubmit and not found:
            skipped.append((domain, 'already submitted, no wallets found'))
            continue
        sent_before = done_addrs.get(domain, set())
        new = [(a, opt) for a, opt in found if a not in sent_before]   # resend only what is new
        if resubmit and not new:
            skipped.append((domain, 'already submitted, no new wallets'))
            continue
        flagged = [a for a, opt in new if not opt]
        if flagged and not args.submit_all:
            # Only the addresses whose coin/network is unknown wait for a check; the site and its
            # other wallets still go out now.
            review.append((domain, new, flagged))
            new = [(a, opt) for a, opt in new if opt]
            if resubmit and not new:
                skipped.append((domain, 'new wallets all need a coin/network check'))
                continue
        groups = [new[i:i + PER_FORM] for i in range(0, len(new), PER_FORM)] or [[]]
        for n, group in enumerate(groups, 1):
            queued.append((domain, group, n, len(groups), resubmit))

    if args.limit:
        queued = queued[:args.limit]

    print(f"[sb] {len(sites)} verdict-A site(s); {len(queued)} submission(s) queued, "
          f"{len(review)} held for review, {len(skipped)} skipped")
    for domain, why in skipped:
        print(f"[sb]   skip {domain}: {why}")

    if review:
        with open(review_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['domain', 'address', 'ioc_label_missing', 'note'])
            for domain, found, flagged in review:
                for addr, opt in found:
                    w.writerow([domain, addr, '' if opt else 'yes',
                                opt or 'coin/network not in the page text -- check the deposit screenshot'])
        print(f"[sb] {len(review)} site(s) need a coin/network check -> {review_path} "
              f"(re-run with --submit-all to send them regardless)")

    if not queued:
        return 0

    new_file = not os.path.exists(submitted_path)
    sent = fail = 0
    with open(submitted_path, 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=SUBMITTED_FIELDS)
        if new_file:
            w.writeheader()
        for domain, group, part, parts, resubmit in queued:
            body = payload(domain, group, part, parts)
            tag = f"{domain} [{part} of {parts}]" + (' RESUBMIT' if resubmit else '')
            if not post:
                print(f"[sb] DRY-RUN would submit {tag}: {' '.join(a for a, _ in group) or 'no wallets'}")
                continue
            status = ''
            try:
                r = http.post(POST_URL, data=body + [('fbzx', fresh_fbzx())], timeout=30, **_IMPERSONATE)
                ok = r.status_code == 200 and RECORDED in r.text
                status = 'recorded' if ok else f'HTTP {r.status_code}, no confirmation'
            except Exception as e:
                status = f"error: {str(e).splitlines()[0] if str(e) else e!r}"
                ok = False
            if ok:
                sent += 1
                print(f"[sb] submitted {tag} ({status})")
            else:
                fail += 1
                print(f"[sb] FAILED {tag} ({status})", file=sys.stderr)
            w.writerow({'submitted_utc': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
                        'domain': domain, 'part': part, 'parts': parts,
                        'resubmit': 'yes' if resubmit else '',
                        'addresses': ' '.join(a for a, _ in group), 'vm': vm,
                        'status': status if ok else f'FAILED {status}'})
            f.flush()
            if args.pause:
                time.sleep(args.pause)

    if post:
        print(f"[sb] {sent} submitted, {fail} failed -> {submitted_path}")
    return 1 if fail and not sent else 0


if __name__ == '__main__':
    sys.exit(main())
