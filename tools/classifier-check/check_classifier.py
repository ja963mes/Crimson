"""Re-classify a labelled CSV with the current hyip_classifier: blind-run evidence features + fetched pages."""
import csv, json, os, re, sys, collections
sys.path.insert(0, r"D:\Coding\crimson\crawler-test\authentication_crawling")
import hyip_classifier as hc
csv_path, run_dir, pages = sys.argv[1], sys.argv[2], sys.argv[3]
rows = list(csv.DictReader(open(csv_path, encoding='utf-8-sig')))
VC = "Crawler's Verdict"
mine = lambda l: 'YES' if l.strip().upper().startswith('YES') else 'NO' if l.strip().upper().startswith('NO') else 'DEAD'
crawl = lambda v: 'YES' if v.strip().upper().startswith('YES') else 'NO' if v.strip().upper().startswith('NO') else 'UNREACHABLE'
def folder(d): return os.path.join(pages, re.sub(r'[^a-z0-9.-]', '_', d))
c = collections.Counter(); out = []
for r in rows:
    d = r['domain'].strip().lower()
    if mine(r['HYIP']) not in ('YES', 'NO') or crawl(r.get(VC) or '') == 'UNREACHABLE':
        continue
    ev = hc.SiteEvidence('https://' + d + '/')
    p = os.path.join(run_dir, d, 'hyip_evidence.json')
    if os.path.exists(p):
        e = json.load(open(p, encoding='utf-8'))
        for k, v in (e.get('features') or {}).items():
            ev.add(k, v)
        ev.read = max(1, len(e.get('pages_read') or []))
        ev.title = e.get('title') or ''
        for k in hc.title_feats(ev.title, ev.domain):
            ev.add(k, 'title')
    for k in ('home', 'about', 'plans', 'how', 'download', 'mining', 'services', 'register'):
        f = os.path.join(folder(d), k + '.html')
        if os.path.exists(f):
            html = open(f, encoding='utf-8', errors='replace').read()
            if 'optimum safe browsing' in html[:5000].lower():
                continue
            ev.add_page(html, f"https://{d}/{k}", 'register' if k == 'register' else ('home' if k == 'home' else 'page'), count=False)
    v = hc.classify(ev)
    got = {'A': 'YES', 'B': 'NO'}.get(v['verdict'], 'NONE')
    c[(mine(r['HYIP']), got)] += 1
    out.append((d, mine(r['HYIP']), got, v['step'], '; '.join(v['reasons'])[:120]))
tp, fn, fp, tn = c[('YES','YES')], c[('YES','NO')], c[('NO','YES')], c[('NO','NO')]
n = sum(c.values())
print(f"judged {n}: TP {tp} FN {fn} FP {fp} TN {tn} | agree {tp+tn} = {100*(tp+tn)/n:.1f}% | its YES right {tp}/{tp+fp} | your YES caught {tp}/{tp+fn} | other {dict((k,v) for k,v in c.items() if k[1]=='NONE')}")
if '--show' in sys.argv:
    for o in out:
        if o[1] != o[2]: print("  ", o)
