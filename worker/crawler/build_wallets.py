#!/usr/bin/env python3
"""
build_wallets.py -- turns iocs_searcher.sh output into one row per (domain, wallet).

iocs_searcher.sh writes iocs_results as blocks of

    Searching IOCs in <path to a saved page>
    bitcoin	bc1q...
    phoneNumber	+44...

This keeps the cryptocurrency addresses, ties each one to the domain the crawler was
given, and writes wallets.csv next to this script:

    domain           the site as feeder.py gave it to the crawler (no scheme)
    address          the wallet address
    ioc_type         iocsearcher's type: a format, not an asset. "tron" is TRX or
                     USDT-TRC20; "ethereum" is ETH, or USDT/USDC on any EVM chain.
    suggested_type   the ScamBusters form option the label points to, or blank if
                     it doesn't say. A guess from the label; check before submitting.
    label            the page text just before the address, e.g. "USDT_TRC20 Wallet
                     Address:", which is what settles the asset and network
    pages_seen       how many saved pages show the address
    page             the saved page the label came from (a deposit page if any)
    screenshot       that page's screenshot, if one was saved under a matching name
    domain_source    source_url.txt (written by the crawler), folder_name (parsed from
                     the folder name and found in urls.txt), or folder_name_unmatched
    first_seen_utc   when the earliest page showing the address was saved
    vm               this host

crimson-crawler.service runs it after iocs_searcher.sh on every pass; it rebuilds the
whole file each time, so it is safe to run by hand too.
"""
import csv
import datetime
import os
import re
import socket
import sys
from collections import defaultdict

from bs4 import BeautifulSoup, NavigableString

HERE = os.path.dirname(os.path.abspath(__file__))
IOCS_RESULTS = os.path.join(HERE, "iocs_results")
URLS_FILE = os.path.join(HERE, "urls.txt")
OUT = os.path.join(HERE, "wallets.csv")

WALLET_TYPES = {"bitcoin", "bitcoincash", "ethereum", "litecoin", "tron", "ripple",
                "dogecoin", "monero", "cardano", "zcash", "dashcoin", "tezos", "webmoney"}
LABEL_CHARS = 150

# Form options by iocsearcher type, decided by words in the label. Order matters: the
# first rule whose pattern matches wins, and a rule with no pattern is the fallback
# when the type itself names one asset.
FORM_RULES = {
    "bitcoin": [(None, "Bitcoin (BTC) - Bitcoin (btc)")],
    "bitcoincash": [(None, "Bitcoin Cash (BCH) - Bitcoin Cash (bch)")],
    "litecoin": [(None, "Litecoin (LTC) - Litecoin (ltc)")],
    "dogecoin": [(None, "Dogecoin (DOGE) - Dogecoin (doge)")],
    "cardano": [(None, "Cardano (ADA) - Cardano (ada)")],
    "dashcoin": [(None, "Dash (DASH) - Dash (dash)")],
    "ripple": [(None, "Ripple (XRP) - XRP Ledger (xrp)")],
    "tron": [
        (r"usdt|tether|trc-?20", "Tether (USDT) - Tron TRC-20 (trx)"),
        (r"\btrx\b|\btron\b", "Tron (TRX) - Tron (trx)"),
    ],
    "ethereum": [
        (r"(usdt|tether).{0,25}(bep-?20|bsc|bnb|binance)|(bep-?20|bsc).{0,25}(usdt|tether)", "Tether (USDT) - BNB Smart Chain BEP-20 (bsc)"),
        (r"(usdt|tether).{0,25}(polygon|matic)", "Tether (USDT) - Polygon PoS (matic)"),
        (r"(usdt|tether).{0,25}arbitrum", "Tether (USDT) - Arbitrum One (arb)"),
        (r"(usdt|tether).{0,25}optimism", "Tether (USDT) - Optimism (op)"),
        (r"(usdt|tether).{0,25}(avax|avalanche)", "Tether (USDT) - Avalanche C-Chain (avax)"),
        (r"usdt|tether|erc-?20", "Tether (USDT) - Ethereum ERC-20 (eth)"),
        (r"usdc|usd coin", "USD Coin (USDC) - Ethereum ERC-20 (eth)"),
        (r"\bbnb\b|bep-?20|\bbsc\b|binance smart", "BNB (BNB) - BNB Smart Chain BEP-20 (bsc)"),
        (r"\bmatic\b|polygon", "Polygon (MATIC) - Polygon PoS (matic)"),
        (r"\beth\b|ethereum|ether\b", "Ethereum (ETH) - Ethereum (eth)"),
    ],
}


def suggest_type(ioc_type, label):
    text = label.lower()
    for pattern, option in FORM_RULES.get(ioc_type, []):
        if pattern is None or re.search(pattern, text):
            return option
    return ""


def parse_iocs_results(path):
    """Yield (page_path, ioc_type, address) for every wallet line in iocs_results."""
    page = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("Searching IOCs in "):
                page = line[len("Searching IOCs in "):].strip()
                continue
            parts = line.split("\t")
            if page and len(parts) == 2 and parts[0] in WALLET_TYPES:
                yield page, parts[0], parts[1].strip()


def load_urls():
    """Bare host (lowercase, no www.) -> the urls.txt entry it came from."""
    hosts = {}
    if os.path.exists(URLS_FILE):
        for line in open(URLS_FILE, encoding="utf-8", errors="replace"):
            url = line.strip()
            host = re.sub(r"^[a-z]+://", "", url, flags=re.I).split("/")[0].lower()
            if host:
                hosts.setdefault(host[4:] if host.startswith("www.") else host, url)
    return hosts


