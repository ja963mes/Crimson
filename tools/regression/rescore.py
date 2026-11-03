"""Re-score a finished run's HYIP verdicts offline with the current hyip_classifier.py.

Every site folder of a run holds hyip_evidence.json (the features the crawl saw and where). This
rebuilds the evidence from those files, classifies it again, and compares the new verdicts with the
ones the run logged and, when a label CSV is given, with its HYIP column (A against YES..., B against
NO...). Nothing is crawled and no file is changed.

    python rescore.py ..\\runs-2026-10-30-rows101-173 --labels ..\\..\\vm-results\\confirmed-2026-10-30.csv
"""
import argparse
import collections
import csv
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'authentication_crawling'))
import hyip_classifier as hc  # noqa: E402
from run_batch import label_side  # noqa: E402


def evidence_from(path):
    """SiteEvidence rebuilt from one hyip_evidence.json, and the verdict the run logged."""
    data = json.load(open(path, encoding='utf-8'))
    ev = hc.SiteEvidence(data['url'])
    ev.features = dict(data['features'])
    ev.onsite = set(ev.features) - set(data.get('offsite_features') or [])
    ev.pages = list(data.get('pages_read') or [])
    ev.read = data.get('read', len(ev.pages) if data['verdict']['verdict'] != 'none' else 0)
    ev.unreachable, ev.parked = data.get('unreachable'), data.get('parked')
    ev.title = data.get('title') or ''
    return ev, data['verdict']


def main():
    parser = argparse.ArgumentParser(description="Re-score a run's HYIP verdicts from its hyip_evidence.json files")
    parser.add_argument('run', help="run folder (one sub-folder per site)")
    parser.add_argument('--labels', help="label CSV with domain and HYIP columns, to compare against")
    args = parser.parse_args()

    labels = {}
    if args.labels:
        with open(args.labels, newline='', encoding='utf-8-sig') as f:
            labels = {(r.get('domain') or '').strip(): (r.get('HYIP') or '').strip() for r in csv.DictReader(f)}
    rows = []
    for path in sorted(glob.glob(os.path.join(args.run, '*', 'hyip_evidence.json'))):
        domain = os.path.basename(os.path.dirname(path))
        ev, old = evidence_from(path)
        new = hc.classify(ev)
        rows.append((domain, labels.get(domain, ''), old, new))

    changed = [r for r in rows if r[2]['verdict'] != r[3]['verdict']]
    print(f"{len(rows)} sites re-scored; {len(changed)} verdicts changed")
    for domain, label, old, new in changed:
        print(f"  {domain[:42]:42} {old['verdict']} -> {new['verdict']}  [label {label or '-'}]  {'; '.join(new['reasons'])[:150]}")
    if not labels:
        return
    for name, i in (('run', 2), ('re-scored', 3)):
        m = collections.Counter((label_side(r[1]), r[i]['verdict']) for r in rows if label_side(r[1]) and r[i]['verdict'] in ('A', 'B'))
        n = sum(m.values())
        yes_a, no_b = m[('A', 'A')], m[('B', 'B')]
        said_a, said_b = m[('A', 'A')] + m[('B', 'A')], m[('A', 'B')] + m[('B', 'B')]
        print(f"\n{name}: {n} sites with a YES/NO label and a verdict")
        print(f"  label YES: A {yes_a:3}  B {m[('A', 'B')]:3}      label NO: A {m[('B', 'A')]:3}  B {no_b:3}")
        print(f"  agreement {(yes_a + no_b) / n:.1%}   YES found {yes_a}/{yes_a + m[('A', 'B')]}   "
              f"NO found {no_b}/{no_b + m[('B', 'A')]}   YES precision {yes_a}/{said_a}   NO precision {no_b}/{said_b}")
    wrong = [r for r in rows if label_side(r[1]) and r[3]['verdict'] in ('A', 'B') and label_side(r[1]) != r[3]['verdict']]
    if wrong:
        print("\nstill disagreeing after re-scoring:")
        for domain, label, old, new in wrong:
            print(f"  {domain[:42]:42} label {label[:24]:24} verdict {new['verdict']} {new['step']:8} {'; '.join(new['reasons'])[:120]}")


if __name__ == '__main__':
    main()
