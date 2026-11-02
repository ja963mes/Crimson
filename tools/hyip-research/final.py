"""Local: features.json -> report_data.json (prevalence per family, Venn regions, template matches) + label-conflict CSV"""
import json, sys, csv, collections, math
S, OUTCSV = sys.argv[1], sys.argv[2]
F = json.load(open(f'{S}/features.json', encoding='utf-8'))
FAMS = ['broker/trading', 'call-center', 'MLM', 'news/review funnel', 'mining/generator', 'malware', 'other investment']
STRUCT = json.load(open(f'{S}/prevalence.json'))  # only for the struct flag per feature
is_struct = {r['feature']: r['struct'] for r in STRUCT}

def has(d, *ks):
    return all(k in F[d]['f'] for k in ks)

def any_(d, *ks):
    return any(k in F[d]['f'] for k in ks)

# template signatures (live HTML only)
def lead_funnel(d):
    return F[d]['live_ok'] and has(d, 'lead_form_phone') and not any_(d, 'password_field_any', 'signup_form') \
        and any_(d, 'intl_tel_input', 'lead_form_first_last', 'title_glyph', 'title_official_country')

def hyip_template(d):
    return F[d]['live_ok'] and any_(d, 'signup_form', 'password_field_any') and has(d, 'pct_per_period') \
        and any_(d, 'plan_tiers', 'min_max_deposit', 'principal_return', 'signup_wallet_fields', 'goldcoders_hscript', 'last_tx_table')

conf = []
for d, v in F.items():
    if v['family'] == 'HYIP' and lead_funnel(d):
        conf.append((d, v['group'], 'labelled HYIP, page is the call-center lead-funnel template', v['title']))
    if v['family'] in FAMS and hyip_template(d):
        conf.append((d, v['group'], 'labelled non-HYIP, page is the HYIP account + plans template', v['title']))
with open(OUTCSV, 'w', newline='', encoding='utf-8') as fh:
    w = csv.writer(fh)
    w.writerow(['domain', 'current_label', 'template_match', 'page_title'])
    for r in sorted(conf, key=lambda r: (r[2], r[0])):
        w.writerow(r)
cc = collections.Counter(r[2] for r in conf)
print(cc)

# HYIP set without the lead-funnel lookalikes, for a clean A/B comparison
lookalike = {r[0] for r in conf if r[2].startswith('labelled HYIP')}
groups = collections.defaultdict(list)
for d, v in F.items():
    fam = v['family']
    if fam == 'HYIP':
        groups['HYIP_all'].append(d)
        if d not in lookalike:
            groups['HYIP'].append(d)
    elif fam in FAMS:
        groups[fam].append(d)
        groups['OTHER'].append(d)

def rate(fam, k):
    ds = groups[fam]
    if is_struct.get(k, False):
        ds = [d for d in ds if F[d]['live_ok']]
    n = sum(1 for d in ds if k in F[d]['f'])
    return n, len(ds)

def fisher_p(a, b, c, d):
    n = a + b + c + d; r1, c1 = a + b, a + c
    lf = lambda x: math.lgamma(x + 1)
    p = lambda x: math.exp(lf(r1) + lf(n - r1) + lf(c1) + lf(n - c1) - lf(n) - lf(x) - lf(r1 - x) - lf(c1 - x) - lf(n - r1 - c1 + x))
    p0 = p(a); lo, hi = max(0, r1 + c1 - n), min(r1, c1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= p0 * (1 + 1e-7)))

skip = lambda k: k.startswith('tld_') and k not in ('tld_com', 'tld_not_com') or k.startswith('ioc_') and k not in ('ioc_phoneNumber', 'ioc_email', 'ioc_telegramHandle')
rows = []
for k in sorted({k for v in F.values() for k in v['f']}):
    if skip(k):
        continue
    r = {'k': k, 'struct': is_struct.get(k, False)}
    for g in ['HYIP', 'HYIP_all', 'OTHER'] + FAMS:
        r[g] = rate(g, k)
    (a, na), (c, nc) = r['HYIP'], r['OTHER']
    pH, pO = a / na, c / nc
    r['pH'], r['pO'], r['p'] = pH, pO, fisher_p(a, na - a, c, nc - c)
    fr = {f: r[f][0] / r[f][1] for f in FAMS if r[f][1] >= 10}
    r['fam_hi'] = [f for f, x in fr.items() if x >= 0.4 and x >= 2.5 * max(pH, 0.02)]
    if pH >= 0.15 and pH >= 1.8 * pO and r['p'] < 0.001:
        r['region'] = 'A'
    elif (pO >= 0.12 and pO >= 1.8 * pH and r['p'] < 0.001) or r['fam_hi']:
        r['region'] = 'B'
    elif pH >= 0.3 and pO >= 0.3:
        r['region'] = 'AB'
    else:
        r['region'] = 'weak'
    rows.append(r)
json.dump({'rows': rows, 'n': {g: (len(groups[g]), sum(1 for d in groups[g] if F[d]['live_ok'])) for g in groups},
           'conflicts': conf, 'lookalike_n': len(lookalike)}, open(f'{S}/report_data.json', 'w', encoding='utf-8'), ensure_ascii=False)
print({g: (len(groups[g]), sum(1 for d in groups[g] if F[d]['live_ok'])) for g in groups})
for reg in ['A', 'AB', 'B']:
    print('\n==', reg)
    for r in sorted([r for r in rows if r['region'] == reg], key=lambda r: -(r['pH'] - r['pO'])):
        fr = ' '.join(f"{f[:6]}={round(100*r[f][0]/r[f][1]) if r[f][1] else '-'}" for f in FAMS)
        print(f"{r['k']:24s} H={100*r['pH']:5.1f} O={100*r['pO']:5.1f} p={r['p']:.0e} {'S' if r['struct'] else ' '} {r['fam_hi']} | {fr}")
