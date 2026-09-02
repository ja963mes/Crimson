import wordninja
import tldextract
import os
from bs4 import BeautifulSoup, Comment

# ---------------------------------------------------------------------------
# BUG 1 FIX -- segmentation.
#
# was: 'improved_crypto_words.txt.gz'
#
# That file is Table 11 from the paper (101 keyword terms), not a dictionary.
# wordninja.LanguageModel() charges 9e999 for any substring outside its model,
# so with a 101-word vocabulary and no single-character entries, every domain
# that could not be tiled entirely from those words had all DP paths cost
# infinity. min() tie-breaks on length -> k=1 -> the domain shattered into
# characters, and no keyword could match anything:
#
#     pensioninvestment -> ['p','e','n','s','i','o','n','i','n','v',...]
#
# merged_crypto_words.txt.gz is Table 11's terms at ranks 0..101 (cheapest, so
# still preferred by the segmenter) followed by the stock ~126k English list.
# Rebuild it with build_model.py.
# ---------------------------------------------------------------------------
WORD_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'merged_crypto_words.txt.gz')

# ---------------------------------------------------------------------------
# Table 11, URL-filter column -- all 35 terms.
#
# Corrected against the paper:
#   "digial" -> "digital"   (typo; no domain could ever match it)
#   "minder" -> "miner"     (typo; same)
#   added "capitals", "funding"  (in the table, absent from the code)
#   "trade" was listed twice; a set dedupes it
# ---------------------------------------------------------------------------
keyword_in_url = {"crypto", "fx", "earn", "deposit", "trade", "capital", "invest",
"global", "bit", "mining", "ltd", "finance", "miner", "trust", "profit", "asset",
"cardano", "funding", "capitals", "fund", "limited", "chain", "digital", "btc",
"assets", "wealth", "coin", "option", "prime", "bitcoin", "exchange", "money",
"eth", "ethereum", "cryptocurrency"}

# ---------------------------------------------------------------------------
# Inflected forms of the SHORT Table 11 terms.
#
# MIN_PREFIX_LEN (below) is 6, so prefix matching cannot reach inflections of
# "trade" (5), "fund" (4), "coin" (4) or "miner" (5). Lowering the threshold is
# not the answer -- at 4-5 chars it starts firing on bitter/coincidence/
# fundamental/earnest. Enumerating the forms keeps exact-token precision.
#
# Measured against the 43,572 confirmed scam domains in data/data.json: without
# these, 212 domains that the ORIGINAL filter caught are missed, because the
# merged model now segments them correctly ("traderslite" -> traders + lite)
# where the old 102-word model happened to leave a bare "trade" token behind.
#
#     trader  101    traders  35    trading  7    coins  5
#     funds    70    miners    3    funded   1
#
# Recovery, by form, over those 212 domains.
# ---------------------------------------------------------------------------
keyword_in_url |= {"trader", "traders", "trading", "trades", "traded",
                   "funds", "funded", "coins", "miners"}

domain_whitelist = { # Update as needed!

}

# ---------------------------------------------------------------------------
# BUG 2 FIX -- matching.
#
# The original test was exact token membership:
#
#     if url_keyword in domain_name_splits
#
# Even with segmentation repaired, 'pensioninvestment' splits to
# ['pension','investment'] and 'invest' in {'pension','investment'} is False.
# The keyword list is fine; exact matching just cannot see a keyword inside an
# inflected form.
#
# So a token also matches when it STARTS WITH a keyword -- but only for
# keywords of at least MIN_PREFIX_LEN characters. Without that guard the short
# ordinary-English entries in Table 11 fire constantly:
#
#     'bit'  -> bitter, arbitrary       'coin' -> coincidence
#     'fund' -> fundamental             'earn' -> earnest
#
# At 6 chars, 'invest' still catches investment/investing/investors while
# bit/coin/fund/earn/chain/prime stay exact-match only.
# ---------------------------------------------------------------------------
MIN_PREFIX_LEN = 6

lm_ninja = None
if(lm_ninja is None):
    lm_ninja = wordninja.LanguageModel(WORD_MODEL_DIR)


def _token_matches(token, keyword):
    if token == keyword:
        return True
    return len(keyword) >= MIN_PREFIX_LEN and token.startswith(keyword)


def match_domain_name_with_keywords(domain_name):
    for domain_kw in domain_whitelist:
        if(domain_name.endswith(domain_kw)):
            return False
    extracted = tldextract.extract(domain_name)
    domain_without_tld = extracted.domain
    if extracted.subdomain:
        domain_without_tld = extracted.subdomain + '.' + domain_without_tld
    domain_name_splits = set(lm_ninja.split(domain_without_tld))
    for url_keyword in keyword_in_url:
        for token in domain_name_splits:
            if _token_matches(token, url_keyword):
                return True
    return False
