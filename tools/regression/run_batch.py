"""Crawler run over a batch of sites from a HYIP label CSV (vm-results/hyip-yes-*.csv or confirmed-*.csv).

Runs crawler_script.py --url on the first 30 sites whose HYIP column starts with YES (one after
another, each in crawler-test/runs-<CSV date>/<domain>/), shows the crawler's output live, reads
the outcome the same way run_regression.py does, and writes summary.csv in the run folder after
every site, so a stopped run keeps what it finished and --resume carries on from there.

Each site also gets the crawler's HYIP verdict (A = HYIP, B = other scam, none = unreachable),
family and reasons. --all-rows takes every row of the CSV, labelled or not, instead of the YES
rows. When the CSV has labels, the report compares the verdicts with them (A against YES..., B
against NO...; blank, DEAD and MAYBE rows are not compared). --classify-only has the crawler read
the start page and the sign-up search's pages and stop at the sign-up form, so a labelled CSV can
be checked without making accounts.

    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv            # first 30 YES sites
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --resume   # carry on after a stop
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --list     # show the 30 sites, run nothing
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --report   # results of the finished run
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --only rakizfx.com,fxsh6.com
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --limit 0  # every YES site
    python run_batch.py ..\\..\\vm-results\\hyip-yes-2026-10-28.csv --skip 30 --limit 0 --out ..\\runs-2026-10-28-rest
    python run_batch.py ..\\..\\vm-results\\confirmed-2026-10-29.csv --all-rows --limit 0 --out ..\\runs-2026-10-29-classify
    python run_batch.py ..\\..\\vm-results\\confirmed-2026-10-29.csv --all-rows --limit 0 --classify-only --out ..\\runs-2026-10-29-classify-only
    python run_batch.py ..\\..\\vm-results\\confirmed-2026-10-30.csv --all-rows --rows 101-173 --limit 0 --label-column "Crawler Label"

--rows takes spreadsheet row numbers (the header is row 1). --label-column writes each site's verdict
into that column of the CSV after every site, in the HYIP column's form: YES(<reason>) or
NO(<family>: <reason>), UNREACHABLE(<error>) for a site that never loaded. With --report it writes
them from the run folder's summary.csv without running anything.
"""
import argparse
import codecs
import collections
import csv
import datetime
import io
import os
import re
import shutil
import sys

from run_regression import HERE, LEVELS, outcome, run_site, verdict

FIELDS = ['domain', 'level', 'wallet_count', 'wallets', 'reason', 'seconds', 'label', 'verdict', 'family', 'verdict_reasons']


