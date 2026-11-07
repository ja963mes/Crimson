"""Fetch the home page + up to 6 topic subpages for every labelled domain in vm-results/confirmed-*.csv.

Usage: python fetch_family_pages.py <vm-results dir> <out dir>
Writes <out>/<domain>/<kind>.html and <out>/index.json (label, family, pages, errors).
Run from the scripts folder (not the out dir); pages are only parsed, never executed.
"""
import csv, glob, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup

vm_results, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)

def family(label):
    u = re.sub(r"\s+", " ", label.upper().replace("’", "'").replace("�", "'"))
    if u.startswith('YES'):
        return 'HYIP'
    if not u.startswith('NO'):
        return None  # DEAD, MAYBE, blank
    if re.search(r"BRO?KER|BOKER|WEALTH MANAGEMENT", u): return 'BROKER'
    if re.search(r"SOFTWARE|BOT|INVESTMENT APP|TRADING APP|CRYPTO TRACKER", u): return 'SOFTWARE'
    if re.search(r"MINER|MINING", u): return 'MINER'
    if re.search(r"PROP|SIMULATION", u): return 'PROP'
    if re.search(r"CALL", u): return 'CALL'
    if re.search(r"MLM|LEADER", u): return 'MLM'
    if re.search(r"NEWS|REVIEW|REVIERW|ARTICLE|BLOG|LINKS? TO|LEADS TO", u): return 'NEWS/REVIEW'
    if re.search(r"GENERATOR|EARN CRYPTO|BUY CRYPTO", u): return 'GENERATOR'
    return 'OTHER-NO'

labels = {}
for f in sorted(glob.glob(os.path.join(vm_results, 'confirmed-*.csv'))):
    if '.bak' in f:
        continue
    for r in csv.DictReader(open(f, encoding='utf-8-sig', errors='replace')):
        d = (r.get('domain') or '').strip().lower()
        h = (r.get('HYIP') or '').strip()
        if d and h:
            labels[d] = {'label': h, 'file': os.path.basename(f), 'title': r.get('page_title', '')}
todo = {d: v for d, v in labels.items() if family(v['label'])}
for v in todo.values():
    v['family'] = family(v['label'])

SUB = [
    ('about', r'\b(about( us)?|company|who we are|our team|team|\?a=cust&page=about)\b'),
    ('plans', r'\b(plans?|pricing|prices|packages?|investments?|invest|\?a=rules|offers?|tariffs?)\b'),
    ('how', r'\b(how it works|how to (start|invest|get started)|get started|getting started)\b'),
    ('download', r'\b(download|app|apps|mobile app|desktop|install|get the app)\b'),
    ('mining', r'\b(mining|miners?|hash ?rate|hash ?power|machines?|hardware|asic|contracts?|cloud mining)\b'),
    ('services', r'\b(services|products|solutions|platform|features|trading)\b'),
]

def get(url):
    try:
        r = requests.get(url, impersonate='chrome', timeout=20, allow_redirects=True)
        return r.status_code, str(r.url), r.text[:2_000_000]
    except Exception as e:
        return None, url, 'ERR ' + type(e).__name__ + ' ' + str(e)[:160]

def safe(domain):
    return re.sub(r'[^a-z0-9.-]', '_', domain)

def fetch(domain):
    rec = {'domain': domain, 'pages': {}}
    folder = os.path.join(out, safe(domain))
    os.makedirs(folder, exist_ok=True)
    st, final, body = get('https://' + domain)
    if st is None:
        st, final, body = get('http://' + domain)
    rec['home_status'], rec['home_final'] = st, final
    if st is None:
        rec['error'] = body
        return rec
    open(os.path.join(folder, 'home.html'), 'w', encoding='utf-8').write(body)
    rec['pages']['home'] = final
    try:
        soup = BeautifulSoup(body, 'html.parser')
    except Exception:
        return rec
    host = urlparse(final).netloc.lower().removeprefix('www.')
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith(('mailto:', 'tel:', 'javascript:')) or href in ('#', ''):
            continue
        full = urljoin(final, href).split('#')[0]
        h = urlparse(full).netloc.lower().removeprefix('www.')
        if not full.startswith('http') or not (h == host or h.endswith('.' + host) or host.endswith('.' + h)):
            continue
        links.append((full, (a.get_text(' ', strip=True) + ' ' + urlparse(full).path).lower()))
    seen = {final.split('#')[0]}
    for kind, pat in SUB:
        rx = re.compile(pat, re.I)
        for full, txt in links:
            if full in seen or not rx.search(txt):
                continue
            seen.add(full)
            s2, f2, b2 = get(full)
            if s2 and s2 < 400:
                open(os.path.join(folder, kind + '.html'), 'w', encoding='utf-8').write(b2)
                rec['pages'][kind] = f2
            break
    return rec

if __name__ == '__main__':
    t0 = time.time()
    index = {}
    with ThreadPoolExecutor(24) as ex:
        futs = {ex.submit(fetch, d): d for d in todo}
        for n, fu in enumerate(as_completed(futs), 1):
            d = futs[fu]
            try:
                rec = fu.result()
            except Exception as e:
                rec = {'domain': d, 'error': 'CRASH ' + repr(e)[:160], 'pages': {}}
            rec.update(todo[d])
            index[d] = rec
            if n % 100 == 0:
                ok = sum(1 for r in index.values() if r.get('pages'))
                print(f"{n}/{len(todo)} fetched, {ok} with a home page, {time.time()-t0:.0f}s", flush=True)
                json.dump(index, open(os.path.join(out, 'index.json'), 'w', encoding='utf-8'))
    json.dump(index, open(os.path.join(out, 'index.json'), 'w', encoding='utf-8'))
    ok = sum(1 for r in index.values() if r.get('pages'))
    print(f"done: {len(index)} domains, {ok} with a home page, {time.time()-t0:.0f}s", flush=True)
