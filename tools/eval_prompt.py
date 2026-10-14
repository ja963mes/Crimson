"""Score LLM prompt variants against hand-labelled pages, offline.

Each variant is the paper's prompt (PAPER_PROMPT in recv.py) plus additions after
it. Pages are classified by recv.py's own evaluate_ocr_with_ollama(), loaded from
its source so that importing recv.py (Selenium, RabbitMQ, sys.argv) is avoided and
the request, schema and confidence code under test are the production ones.

Labels come from vm-results/confirmed-scams.csv (HYIP column, YES/NO; MAYBE and
DEAD are skipped). Page text is the "text" field of the results.log.* files that
the broker collects (~/crimson/collected_results/results/<vm>/results.log.<YYMMDD>).
Pages in those files from --unlabeled-day that have no label are scored too, and
reported by yes-rate only.

    python eval_prompt.py --results DIR --labels ../vm-results/confirmed-scams.csv \
        [--model llama3.2:3b] [--variants base,e1,e1e2] [--out verdicts.csv]
"""
import argparse
import ast
import collections
import csv
import glob
import json
import math
import os
import re
import time

from curl_cffi import requests as curl_requests

RECV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recv.py")

# Additions appended after PAPER_PROMPT. A blank line separates them, so the
# paper's text is left exactly as it is, final punctuation included.
E1 = (
    "A cryptocurrency investment scam here means a high-yield investment program "
    "(HYIP): a site where visitors open an account and deposit cryptocurrency or "
    "money into the site's own investment plans, in return for promised high returns. "
    "Other cryptocurrency scams, such as wallet drainers, airdrops and giveaways, "
    "token presales, fund recovery services and casinos, are not HYIPs; respond ‘no’ "
    "for them."
)
E2 = (
    "Respond ‘yes’ only if the text itself promises returns on deposits, such as a "
    "percentage or multiple over a period (2% daily, 150% after 7 days), or lists "
    "investment plans with deposit amounts. A trading platform, broker, bank, "
    "prop-trading firm, course or software product that does not promise such "
    "returns is ‘no’."
)
E3 = (
    "Examples:\n"
    'Text: "Invest now for a guaranteed return of 10 percent in one month." '
    'Output: {"answer": "yes", "reason": "promises"}\n'
    'Text: "Trade forex, stocks and crypto CFDs with spreads from 0.1 pips. Open a '
    'live account or try a free demo." Output: {"answer": "no", "reason": "broker"}'
)
# "current" is recv.py's OLLAMA_SYSTEM_PROMPT as deployed; the rest are built here.
VARIANTS = {
    "current": None,
    "base": [],
    "e1": [E1],
    "e2": [E2],
    "e3": [E3],
    "e1e2": [E1, E2],
    "e1e2e3": [E1, E2, E3],
}


def load_recv():
    """The Ollama evaluator, the content filter and their constants, executed from
    recv.py's source."""
    tree = ast.parse(open(RECV, encoding="utf-8").read())
    wanted_funcs = {"answer_confidence", "validate_ollama_response", "evaluate_ocr_with_ollama",
                    "has_hyip_phrase", "has_hyip_marker", "content_exclusions", "table11_filter",
                    "content_filter"}
    wanted_names = {"invest_words", "coin_words", "context_words", "hyip_phrases", "hyip_markers",
                    "exclusion_phrases", "gambling_words"}
    prefixes = ("OLLAMA_", "PAPER_", "PROMPT_", "VERDICT_", "SCAM_")
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in wanted_funcs)
             or (isinstance(n, ast.Assign)
                 and all(isinstance(t, ast.Name) and (t.id.startswith(prefixes) or t.id in wanted_names)
                         for t in n.targets))]
    ns = {"re": re, "json": json, "math": math, "curl_requests": curl_requests,
          "log": lambda msg, *a, **k: print(f"  [recv.log] {msg}")}
    exec(compile(ast.Module(nodes, []), RECV, "exec"), ns)
    return ns


