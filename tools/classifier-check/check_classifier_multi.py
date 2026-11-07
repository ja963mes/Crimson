"""Re-classify a labelled CSV with the current hyip_classifier (Nov 7 copy of check_classifier.py).

Changes from check_classifier.py:
  1. Several run folders, chosen by CSV row number:  --run 2-101=<dir> --run 102-287=<dir>
     (row 1 is the header, so the first site is row 2).
  2. The crawler verdict column is named with --vc (e.g. "Crawler Label"); none for a file without one.
  3. Rows no run folder covers are "pages only, no crawler evidence": judged from the downloaded pages
     alone, and reported separately. Such a site did not load if it has no home.html or only the ISP
     sinkhole page.
  4. A NONE result (nothing could be read) counts as did-not-load, never as NO.
  5. The ISP sinkhole check reads the whole page, not the first 5,000 characters.
  6. Pages-only rows whose home page came back HTTP 4xx/5xx (pages_dir/index.json home_status) did not load.

Usage: python check_classifier_multi.py <csv> <pages_dir> [--run LO-HI=DIR ...] [--vc COLUMN] [--show] [--out rows.csv]
"""
import argparse, collections, csv, json, os, re, sys
sys.path.insert(0, r"D:\Coding\crimson\crawler-test\authentication_crawling")
import hyip_classifier as hc

ap = argparse.ArgumentParser()
ap.add_argument('csv')
ap.add_argument('pages')
ap.add_argument('--run', action='append', default=[], help='LO-HI=DIR, CSV row numbers (header = row 1)')
ap.add_argument('--vc', default=None, help='crawler verdict column')
ap.add_argument('--show', action='store_true')
ap.add_argument('--out', default=None)
a = ap.parse_args()

runs = []
for spec in a.run:
    rng, d = spec.split('=', 1)
    lo, hi = (int(x) for x in rng.split('-'))
    runs.append((lo, hi, d))

def run_for(rowno):
    for lo, hi, d in runs:
        if lo <= rowno <= hi:
            return d
    return None

def lab(s):
    s = (s or '').strip().upper()
    return 'YES' if s.startswith('YES') else 'NO' if s.startswith('NO') else ''

mine = lambda l: lab(l) or 'DEAD'
def folder(d): return os.path.join(a.pages, re.sub(r'[^a-z0-9.-]', '_', d))
SINK = 'optimum safe browsing'

def page_html(d, kind):
    f = os.path.join(folder(d), kind + '.html')
    if not os.path.exists(f):
        return None, 'missing'
    html = open(f, encoding='utf-8', errors='replace').read()
    # Whole page: the sinkhole page's <title> sits ~7 KB in, after inline CSS (check_classifier.py
    # looked at the first 5,000 characters and missed it, Nov 7).
    if SINK in html.lower():
        return None, 'sinkhole'
    return html, 'ok'