def load_batch(path, all_rows=False, row_range=None):
    """[(domain, label)] for the YES rows of a label CSV (every row with all_rows), in file order, without repeats.
    row_range=(first, last) keeps only those spreadsheet rows (the header is row 1)."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    sites, seen = [], set()
    for n, r in enumerate(rows, start=2):
        if row_range and not row_range[0] <= n <= row_range[1]:
            continue
        domain, label = (r.get('domain') or '').strip(), (r.get('HYIP') or '').strip()
        if domain and (all_rows or label.upper().startswith('YES')) and domain not in seen:
            seen.add(domain)
            sites.append((domain, label))
    return sites


def short_reason(reasons, parts=2, limit=160):
    """The deciding rule and its first piece of evidence from a verdict's reasons, without the
    page each feature was seen on ("(home)", "(+0.8, research L)"); summary.csv keeps them."""
    pieces = [p.strip() for p in (reasons or '').split('; ') if p.strip() and p.strip() != 'no hard rule applies']
    if pieces and pieces[0].startswith('score '):
        # No hard rule: name the features that weighed most, e.g. "score +4.6 from a login link, live-chat widget".
        feats = [re.sub(r"\s*\([+-]\d[^()]*\)$", '', p) for p in pieces if re.search(r"\([+-]\d", p)][:2]
        notes = [p for p in pieces[1:] if p.startswith('no ')]
        return ('; '.join([f"{pieces[0].split(' over ')[0]} from {', '.join(feats) or 'no scored features'}"] + notes))[:limit]
    pieces = [re.sub(r"\s*\([^()]*\)$", '', p) for p in pieces[:parts]]
    return ('; '.join(pieces) or 'no reason given')[:limit]


def label_text(row):
    """What goes in the label CSV's crawler column for one summary row, in the HYIP column's form:
    YES(<reason>) or NO(<family>: <reason>); UNREACHABLE(<error>) when the site never loaded."""
    why = short_reason(row.get('verdict_reasons'))
    if row.get('verdict') == 'A':
        return f"YES({why})"
    if row.get('verdict') == 'B':
        return f"NO({row.get('family') or 'other scam'}: {why})"
    if row.get('verdict') == 'none':
        return f"UNREACHABLE({short_reason(row.get('verdict_reasons'), parts=1)})"
    return 'NO VERDICT(crawler error, no verdict line)'


def write_labels(path, column, labels):
    """Put labels {domain: text} in `column` of the label CSV, keeping its BOM, line endings and other cells.
    False (with a message) when the file cannot be written, e.g. while it is open in Excel."""
    with open(path, 'rb') as f:
        raw = f.read()
    text = raw.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in text else '\n'
    table = list(csv.reader(io.StringIO(text, newline='')))
    header = table[0]
    if column not in header:
        header.append(column)
    col, dom = header.index(column), header.index('domain')
    for r in table[1:]:
        if len(r) > dom and r[dom].strip() in labels:
            r.extend([''] * (len(header) - len(r)))
            r[col] = labels[r[dom].strip()]
    out = io.StringIO(newline='')
    csv.writer(out, lineterminator=newline).writerows(table)
    try:
        with open(path, 'w', encoding='utf-8-sig' if raw.startswith(codecs.BOM_UTF8) else 'utf-8', newline='') as f:
            f.write(out.getvalue())
        return True
    except OSError as e:
        print(f"!!! could not write {column!r} into {path} ({e}); it is retried after the next site", flush=True)
        return False


def label_side(label):
    """'A' for a YES... label, 'B' for a NO... label, None for blank, DEAD, MAYBE and the rest."""
    label = label.strip().upper()
    return 'A' if label.startswith('YES') else 'B' if label.startswith('NO') else None


def read_summary(path):
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_summary(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Crawler run over the YES sites of a hyip-yes CSV")
    parser.add_argument('csv', help="label CSV with domain and HYIP columns")
    parser.add_argument('--all-rows', action='store_true', help="every row of the CSV, labelled or not, not just YES rows")
    parser.add_argument('--classify-only', action='store_true', help="HYIP verdict only: the crawler stops at the sign-up form")
    parser.add_argument('--out', help="run folder (default crawler-test/runs-<date in the CSV name>)")
    parser.add_argument('--skip', type=int, default=0, help="leave out the first N YES sites (e.g. 30 for the rest after a first batch)")
    parser.add_argument('--limit', type=int, default=30, help="only the first N YES sites after --skip (default 30, 0 for all)")
    parser.add_argument('--only', help="comma-separated domains from the CSV")
    parser.add_argument('--resume', action='store_true', help="skip domains already in the folder's summary.csv")
    parser.add_argument('--show-browser', action='store_true', help="run Chrome with a visible window")
    parser.add_argument('--list', action='store_true', help="list the chosen sites and exit")
    parser.add_argument('--report', action='store_true', help="print the run folder's summary.csv and exit")
    parser.add_argument('--rows', help="only these spreadsheet rows of the CSV, e.g. 101-173 (the header is row 1)")
    parser.add_argument('--label-column', help="write each site's verdict into this column of the CSV (made if missing; "
                                               "the CSV is backed up first)")
    args = parser.parse_args()

    row_range = None
    if args.rows:
        m = re.fullmatch(r"(\d+)-(\d+)", args.rows.strip())
        if not m:
            sys.exit("--rows takes first-last, e.g. 101-173")
        row_range = (int(m.group(1)), int(m.group(2)))
    sites = load_batch(args.csv, args.all_rows, row_range)
    if args.only:
        wanted = {d.strip() for d in args.only.split(',') if d.strip()}
        unknown = wanted - {d for d, _ in sites}
        if unknown:
            sys.exit(f"Not {'a row' if args.all_rows else 'a YES row'} in {args.csv}: {', '.join(sorted(unknown))}")
        sites = [s for s in sites if s[0] in wanted]
    sites = sites[args.skip:]
    if args.limit:
        sites = sites[:args.limit]

    if args.out:
        out = os.path.abspath(args.out)
    else:
        date = re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(args.csv))
        out = os.path.join(HERE, '..', 'runs-' + (date.group(0) if date else 'batch'))
    out = os.path.normpath(out)
    summary_file = os.path.join(out, 'summary.csv')

    if args.report:
        rows = read_summary(summary_file)
        if not rows:
            sys.exit(f"No results in {summary_file}")
        report(rows, summary_file)
        if args.label_column and write_labels(args.csv, args.label_column, {r['domain']: label_text(r) for r in rows}):
            print(f"\nWrote {len(rows)} {args.label_column!r} labels from {summary_file} into {args.csv}")
        return
    rows = read_summary(summary_file) if args.resume else []
    done = {r['domain'] for r in rows}
    todo = [s for s in sites if s[0] not in done]
    if args.list:
        for i, (domain, label) in enumerate(todo, start=1):
            print(f"{i:3}  {domain}" + (f"  [{label}]" if label != 'YES' else ''))
        print(f"\n{len(todo)} sites to run into {out}" + (f" ({len(done)} already in summary.csv)" if done else ''))
        return
    if not args.resume and os.path.exists(summary_file):
        sys.exit(f"{summary_file} exists; use --resume to carry on, or --out for a new folder")

    os.makedirs(out, exist_ok=True)
    labels = {}
    if args.label_column:
        slug = re.sub(r'\W+', '-', args.label_column.lower()).strip('-')
        backup = f"{args.csv}.bak-{datetime.date.today()}-before-{slug}"
        if not os.path.exists(backup):
            shutil.copy2(args.csv, backup)
            print(f"Backed up {args.csv} to {backup}")
        labels = {r['domain']: label_text(r) for r in rows}  # sites already done (--resume)
    try:
        for i, (domain, label) in enumerate(todo, start=1):
            url = f"https://{domain}/"
            print(f"\n===== [{i}/{len(todo)}] {domain} ({url}) =====", flush=True)
            lines, seconds = run_site(domain, url, args.show_browser, runs=out,
                                      extra_args=['--classify-only'] if args.classify_only else ())
            level, wallets, reason = outcome(lines)
            v, family, why = verdict(lines)
            rows.append({'domain': domain, 'level': level, 'wallet_count': len(wallets), 'wallets': ' '.join(wallets),
                         'reason': reason, 'seconds': int(seconds), 'label': label,
                         'verdict': v, 'family': family, 'verdict_reasons': why})
            write_summary(summary_file, rows)
            if args.label_column:
                labels[domain] = label_text(rows[-1])
                write_labels(args.csv, args.label_column, labels)
            print(f"----- {domain}: {level}, {len(wallets)} wallet-like strings, {int(seconds)} s" + (f" ({reason})" if reason else '')
                  + (f"; HYIP verdict {v}" + (f" ({family})" if v == 'B' else '') if v else ''))
    except KeyboardInterrupt:
        print("\nStopped; summary.csv has the sites finished so far (rerun with --resume to carry on).")

    report(rows, summary_file)


def report(rows, summary_file):
    """Every site, best level first, then the count per level, the wallets found and the verdicts
    against the CSV's labels."""
    print(f"\n{'domain':45} {'level':13} {'wallets':>7} {'secs':>5} {'verdict':7}  reason")
    for r in sorted(rows, key=lambda r: -LEVELS.index(r['level'])):
        print(f"{r['domain'][:45]:45} {r['level']:13} {r['wallet_count']:>7} {r['seconds']:>5} {r.get('verdict') or '-':7}  {r['reason']}")
    counts = collections.Counter(r['level'] for r in rows)
    print(f"\n{len(rows)} sites in {summary_file}")
    for level in reversed(LEVELS):
        if counts[level]:
            print(f"  {level:13} {counts[level]}")
    for r in rows:
        if r['level'] == 'WALLET':
            print(f"  {r['domain']}: {r['wallets']}")
    compare_labels(rows)