def load_pages(results_dir, labels_csv, unlabeled_day):
    records = collections.defaultdict(dict)  # url -> {(vm, day): record}
    for path in glob.glob(os.path.join(results_dir, "*", "results.log.*")):
        vm = os.path.basename(os.path.dirname(path))
        day = path.rsplit(".", 1)[1]
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line.split("\t", 1)[1])
            except (IndexError, ValueError):
                continue
            if rec.get("text"):
                records[rec["url"]][(vm, day)] = rec

    pages, labelled = [], set()
    for row in csv.DictReader(open(labels_csv, encoding="utf-8-sig")):
        label = row["HYIP"].strip().rstrip("'").upper()
        if label not in ("YES", "NO") or row["domain"] not in records:
            continue
        vm = re.search(r"VM(\d)", row["vm"])
        m, d, y = row["date"].split("/")
        key = (vm.group(1) if vm else "", f"{y[-2:]}{int(m):02d}{int(d):02d}")
        hits = records[row["domain"]]
        rec = hits.get(key) or next(iter(hits.values()))
        pages.append({"domain": row["domain"], "set": "labelled", "label": label, "text": rec["text"]})
        labelled.add(row["domain"])

    for url, hits in sorted(records.items()):
        rec = next((r for (vm, day), r in hits.items() if day == unlabeled_day), None)
        if rec and url not in labelled:
            pages.append({"domain": url, "set": unlabeled_day, "label": "?", "text": rec["text"]})
            labelled.add(url)
    return pages


def summarise(name, rows, min_conf):
    lab = [r for r in rows if r["set"] == "labelled"]
    tp = sum(r["answer"] == "yes" and r["label"] == "YES" for r in lab)
    fp = sum(r["answer"] == "yes" and r["label"] == "NO" for r in lab)
    fn = sum(r["answer"] != "yes" and r["label"] == "YES" for r in lab)
    filed = [r for r in lab if r["answer"] == "yes" and r["confidence"] >= min_conf]
    filed_tp = sum(r["label"] == "YES" for r in filed)
    escalated = sum(r["answer"] == "unsure" or r["confidence"] < min_conf for r in rows)
    unl = [r for r in rows if r["set"] != "labelled"]
    reasons = collections.Counter(r["reason"].lower() for r in rows).most_common(4)
    pct = lambda a, b: f"{100 * a / b:5.1f}" if b else "  n/a"
    print(f"{name:8} | P {pct(tp, tp + fp)} R {pct(tp, tp + fn)} (tp {tp:2} fp {fp:2} fn {fn:2})"
          f" | filed P {pct(filed_tp, len(filed))} R {pct(filed_tp, tp + fn)}"
          f" | escalated {escalated:3}/{len(rows)}"
          f" | unlabelled yes {sum(r['answer'] == 'yes' for r in unl):3}/{len(unl)}"
          f" | {', '.join(f'{k}:{v}' for k, v in reasons)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="directory holding <vm>/results.log.<YYMMDD>")
    ap.add_argument("--labels", required=True, help="confirmed-scams.csv")
    ap.add_argument("--model", default=None, help="Ollama model (default: recv.py's OLLAMA_MODEL)")
    # 127.0.0.1 rather than recv.py's localhost: on Windows, localhost tries IPv6
    # first and adds ~2s to every request.
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/chat")
    ap.add_argument("--variants", default=",".join(VARIANTS), help="comma-separated, from: " + ", ".join(VARIANTS))
    ap.add_argument("--unlabeled-day", default="261007", help="YYMMDD of unlabelled pages to include")
    ap.add_argument("--content-filter", action="store_true",
                    help="score only pages that pass recv.py's content_filter(), as in the pipeline")
    ap.add_argument("--out", default="eval_verdicts.csv", help="per-page verdicts")
    args = ap.parse_args()

    recv = load_recv()
    recv["OLLAMA_URL"] = args.ollama_url
    if args.model:
        recv["OLLAMA_MODEL"] = args.model
    pages = load_pages(args.results, args.labels, args.unlabeled_day)
    if args.content_filter:
        dropped = collections.Counter((p["set"], p["label"]) for p in pages if not recv["content_filter"](p["text"]))
        pages = [p for p in pages if recv["content_filter"](p["text"])]
        print(f"content_filter dropped: {dict(dropped)} (recall below is of the pages left)")
    counts = collections.Counter((p["set"], p["label"]) for p in pages)
    print(f"model {recv['OLLAMA_MODEL']}, pages: {dict(counts)}")

    out_rows = []
    for name in args.variants.split(","):
        if VARIANTS[name] is None:
            prompt = recv["OLLAMA_SYSTEM_PROMPT"]
        else:
            prompt = "\n\n".join([recv["PAPER_PROMPT"]] + VARIANTS[name])
        rows, start = [], time.time()
        for p in pages:
            v = recv["evaluate_ocr_with_ollama"](p["text"], p["domain"], "eval", system_prompt=prompt)
            rows.append({**{k: p[k] for k in ("domain", "set", "label")}, "variant": name,
                         "answer": v["answer"], "confidence": v["confidence"], "reason": v["reason"]})
        summarise(name, rows, recv["OLLAMA_MIN_CONFIDENCE"])
        print(f"{'':8}   {len(pages)} pages in {time.time() - start:.0f}s")
        out_rows += rows

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)
    print(f"per-page verdicts: {args.out}")


if __name__ == "__main__":
    main()
