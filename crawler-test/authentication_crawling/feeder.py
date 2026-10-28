#!/usr/bin/env python3
"""
feeder.py -- bridges the Crimson detection pipeline to the authenticated crawler.

recv.py writes every CONFIRMED crypto-scam (LLM verdict is_scam and confidence > 80)
as a JSON line to a results log:

    HH:MM:SS:\t{"url": "<bare-domain>", "title": ..., "is_scam": true, "confidence": 85, ...}

Those files live at:
  * on a worker node:  <src>/results/<SYSNO>/results.log.<YYMMDD>
  * on the master:     <src>/collected_results/results/<N>/results.log.<YYMMDD>   (union, via rsync)

This script harvests the "url" field from those records, normalizes each to a full
URL, de-duplicates against the domains already handed to the crawler, and APPENDS
the new ones to the crawler's input file (urls.txt).

IMPORTANT: it only ever appends -- it never rewrites or reorders urls.txt. That is
deliberate: crawler_script.py resumes from a single-URL checkpoint (crawled_urls.txt)
by scanning urls.txt for that exact line, which only works if line order is stable.

Typical use (standalone or on a schedule, just before/alongside the crawler):

    python3 feeder.py                                  # defaults (see below)
    python3 feeder.py --results '/path/results/*/results.log.*'
    python3 feeder.py --dry-run                        # preview only, write nothing

The default results glob can also be set via the CRIMSON_RESULTS_GLOB env var.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Where confirmed-scam records are read FROM. Override with --results (repeatable)
# or the CRIMSON_RESULTS_GLOB env var. Default targets the master's aggregated set.
DEFAULT_RESULTS = os.environ.get(
    "CRIMSON_RESULTS_GLOB",
    "/home/ubuntu/crimson/collected_results/results/*/results.log.*",
)
# Where domains are written TO (kept next to this script so the crawler, which
# anchors the same paths to its own directory, always reads the same files).
DEFAULT_OUT = os.path.join(HERE, "urls.txt")
DEFAULT_STATE = os.path.join(HERE, "fed_domains.txt")

# Each results.log line is "HH:MM:SS:\t{...json...}"; grab the JSON object.
_JSON_RE = re.compile(r"\{.*\}")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def normalize_domain(url):
    """Reduce a stored url to a bare, lowercased host with no scheme/path.

    This is the de-dupe key, so 'https://Foo.com/x' and 'foo.com' collapse to
    the same entry and are never crawled twice.
    """
    u = (url or "").strip().lower()
    u = _SCHEME_RE.sub("", u)      # drop any scheme the record may carry
    u = u.split("/", 1)[0]          # drop any path/query
    return u.strip().strip(".")


def load_fed(state_path):
    """Return the set of domains already fed to the crawler (empty if first run)."""
    if not os.path.exists(state_path):
        return set()
    with open(state_path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def harvest(results_globs):
    """Yield unique bare domains found in every results.log matched by the globs.

    Malformed lines and unreadable files are skipped with a warning, so a single
    bad record never stops the run.
    """
    seen = set()
    matched_any = False
    for pattern in results_globs:
        for path in glob.glob(pattern):
            matched_any = True
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = _JSON_RE.search(line)
                        if not m:
                            continue
                        try:
                            rec = json.loads(m.group(0))
                        except json.JSONDecodeError:
                            continue
                        dom = normalize_domain(rec.get("url"))
                        if dom and dom not in seen:
                            seen.add(dom)
                            yield dom
            except OSError as e:
                print(f"[feeder] WARN: cannot read {path}: {e}", file=sys.stderr)
    if not matched_any:
        print(
            f"[feeder] WARN: no results.log files matched {results_globs}",
            file=sys.stderr,
        )


def main():
    ap = argparse.ArgumentParser(
        description="Feed confirmed-scam domains from the Crimson pipeline into the crawler's urls.txt"
    )
    ap.add_argument(
        "--results", action="append", metavar="GLOB",
        help=f"glob for results.log files (repeatable). default: {DEFAULT_RESULTS}",
    )
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"crawler input file to append to (default: {DEFAULT_OUT})")
    ap.add_argument("--state", default=DEFAULT_STATE,
                    help=f"dedupe state file (default: {DEFAULT_STATE})")
    ap.add_argument("--scheme", default="https", choices=["https", "http"],
                    help="scheme to prepend to each domain (default: https)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the domains that would be added, but write nothing")
    args = ap.parse_args()

    results_globs = args.results if args.results else [DEFAULT_RESULTS]
    fed = load_fed(args.state)

    new = []
    for dom in harvest(results_globs):
        if dom not in fed:
            new.append(dom)
            fed.add(dom)  # guard against duplicates within this same run

    if not new:
        print("[feeder] no new confirmed-scam domains to add.")
        return

    if args.dry_run:
        print(f"[feeder] DRY-RUN: would add {len(new)} new domain(s):")
        for d in new:
            print(f"    {args.scheme}://{d}")
        return

    with open(args.out, "a", encoding="utf-8") as out_f, \
            open(args.state, "a", encoding="utf-8") as state_f:
        for d in new:
            out_f.write(f"{args.scheme}://{d}\n")
            state_f.write(f"{d}\n")

    print(f"[feeder] added {len(new)} new domain(s) to {args.out}")


if __name__ == "__main__":
    main()