def compare_labels(rows):
    """Verdict counts, and when the CSV has labels: A against YES..., B against NO... and the disagreements."""
    verdicts = collections.Counter(r.get('verdict') or '' for r in rows)
    if not any(verdicts[k] for k in ('A', 'B', 'none')):
        return
    print(f"\nHYIP verdicts: A {verdicts['A']}   B {verdicts['B']}   none (unreachable) {verdicts['none']}"
          + (f"   no verdict line {verdicts['']}" if verdicts[''] else ''))
    compared = [r for r in rows if label_side(r.get('label') or '') and r.get('verdict') in ('A', 'B')]
    if not compared:
        return
    m = collections.Counter((label_side(r['label']), r['verdict']) for r in compared)
    left_out = collections.Counter('unreachable' if r.get('verdict') == 'none' else (r.get('label') or 'blank').split('(')[0].strip()
                                   for r in rows if r not in compared)
    print(f"Against the CSV's labels ({len(compared)} sites; left out: "
          + ', '.join(f"{k} {n}" for k, n in sorted(left_out.items())) + ")")
    print(f"  label YES: verdict A {m[('A', 'A')]:3}  B {m[('A', 'B')]:3}")
    print(f"  label NO : verdict A {m[('B', 'A')]:3}  B {m[('B', 'B')]:3}")
    print(f"  agreement {(m[('A', 'A')] + m[('B', 'B')]) / len(compared):.0%}")
    for r in compared:
        if label_side(r['label']) != r['verdict']:
            print(f"  disagree: {r['domain']} label {r['label']!r}, verdict {r['verdict']}"
                  + (f" ({r['family']})" if r['verdict'] == 'B' else '') + f", {r['level']}: {(r.get('verdict_reasons') or '')[:160]}")
    stopped = [r for r in rows if r['level'] == 'CLASSIFIED_B']
    if stopped:
        print(f"  stopped before sign-up as B: {len(stopped)} ({sum(1 for r in stopped if label_side(r.get('label') or '') == 'A')} of them labelled YES)")


if __name__ == '__main__':
    main()
