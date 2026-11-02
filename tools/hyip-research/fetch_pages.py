"""Run on VM1 in ~/hyip-feature-research.
1. Join labels.json with the hub results logs (detection-time title, OCR text, js libs, IOCs) -> records.json
2. Fetch live homepage + up to 5 discovered subpages per domain with curl_cffi -> html/<domain>/<n>.html + fetch_index.json
"""
import json, glob, os, re, sys, hashlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup

os.chdir(os.path.expanduser('~/hyip-feature-research'))
labels = {r['domain']: r for r in json.load(open('labels.json'))}
wanted = {d for d, r in labels.items() if r['group'].split(':')[0] in ('HYIP', 'OTHER', 'NO-PLAIN', 'NO-AMBIG')}

# ---- 1. detection-time records
recs = {}
for f in sorted(glob.glob('results/*/results.log*')):
    for line in open(f, errors='replace'):
        i = line.find('{')
        if i < 0:
            continue
        try:
            d = json.loads(line[i:])
        except Exception:
            continue
        u = (d.get('url') or '').lower()
        for cand in (u, u[4:] if u.startswith('www.') else None):
            if cand in wanted:
                keep = {k: d.get(k) for k in ('url', 'title', 'ioc', 'js_libraries', 'text', 'creation_date', 'ip_info')}
                keep['_src'] = f
                recs[cand] = keep  # later files win (newest)
json.dump(recs, open('records.json', 'w'))
print('records matched', len(recs), 'of', len(wanted), flush=True)

if len(sys.argv) > 1 and sys.argv[1] == 'records-only':
    sys.exit()

# ---- 2. live fetch
SUB = [
    ('login', r'\b(log ?in|sign ?in|member login|client area|my account|\?a=login)\b'),
    ('register', r'\b(register|sign ?up|create (an )?account|open (an )?account|join now|get started|start (investing|trading)|\?a=signup|registration)\b'),
    ('contact', r'\b(contact|support|\?a=support)\b'),
    ('plans', r'\b(plans?|pricing|packages?|invest(ment)?s?|\?a=rules|offers?)\b'),
    ('about', r'\b(about|company|who we are|\?a=cust&page=about)\b'),
]

def get(url):
    try:
        r = requests.get(url, impersonate='chrome', timeout=20, allow_redirects=True)
        return r.status_code, str(r.url), r.text[:3_000_000]
    except Exception as e:
        return None, url, 'ERR ' + type(e).__name__ + ' ' + str(e)[:200]

def fetch(domain):
    out = {'domain': domain, 'pages': []}
    os.makedirs(f'html/{domain}', exist_ok=True)
    st, final, body = get('https://' + domain)
    if st is None:
        st, final, body = get('http://' + domain)
    out['home_status'], out['home_final'] = st, final
    if st is None:
        out['error'] = body
        return out
    open(f'html/{domain}/home.html', 'w').write(body)
    out['pages'].append({'kind': 'home', 'url': final, 'status': st})
    try:
        soup = BeautifulSoup(body, 'html.parser')
    except Exception:
        return out
    host = urlparse(final).netloc
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
            continue
        full = urljoin(final, href)
        txt = (a.get_text(' ', strip=True) + ' ' + href).lower()
        links.append((full, txt))
    seen = {final}
    for kind, pat in SUB:
        for full, txt in links:
            if re.search(pat, txt) and full not in seen:
                ph = urlparse(full).netloc
                # off-site register links are recorded but still fetched (funnels hand off to other domains)
                seen.add(full)
                s2, f2, b2 = get(full)
                out['pages'].append({'kind': kind, 'url': f2, 'status': s2, 'offsite': ph != host})
                if s2 is not None:
                    open(f'html/{domain}/{kind}.html', 'w').write(b2)
                break
    return out

todo = sorted(wanted)
res = []
with ThreadPoolExecutor(24) as ex:
    for i, r in enumerate(ex.map(fetch, todo)):
        res.append(r)
        if i % 100 == 0:
            print(i, flush=True)
json.dump(res, open('fetch_index.json', 'w'))
print('done', sum(1 for r in res if r.get('home_status')), 'reachable of', len(res))