def bare_domain(url):
    return re.sub(r"^[a-z]+://", "", url.strip(), flags=re.I).split("/")[0].lower()


def site_domain(folder, urls_by_host):
    """(domain, domain_source) for a crawler site folder."""
    src = os.path.join(HERE, folder, "source_url.txt")
    if os.path.exists(src):
        url = open(src, encoding="utf-8").read().strip()
        if url:
            return bare_domain(url), "source_url.txt"
    # Folder = login URL with "://" and "/" replaced by "_": https_<host>_<path>
    host = re.sub(r"^https?_", "", folder).split("_")[0].lower()
    key = host[4:] if host.startswith("www.") else host
    if key in urls_by_host:
        return bare_domain(urls_by_host[key]), "folder_name"
    return host, "folder_name_unmatched"


def label_before(html, address):
    """The visible text just before the address (in text or in an attribute such as
    an <input value=...>), whitespace collapsed, at most LABEL_CHARS characters."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    needle = address.lower()
    node = soup.find(string=lambda s: s and needle in s.lower())
    parts = []
    if node is not None:
        parts.append(str(node)[:str(node).lower().index(needle)])
    else:
        def has_attr_value(tag):
            for v in tag.attrs.values():
                v = " ".join(v) if isinstance(v, list) else str(v)
                if needle in v.lower():
                    return True
            return False
        node = soup.find(has_attr_value)
        if node is None:
            return ""
    for s in node.find_all_previous(string=True):
        if not isinstance(s, NavigableString):
            continue
        t = " ".join(s.split())
        if t:
            parts.append(t)
        if sum(len(p) for p in parts) >= LABEL_CHARS:
            break
    text = " ".join(" ".join(p.split()) for p in reversed(parts) if p.strip())
    return text[-LABEL_CHARS:].strip()


def screenshot_for(page_path):
    """The screenshot the crawler saved for a page, relative to HERE, or ''."""
    folder, name = os.path.split(page_path)
    shot, note = "", ""
    if name.startswith("deposit_page_"):
        shot = os.path.join(folder, "deposit_screenshot_" + name[len("deposit_page_"):-len(".html")] + ".png")
        if not os.path.exists(shot):
            # Crawls before Oct 14 2026 saved deposit_screenshot_<n>.png, a name shared
            # by every deposit page of the site, so it may show another deposit page.
            m = re.search(r"_(\d+)\.html$", name)
            shot = os.path.join(folder, f"deposit_screenshot_{m.group(1)}.png") if m else ""
            note = " (name shared by all deposit pages)"
    elif name.startswith("page_"):
        shot = os.path.join(folder, "screenshot_" + name[len("page_"):-len(".html")] + ".png")
    if not shot or not os.path.exists(shot):
        return ""
    return os.path.relpath(shot, HERE) + note


def main():
    if not os.path.exists(IOCS_RESULTS):
        print(f"[wallets] no {IOCS_RESULTS} yet; nothing to build.")
        return
    urls_by_host = load_urls()
    # (domain, address) -> details
    rows = {}
    pages = defaultdict(set)
    for page, ioc_type, address in parse_iocs_results(IOCS_RESULTS):
        if not os.path.exists(page):
            continue
        rel = os.path.relpath(page, HERE)
        folder = rel.split(os.sep)[0]
        domain, source = site_domain(folder, urls_by_host) if os.sep in rel else ("", "no_site_folder")
        key = (domain, address)
        pages[key].add(page)
        is_deposit = os.path.basename(page).startswith("deposit_page_")
        current = rows.get(key)
        # Prefer a deposit page's label: that is where sites say what to send.
        if current is None or (is_deposit and not current["is_deposit"]):
            try:
                html = open(page, encoding="utf-8", errors="replace").read()
                label = label_before(html, address)
            except Exception as e:
                print(f"[wallets] WARN: could not read label from {page}: {e}", file=sys.stderr)
                label = ""
            rows[key] = {"domain": domain, "address": address, "ioc_type": ioc_type,
                         "label": label, "page": page, "domain_source": source,
                         "is_deposit": is_deposit}

    vm = socket.gethostname()
    out_rows = []
    for key, r in rows.items():
        first = min(os.path.getmtime(p) for p in pages[key])
        out_rows.append({
            "domain": r["domain"],
            "address": r["address"],
            "ioc_type": r["ioc_type"],
            "suggested_type": suggest_type(r["ioc_type"], r["label"]),
            "label": r["label"],
            "pages_seen": len(pages[key]),
            "page": os.path.relpath(r["page"], HERE),
            "screenshot": screenshot_for(r["page"]),
            "domain_source": r["domain_source"],
            "first_seen_utc": datetime.datetime.fromtimestamp(first, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "vm": vm,
        })
    out_rows.sort(key=lambda r: (r["domain"], r["ioc_type"], r["address"]))

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows else
                           ["domain", "address", "ioc_type", "suggested_type", "label", "pages_seen",
                            "page", "screenshot", "domain_source", "first_seen_utc", "vm"])
        w.writeheader()
        w.writerows(out_rows)
    os.replace(tmp, OUT)
    print(f"[wallets] {len(out_rows)} wallet(s) from {len({r['domain'] for r in out_rows})} site(s) -> {OUT}")


if __name__ == "__main__":
    main()
