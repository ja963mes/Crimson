"""Local: report_data.json -> ../authentication_crawling/hyip_features.json, the per-feature rates
hyip_classifier.py scores with: the HYIP rate (925 sites, lead-funnel lookalikes left out), the
other-scam rate (216 sites), the rate in each other-scam family and the Venn region (A, AB, B, weak).
Each rate is [sites with the feature, sites measured]; structural features count live pages only."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FAMS = ['broker/trading', 'call-center', 'MLM', 'news/review funnel', 'mining/generator', 'malware', 'other investment']

rows = json.load(open(os.path.join(HERE, 'report_data.json'), encoding='utf-8'))['rows']
out = {r['k']: {'region': r['region'], 'H': r['HYIP'], 'O': r['OTHER'], 'fam': {f: r[f] for f in FAMS}} for r in rows}
path = os.path.join(HERE, '..', 'authentication_crawling', 'hyip_features.json')
with open(path, 'w', encoding='utf-8') as f:  # one feature per line
    f.write('{\n' + ',\n'.join(f"{json.dumps(k)}: {json.dumps(out[k])}" for k in sorted(out)) + '\n}\n')
print(f"{len(out)} features -> {os.path.normpath(path)}")
