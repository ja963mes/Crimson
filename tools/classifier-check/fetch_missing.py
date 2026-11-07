"""Fetch pages for labelled domains that are not yet in <out>/index.json (reuses fetch_family_pages.fetch)."""
import json, os, sys
vm_results, out = sys.argv[1], sys.argv[2]
sys.argv = [sys.argv[0], vm_results, out]
import fetch_family_pages as F
from concurrent.futures import ThreadPoolExecutor
idx_path = os.path.join(out, 'index.json')
index = json.load(open(idx_path, encoding='utf-8'))
missing = [d for d in F.todo if d not in index]
print(len(missing), "labelled domains not fetched yet", flush=True)
with ThreadPoolExecutor(16) as ex:
    for d, rec in zip(missing, ex.map(F.fetch, missing)):
        rec.update(F.todo[d]); index[d] = rec
json.dump(index, open(idx_path, 'w', encoding='utf-8'))
print("done;", sum(1 for d in missing if index[d].get('pages')), "with a home page", flush=True)
