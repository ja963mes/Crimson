"""For every site in <pages>/index.json with a saved home page, fetch its register page (or login page
if there is no register link) into <pages>/<domain>/register.html. Plain GETs only; nothing is submitted.

Usage: python fetch_register.py <pages dir>
"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup

pages = sys.argv[1]
index = json.load(open(os.path.join(pages, 'index.json'), encoding='utf-8'))
REG = re.compile(r'\b(register|sign ?up|create (an |your )?account|open (an |your )?account|join now|registration)\b|[?&]a=signup|/register|/signup|/sign-up', re.I)
LOGIN = re.compile(r'\b(log ?in|sign ?in)\b|[?&]a=login|/login|/signin', re.I)

def folder(d):
    return os.path.join(pages, re.sub(r'[^a-z0-9.-]', '_', d))

def get(url):
    try:
        r = requests.get(url, impersonate='chrome', timeout=20, allow_redirects=True)
        return r.status_code, str(r.url), r.text[:2_000_000]
    except Exception as e:
        return None, url, ''

def one(d):
    f = folder(d)
    home = os.path.join(f, 'home.html')
    if not os.path.exists(home) or os.path.exists(os.path.join(f, 'register.html')):
        return d, 'skip'
    base = index[d].get('home_final') or ('https://' + d)
    soup = BeautifulSoup(open(home, encoding='utf-8', errors='replace').read(), 'html.parser')
    host = urlparse(base).netloc.lower().removeprefix('www.')
    reg, login = None, None
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith(('mailto:', 'tel:', 'javascript:')) or href in ('#', ''):
            continue
        try:
            full = urljoin(base, href).split('#')[0]
            h = urlparse(full).netloc.lower().removeprefix('www.')
        except ValueError:
            continue
        if not full.startswith('http') or not (h == host or h.endswith('.' + host) or host.endswith('.' + h)):
            continue
        txt = a.get_text(' ', strip=True) + ' ' + urlparse(full).path + ('?' + urlparse(full).query if urlparse(full).query else '')
        if not reg and REG.search(txt):
            reg = full
        elif not login and LOGIN.search(txt):
            login = full
    target = reg or login
    if not target:
        return d, 'no link'
    st, final, body = get(target)
    if st and st < 400 and body:
        open(os.path.join(f, 'register.html'), 'w', encoding='utf-8').write(body)
        return d, 'register' if reg else 'login'
    return d, 'failed'

if __name__ == '__main__':
    t0 = time.time()
    todo = [d for d in index if os.path.exists(os.path.join(folder(d), 'home.html'))]
    stats = {}
    with ThreadPoolExecutor(24) as ex:
        def safe_one(d):
            try:
                return one(d)
            except Exception as e:
                return d, 'error'
        for n, (d, res) in enumerate(ex.map(safe_one, todo), 1):
            stats[res] = stats.get(res, 0) + 1
            if n % 200 == 0:
                print(f"{n}/{len(todo)} {stats} {time.time()-t0:.0f}s", flush=True)
    print(f"done {len(todo)}: {stats} {time.time()-t0:.0f}s", flush=True)