rows = list(csv.DictReader(open(a.csv, encoding='utf-8-sig')))
_ix = os.path.join(a.pages, 'index.json')
index = json.load(open(_ix, encoding='utf-8')) if os.path.exists(_ix) else {}
out = []
for i, r in enumerate(rows):
    rowno = i + 2
    d = r['domain'].strip().lower()
    his = mine(r['HYIP'])
    run = run_for(rowno)
    group = 'crawler' if run else 'pages-only'
    cv_raw = (r.get(a.vc) or '').strip() if a.vc else ''
    cv = lab(cv_raw) or ('UNREACHABLE' if cv_raw else '')
    rec = {'row': rowno, 'domain': d, 'group': group, 'his': his, 'crawler': cv, 'status': '', 'clf': '', 'step': '', 'reasons': ''}
    out.append(rec)
    if group == 'crawler' and not cv:
        sys.exit(f"row {rowno} {d}: a run folder covers it but the verdict column is empty")
    if his == 'DEAD':
        rec['status'] = 'his DEAD'
        continue
    ev = hc.SiteEvidence('https://' + d + '/')
    if group == 'crawler':
        if cv == 'UNREACHABLE':
            rec['status'] = 'no load: crawler UNREACHABLE'
            continue
        p = os.path.join(run, d, 'hyip_evidence.json')
        if not os.path.exists(p):
            sys.exit(f"row {rowno} {d}: no {p}")
        e = json.load(open(p, encoding='utf-8'))
        for k, v in (e.get('features') or {}).items():
            ev.add(k, v)
        ev.read = max(1, len(e.get('pages_read') or []))
        ev.title = e.get('title') or ''
        for k in hc.title_feats(ev.title, ev.domain):
            ev.add(k, 'title')
    else:
        _, st = page_html(d, 'home')
        if st != 'ok':
            rec['status'] = 'no load: no home page' if st == 'missing' else 'no load: ISP sinkhole'
            continue
        hs = (index.get(d) or {}).get('home_status')
        if isinstance(hs, int) and hs >= 400:  # e.g. Railway "not provisioned" 404, WordPress database-error 500
            rec['status'] = f'no load: home page HTTP error'
            continue
    for k in ('home', 'about', 'plans', 'how', 'download', 'mining', 'services', 'register'):
        html, st = page_html(d, k)
        if html is not None:
            ev.add_page(html, f"https://{d}/{k}", 'register' if k == 'register' else ('home' if k == 'home' else 'page'), count=False)
    v = hc.classify(ev)
    got = {'A': 'YES', 'B': 'NO'}.get(v['verdict'], 'NONE')
    rec.update(clf=got, step=v['step'], reasons='; '.join(v['reasons'])[:200])
    rec['status'] = 'no load: classifier NONE' if got == 'NONE' else 'judged'

def report(recs, name):
    his = collections.Counter(x['his'] for x in recs)
    print(f"\n== {name}: {len(recs)} sites = {his['YES']} YES + {his['NO']} NO + {his['DEAD']} DEAD")
    live = [x for x in recs if x['his'] != 'DEAD']
    print(f"   minus {his['DEAD']} DEAD -> {len(live)}")
    nl = collections.Counter((x['status'], x['his']) for x in live if x['status'].startswith('no load'))
    for st in sorted({s for s, _ in nl}):
        y, n = nl[(st, 'YES')], nl[(st, 'NO')]
        print(f"   minus {y + n} did not load [{st[9:]}] ({y} your YES, {n} your NO)")
    j = [x for x in live if x['status'] == 'judged']
    print(f"   = {len(j)} judged ({sum(x['his'] == 'YES' for x in j)} your YES, {sum(x['his'] == 'NO' for x in j)} your NO)")
    def table(key, who):
        c = collections.Counter((x['his'], x[key]) for x in j)
        tp, fn, fp, tn = c[('YES', 'YES')], c[('YES', 'NO')], c[('NO', 'YES')], c[('NO', 'NO')]
        n = len(j)
        assert tp + fn + fp + tn == n, c
        print(f"   {who:<10} TP {tp:>3} FN {fn:>3} FP {fp:>3} TN {tn:>3} | match {tp + tn} + disagree {fp + fn} = {n}"
              f" | {100 * (tp + tn) / n:.1f}% | your YES caught {tp}/{tp + fn} | your NO kept NO {tn}/{tn + fp}")
    if j:
        table('clf', 'classifier')
        if all(x['crawler'] in ('YES', 'NO') for x in j) and name.find('pages only') < 0:
            table('crawler', 'crawler')
    return j

groups = [g for g in ('crawler', 'pages-only') if any(x['group'] == g for x in out)]
for g in groups:
    recs = [x for x in out if x['group'] == g]
    lo, hi = recs[0]['row'], recs[-1]['row']
    report(recs, f"{os.path.basename(a.csv)} rows {lo}-{hi}, " + ('crawler evidence + downloaded pages' if g == 'crawler' else 'pages only, no crawler evidence'))
if len(groups) > 1:
    report(out, f"{os.path.basename(a.csv)} all rows (classifier only)")

if a.show:
    print('\nDisagreements (row, domain, group, his, classifier, crawler, step, reasons):')
    for x in out:
        if x['status'] == 'judged' and x['his'] != x['clf']:
            print(f"   {x['row']:>3} {x['domain']:<40} {x['group']:<10} his {x['his']:<3} clf {x['clf']:<3} crawler {x['crawler'] or '-':<3} {x['step']}: {x['reasons'][:110]}")
if a.out:
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
