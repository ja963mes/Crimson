import pika, sys, os
import pytz
from datetime import datetime, timezone, timedelta, date
import time
import utils.keyword_utils as kw_utils
import utils.screenshot
from curl_cffi import requests as curl_requests
import html2text
import pytesseract
import whois
import re
from PIL import Image
import signal
import json
import math
import subprocess
import socket
import logging
from logging.handlers import TimedRotatingFileHandler
from cachetools import LRUCache
from bs4 import BeautifulSoup
import iocsearcher
from iocsearcher.searcher import Searcher
from openai import OpenAI

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = 30
OPENAI_MAX_RETRIES = 2
_openai_client = None


QUEUE_IP = '130.245.32.119'
searcher = Searcher()
os.environ['OMP_THREAD_LIMIT'] = '1'
SYSNO = sys.argv[1]
SCREENNO = sys.argv[2]
newYorkTz = pytz.timezone("America/New_York")
unavailable_domains = 0
domains_checked = 0
less_words_domains = 0
ocr_domains = 0
selenium_obj = utils.screenshot.SeleniumScreenshot()
REMOTE_IP = QUEUE_IP
REMOTE_USER = 'ubuntu'
PRIVATE_KEY_PATH = '../../.ssh/id_ed25519'
visited_cache = LRUCache(maxsize=1000)

def ensure_directory_exists(directory_path):
    if not os.path.exists(directory_path):
        try:
            os.makedirs(directory_path)
        except OSError as e:
            log(f"Error creating directory {directory_path}: {e}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'errors.txt', 'a')

def sync(url_name, curr_date, local_file_path, remote_file_path):
    # Absolute path on the worker node (VM1/VM2/VM3)
    abs_local_path = f"/home/ubuntu/crimson/src/{local_file_path}"

    # Absolute path on the central node (the broker host, QUEUE_IP)
    abs_remote_path = f"/home/ubuntu/crimson/collected_results/{local_file_path}"

    # Absolute path to the SSH key
    abs_key_path = "/home/ubuntu/.ssh/id_ed25519"

    try:
        # --mkpath: a new instance's collected_results/data/<SYSNO>/check/ is two
        # levels deep, and rsync otherwise creates only the last one. The timeouts
        # stop an unreachable broker from blocking the worker (no alarm covers sync).
        subprocess.run([
            'rsync', '-avz', '--mkpath', '--timeout=60',
            '-e', f'ssh -i {abs_key_path} -o StrictHostKeyChecking=no -o ConnectTimeout=20',
            abs_local_path,
            f'{REMOTE_USER}@{REMOTE_IP}:{abs_remote_path}'
        ], check=True, stdout=subprocess.DEVNULL)
        print(f" [->] Successfully synced data for {url_name}")

    except Exception as e:
        log(f"{url_name},{e}", curr_date, 'rsync-failure.txt', 'a')
        print(f" [!] Sync failed for {url_name}: {e}")

def sanitize_text(raw_text):
    """Cleans text payloads and strips non-analytical garbage.

    Returns the FULL cleaned text. See the note above the return statement.
    """
    if not raw_text:
        return ""
    if isinstance(raw_text, bytes):
        raw_text = raw_text.decode('utf-8', 'replace')

    text = raw_text.lower()
    text = re.sub(r'[^\x00-\x7F]+', ' ', text)       # Vaporize glitchy Unicode artifacts
    text = re.sub(r'[^\w\s\.\,\!\?\$%]+', ' ', text)  # Keep critical numbers, letters, and financial punctuation ($ and %)
    text = re.sub(r'\b[a-z]\b', '', text)             # Eliminate single-letter word clutter
    text = re.sub(r'\s+', ' ', text).strip()          # Compress all whitespace blocks into single spaces
    # No length cap. The 5,000-character ceiling that used to be applied here was
    # removed deliberately.
    #
    # It existed to bound LLM token spend, but sanitize_text() runs BEFORE
    # content_filter(), so Table 11's keywords were being matched against a
    # truncated page rather than the whole one. Measured on marsses.com: 13,433
    # characters of DOM text, of which the filter saw 5,000 -- 63% of the page
    # discarded before a single keyword was tested. A page whose only invest* term
    # sits in a footer or an FAQ was being rejected on text that was never read.
    #
    # Accepted trade-off: the classifiers now receive the full page as well, so
    # per-page token cost scales with page size instead of being fixed. Watch
    # openai_usage.txt for spend, and note that CPU-only Ollama inference time
    # grows with input length -- the 120s SIGALRM in main() is the ceiling that
    # will complain first.
    return text

def extract_dom_text(html_content):
    """Extracts human-viewable text nodes directly from raw HTML code data."""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        # Drop completely hidden / execution-only code structures out of the DOM tree
        for element in soup(['script', 'style', 'noscript', 'canvas', 'meta', 'svg']):
            element.extract()
        return soup.get_text(separator=' ').strip()
    except Exception:
        return ""

# ---------------------------------------------------------------------------
# CONTENT FILTER -- restored from the published Crimson artifact.
#
# This replaces heuristic_pre_filter(), which was a later bolt-on written to
# conserve LLM API quota:
#
#     suspicious_triggers = ["connect wallet", "claim your", "airdrop",
#         "guaranteed return", "seed phrase", "recovery phrase",
#         "double your crypto", "presale live"]
#     return sum(1 for t in suspicious_triggers if t in clean_text) >= 2
#
# Those eight phrases describe wallet drainers and airdrop phishing. Crimson
# studies cryptocurrency INVESTMENT scams (HYIPs), which share almost no
# vocabulary with that threat class -- measured 0/8 on every known-scam page
# tested. The list is removed, not extended.
#
# What follows is the original filter from recv.py's OCR() in the published
# artifact, with the word lists filled in from Table 11 of the paper. The
# artifact ships them empty ("# Update as needed!"), which is why this slot
# was vacant for the bolt-on to occupy.
#
# The rule is unchanged: a page must contain at least one INVEST word AND at
# least one COIN word AND at least one CONTEXT word.
#
# Lists are module-level rather than rebuilt inside the function on every
# call, which is the only behavioural change from the original.
# ---------------------------------------------------------------------------

# Table 11, Content-Filter / Invest Words
#
# The paper lists exactly one term here, matched as an exact token. That misses
# every inflected form, and this column is the binding constraint of the whole
# filter -- coin has 58 terms and context has 23, so in practice a page fails
# content_filter() on the invest column alone.
#
# Measured over the 43,572 confirmed scam domains in data/data.json, bare
# "invest" accounts for only 46.5% of invest* occurrences:
#
#     invest       2177  46.5%      investor       50   1.1%
#     investment   1805  38.5%      investing      29   0.6%
#     investments   520  11.1%      investors      27   0.6%
#     invests        71   1.5%      invested        7   0.1%
#
# All eight forms are attested, so all eight are listed. Enumerating them
# keeps the original exact-token match (and its precision) rather than
# switching this column to prefix matching, which would fire on unrelated
# words that merely start with a listed term.
invest_words = ["invest", "invests", "invested", "investing",
                "investment", "investments", "investor", "investors"]

# Table 11, Content-Filter / Coin Words
coin_words = ["cryptocurrency", "crypto", "bitcoin", "ethereum", "cardano",
"ripple", "binance", "shiba", "inu", "dogecoin", "solana", "tether", "tron",
"polkadot", "eth", "btc", "xrp", "ada", "bnb", "shib", "doge", "sol", "usdt",
"trx", "dot", "algo", "litecoin", "chainlink", "uniswap", "pancakeswap",
"avalanche", "neo", "iota", "aave", "luna", "synthetix", "theta", "grt",
"1inch", "sushi", "matic", "btcusd", "usdbtc", "ethusd", "usdeth", "adausd",
"usdada", "xrpusd", "usdxrp", "bnbusd", "usdbnb", "shibusd", "usdshib",
"dogeusd", "usddoge", "solusd", "usdsol", "usdtusd", "usdusdt"]

# Table 11, Content-Filter / Context Words
context_words = ["deposit", "withdraw", "reward", "growth", "gain", "capital",
"potential", "wallet", "safe", "secure", "fund", "profit", "insurance",
"wealth", "send", "transfer", "sell", "buy", "trade", "asset", "client",
"solution", "funding"]


# --- HYIP phrases and exclusion words (added Oct 6, 2026) ---
#
# The three lists above pass HYIPs (62 of the 63 in the Oct 1-6 hand-labelled
# sample) but also 79 of the 86 confirmed domains labelled as not HYIPs. Both
# lists below were measured on that sample, matched against sanitize_text()
# output (lowercase, hyphens turned into spaces, '%' kept).
#
# HYIP phrases: share of HYIPs vs non-HYIPs containing each. "investment
# plan(s)" 43% vs 0%; daily/weekly/hourly profit, return, roi, interest or
# earning 28% vs 0%; "N% daily/weekly/..." 20% vs 2%; "principal" 10% vs 1%;
# "total deposits/withdrawn" 5% vs 0%. A page with one passes without an invest
# word.
#
# "principal" was narrowed on Oct 8 to principal locked/returned/included: bare,
# it matched Catalan "contingut principal" (main content) on a private bank, and
# 3 of its 7 HYIP matches were generic ("loss of principal").
hyip_phrases = [re.compile(p) for p in (
    r'\binvestment plans?\b',
    r'\b(daily|weekly|hourly) (profit|return|roi|interest|earning)',
    r'\d+(\.\d+)? ?% ?(daily|weekly|monthly|hourly|per day|per week|per month|a day|every day|each day|after)',
    r'\bprincipal (is |will be )?(locked|returned|return|included|back)\b',
    r'\btotal (deposits?|withdrawn|withdrawals)\b',
)]

# HYIP markers (added Oct 8): min/max deposit, plan tiers, referral commission, and
# % over a stated period. Weaker than the phrases: casinos and presales also show
# minimum deposits, VIP plans and referral bonuses, so a marker counts only on a
# page with no exclusion word, and it does not stand in for the invest word.
#
# Since Oct 8 a page needs a phrase or a marker to reach the LLM (content_filter()).
# On the hand-labelled pages that pass the rest of the filter, llama3.2:3b could not
# tell HYIPs from look-alikes under any prompt tried (AUC 0.39-0.49, eval_prompt.py),
# while these could: with the exclusion words, they pass 44 of the 67 labelled
# HYIPs and 4 of the 87 non-HYIPs, against 66 and 35 when neither was required.
hyip_markers = [re.compile(p) for p in (
    r'(min(imum)?\.? ?(deposit|investment|invest)|max(imum)?\.? ?(deposit|investment))',
    r'\b(basic|starter|silver|gold|premium|vip|diamond|platinum) plan\b',
    r'\breferral (commission|bonus|program)',
    r'\d+(\.\d+)? ?% ?(per hour|roi|for \d+ days|in \d+ (hours|days))',
)]

# Exclusion words mark meme coins and presales, parked domains, QFS pages and
# casinos. None appears on any of the 67 labelled HYIPs, so since Oct 8 they drop
# a page even when it has an HYIP phrase (a gambling app passed on "daily
# earnings"). Gambling words count only when two different ones appear:
# one HYIP describes a resort "casino" project, another says trading is not
# "blind gambling", and HYIPs advertise "limited slots".
exclusion_phrases = {name: re.compile(p) for name, p in (
    ('tokenomics', r'\btokenomics\b'),
    ('presale', r'\bpre ?sale\b'),
    ('airdrop', r'\bairdrops?\b'),
    ('token sale', r'\btoken (sale|launch)\b'),
    ('circulating supply', r'\bcirculating supply\b'),
    ('contract address', r'\bcontract address\b'),
    ('domain for sale', r'\bdomain\b.{0,40}\bfor sale\b|\bfor sale\b.{0,40}\bdomain\b'),
    ('buy this domain', r'\b(buy this domain|make an offer)\b'),
    ('qfs', r'\b(qfs|quantum financial system)\b'),
)}
gambling_words = {name: re.compile(p) for name, p in (
    ('casino', r'\bcasinos?\b'),
    ('gambling', r'\bgambling\b'),
    ('betting', r'\b(bets?|betting|sportsbook)\b'),
    ('slots', r'\bslots?\b'),
    ('poker', r'\b(poker|jackpot|roulette|blackjack)\b'),
    ('togel', r'\btogel\b'),
    ('odds', r'\bodds\b'),
)}


def has_hyip_phrase(clean_text):
    return any(rx.search(clean_text) for rx in hyip_phrases)


def has_hyip_marker(clean_text):
    return any(rx.search(clean_text) for rx in hyip_markers)


def content_exclusions(clean_text):
    """Names of the exclusion words found in clean_text ([] if none)."""
    found = [name for name, rx in exclusion_phrases.items() if rx.search(clean_text)]
    gambling = [name for name, rx in gambling_words.items() if rx.search(clean_text)]
    return found + gambling if len(gambling) >= 2 else found


def table11_filter(clean_text):
    """Table 11 content filter: invest AND coin AND context, as in the artifact.
    An HYIP phrase may stand in for the invest word.

    The original tokenised with:
        clean_strings(s) = [re.sub(r'[^A-Za-z0-9]', '', x) for x in s.split()]
    and intersected each of the three lists against those tokens.
    """
    def find_intersection(list1, list2):
        return list(set(list1).intersection(set(list2)))

    def clean_strings(string_list):
        return [re.sub(r'[^A-Za-z0-9]', '', s) for s in string_list]

    # The artifact lowercased its OCR text but NOT the HTML text it appended,
    # so capitalised page copy could never match these lowercase lists. Here
    # clean_text has already been lowercased by sanitize_text().
    text_splits = clean_strings(clean_text.split())

    matches = [find_intersection(text_splits, invest_words),
               find_intersection(text_splits, coin_words),
               find_intersection(text_splits, context_words)]
    return bool((len(matches[0]) or has_hyip_phrase(clean_text))
                and len(matches[2]) and len(matches[1]))


def content_filter(clean_text):
    """True if the page should go to the LLM: it passes table11_filter(), has an
    HYIP phrase or marker, and has no exclusion words."""
    return (table11_filter(clean_text)
            and (has_hyip_phrase(clean_text) or has_hyip_marker(clean_text))
            and not content_exclusions(clean_text))

# --- OLLAMA LOCAL EVALUATOR (primary verdict; escalates the unsure cases to OpenAI) ---

OLLAMA_URL = "http://localhost:11434/api/chat"
# in recv.py
OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_MAX_ATTEMPTS = 1
OLLAMA_TIMEOUT = 90  # seconds, generous for CPU-only inference on longer prompts

# Ollama runs llama3.2:3b with a 4,096-token context. A longer prompt is cut to
# its tail, which drops the system prompt, so the model answers without its
# instructions and returns no JSON. Measured Oct 1-5: each of the 102 "JSON
# decode error" failures on VM2/VM3 matched a prompt over 4,096 tokens (median
# ~7k, max 140k), and those pages went to OpenAI. sanitize_text() output is
# ASCII at roughly 3-4 chars per token, so 8,000 chars (~2,000-2,700 tokens)
# plus the ~650-token system prompt fits with room for the reply.
# Only Ollama's copy is capped: content_filter() and the OpenAI fallback still
# see the full page (see the note in sanitize_text()).
OLLAMA_MAX_TEXT_CHARS = 8000

# Below this, a yes/no from Ollama is not trusted on its own and goes to OpenAI.
#
# check() files a confirmed scam only when confidence > SCAM_CONFIDENCE_BAR, so a
# 'yes' settled locally must clear that SAME bar. At equality it does not: check()
# drops through to its else branch and writes the domain to llm_rejected.txt -- a
# positive logged as a rejection. Hence +1 rather than equality. This is not
# hypothetical; llama3 returned exactly 80 on the first live page tested, and
# models round to 80 constantly.
SCAM_CONFIDENCE_BAR = 80  # must match the threshold check() compares against
OLLAMA_MIN_CONFIDENCE = SCAM_CONFIDENCE_BAR + 1

# Both engines use the paper's classification prompt verbatim: Appendix B of "The
# Poorest Man in Babylon" (WWW '25), the prompt of the original Crimson run. It is
# the baseline. Additions go in PROMPT_ADDITIONS, after it; its text and its output
# structure ('answer' yes/no and a one-word 'reason') stay as they are.
#
# It replaced an HYIP-only rewrite (Oct 7) with worked examples that llama3.2:3b
# copied: on VM2, 43 of its 50 "yes" verdicts were exactly {"yes", 100, "plans"},
# the second example's output, and they included a private bank, a gambling app and
# trading courses. It copied the self-reported confidence too (always 90-100), so no
# page fell under OLLAMA_MIN_CONFIDENCE and none went to OpenAI. Confidence now comes
# from the probability the model gives its answer; see answer_confidence().
PAPER_PROMPT = (
    "You are a financial advisor programmed to respond in JSON format. Your responses are "
    "limited to ‘yes’ or ‘no’, represented by the ‘answer’ key, and you must provide a "
    "one-word reasoning for your decision under the ‘reason’ key. Be sure of your answer. "
    "Determine if the provided text likely originates from a cryptocurrency investment "
    "scam website, characterized by promises of high returns from cryptocurrency "
    "investments. If the text suggests a low probability of being a scam, does not prompt "
    "users to log in/sign up, seems like a news site, or does not solicit users to contact "
    "the site for investing in cryptocurrency, respond ‘no’"
)

# E1 and E2 from eval_prompt.py, after a blank line so the paper's text is left
# exactly as it is. On the labelled pages content_filter() passes (44 HYIPs, 4 not),
# llama3.2:3b with these additions files 38 HYIPs and 4 non-HYIPs, and sends 16 of
# 101 pages to OpenAI. With the paper's prompt alone it filed 1 HYIP and sent 73 of
# 113 (measured before "principal" was narrowed).
PROMPT_ADDITIONS = (
    "\n\n"
    "A cryptocurrency investment scam here means a high-yield investment program "
    "(HYIP): a site where visitors open an account and deposit cryptocurrency or "
    "money into the site's own investment plans, in return for promised high returns. "
    "Other cryptocurrency scams, such as wallet drainers, airdrops and giveaways, "
    "token presales, fund recovery services and casinos, are not HYIPs; respond ‘no’ "
    "for them."
    "\n\n"
    "Respond ‘yes’ only if the text itself promises returns on deposits, such as a "
    "percentage or multiple over a period (2% daily, 150% after 7 days), or lists "
    "investment plans with deposit amounts. A trading platform, broker, bank, "
    "prop-trading firm, course or software product that does not promise such "
    "returns is ‘no’."
)
OLLAMA_SYSTEM_PROMPT = PAPER_PROMPT + PROMPT_ADDITIONS

# The paper's output structure, enforced: Ollama's `format` and OpenAI's strict
# json_schema both constrain decoding to it, so the reply always parses.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": ["yes", "no"]},
        "reason": {"type": "string"},
    },
    "required": ["answer", "reason"],
    "additionalProperties": False,
}

OLLAMA_ANSWERS = {"yes", "no"}


def answer_confidence(content, tokens):
    """Confidence 0-100 in the 'answer' value of content, from token probabilities.

    tokens is [(token, [(candidate, logprob), ...]), ...] in order, covering content.
    The token that starts the answer value is located by offset, and the probability
    its candidates give to "yes" and to "no" (any case, spacing or quoting) is
    compared. The candidates are the model's own distribution, before the schema
    constrains it. When the losing answer is not among them, the least likely
    candidate's probability stands in for it, which errs low. Returns 0 if the answer
    token can't be found, so the page escalates instead of being trusted.
    """
    m = re.search(r'"answer"\s*:\s*"', content)
    if not m or not tokens:
        return 0
    pos = 0
    for token, candidates in tokens:
        pos += len(token)
        if pos > m.end():
            break
    else:
        return 0
    mass = {"yes": 0.0, "no": 0.0}
    for candidate, logprob in candidates:
        word = candidate.strip().strip('"').lower()
        if word in mass:
            mass[word] += math.exp(logprob)
    chosen = content[m.end():].split('"', 1)[0].strip().lower()
    if chosen not in mass or mass[chosen] == 0:
        return 0
    other = "no" if chosen == "yes" else "yes"
    if mass[other] == 0:
        mass[other] = math.exp(min(logprob for _, logprob in candidates))
    return round(100 * mass[chosen] / (mass["yes"] + mass["no"]))

def validate_ollama_response(response):
    """Shape check for the local verdict ('answer' yes/no and 'reason'),
    normalising the answer in place. Confidence is not part of the reply; it is
    computed from token probabilities by answer_confidence().
    """
    if not (
        isinstance(response, dict)
        and 'answer' in response
        and 'reason' in response
        and isinstance(response['answer'], str)
        and isinstance(response['reason'], str)
    ):
        return False
    answer = response['answer'].strip().lower()
    if answer not in OLLAMA_ANSWERS:
        return False
    response['answer'] = answer
    return True

def evaluate_ocr_with_ollama(clean_text, domain_name, curr_date, system_prompt=None):
    """
    Primary verdict, computed locally by Ollama (OLLAMA_MODEL). Returns
    {"answer": "yes"|"no", "confidence": 0-100, "reason": str}, or
    {"answer": "unsure", "confidence": 0, ...} if Ollama failed.

    Only the cases this step cannot settle -- a failure, or an answer the model
    gives less than OLLAMA_MIN_CONFIDENCE probability -- are escalated to the paid
    OpenAI call by evaluate_hybrid(). Everything else is decided here at no API cost.

    system_prompt overrides OLLAMA_SYSTEM_PROMPT, for scoring prompt variants
    offline (eval_prompt.py).

    Uses /api/chat with a proper role-structured messages array so llama3's chat
    template is applied correctly, rather than concatenating roles into a single
    raw /api/generate prompt string (which the base model may not interpret as
    an instruction to follow).
    """
    # The page text alone, as the original Crimson artifact sent it. A "Domain:"
    # line used to precede it, and names with invest/trade/capital in them biased
    # the 3b model towards "yes".
    messages = [
        {"role": "system", "content": system_prompt or OLLAMA_SYSTEM_PROMPT},
        {"role": "user", "content": clean_text[:OLLAMA_MAX_TEXT_CHARS]}
    ]

    attempt = 0
    while attempt < OLLAMA_MAX_ATTEMPTS:
        attempt += 1
        try:
            resp = curl_requests.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": -1,
                    # temperature 0: without it Ollama's default (0.8) made this
                    # return both 'yes' and 'no' for identical input across runs,
                    # which is not acceptable for the primary verdict.
                    "options": {"num_predict": 64, "temperature": 0},
                    "format": VERDICT_SCHEMA,
                    "logprobs": True,
                    "top_logprobs": 5,
                },
                timeout=OLLAMA_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            raw_output = body.get("message", {}).get("content", "")

            # 'format' constrains the reply to VERDICT_SCHEMA, so a direct parse
            # should succeed; the {...} extraction is kept as a fallback.
            try:
                parsed = json.loads(raw_output)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', raw_output, re.DOTALL)
                if not match:
                    raise
                parsed = json.loads(match.group(0))

            if validate_ollama_response(parsed):
                tokens = [(t.get("token", ""),
                           [(c.get("token", ""), c.get("logprob", 0.0)) for c in t.get("top_logprobs") or []]
                           or [(t.get("token", ""), t.get("logprob", 0.0))])
                          for t in body.get("logprobs") or []]
                parsed["confidence"] = answer_confidence(raw_output, tokens)
                return parsed
            else:
                log(f"{domain_name},Ollama malformed response attempt {attempt},{raw_output}", curr_date, 'errors.txt', 'a')
                continue
        except TimeoutError:
            raise
        except curl_requests.exceptions.RequestException as e:
            log(f"{domain_name},Ollama connection error attempt {attempt},{e}", curr_date, 'errors.txt', 'a')
            continue
        except json.JSONDecodeError as e:
            log(f"{domain_name},Ollama JSON decode error attempt {attempt},{e}", curr_date, 'errors.txt', 'a')
            continue
        except Exception as e:
            log(f"{domain_name},Ollama unexpected error attempt {attempt},{e}", curr_date, 'errors.txt', 'a')
            continue

    # Fail-safe: escalate, do not decide. Ollama is the PRIMARY evaluator now, so
    # defaulting an outage to "no" would silently drop every domain for as long as
    # the service was down -- the pipeline would look healthy and detect nothing.
    # 'unsure' routes these to OpenAI, which costs money but stays correct.
    log(f"{domain_name},Ollama failed after {OLLAMA_MAX_ATTEMPTS} attempts - escalating to OpenAI", curr_date, 'errors.txt', 'a')
    return {"answer": "unsure", "confidence": 0, "reason": "ollama_unavailable"}


def _get_openai_client():
    """Lazy singleton. Avoids constructing a client per call."""
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(
            api_key=OPENAI_API_KEY,
            timeout=OPENAI_TIMEOUT,
            max_retries=OPENAI_MAX_RETRIES,
        )
    return _openai_client

# The same prompt as Ollama's (see the note above PAPER_PROMPT), and the same
# output structure, enforced by a strict schema.
OPENAI_SYSTEM_PROMPT = OLLAMA_SYSTEM_PROMPT

OPENAI_SCAM_SCHEMA = {
    "name": "scam_verdict",
    "strict": True,
    "schema": VERDICT_SCHEMA,
}




def evaluate_ocr_with_openai(clean_text, domain_name, curr_date):
    """
    Final high-confidence verdict. Only reached when Ollama escalates.

    Returns {"is_scam": bool, "confidence": int, "reason": str, "model": str}
    on success.

    FAILS CLOSED: on any API error returns {"error": True, "is_scam": None, ...}.
    check() routes that to needs_retry.txt instead of filing the domain as safe.
    This is the fix for the 23 API_Exception_Fallback entries that were
    indistinguishable from real rejections.
    """
    try:
        client = _get_openai_client()
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature = 0,
            messages=[
                {"role": "system", "content": OPENAI_SYSTEM_PROMPT},
                {"role": "user", "content": f"Text Content: {clean_text}"},
            ],
            response_format={"type": "json_schema", "json_schema": OPENAI_SCAM_SCHEMA},
            logprobs=True,
            top_logprobs=5,
        )

        msg = resp.choices[0].message

        # Model declined to answer (rare, but strict-mode surfaces it explicitly)
        if getattr(msg, "refusal", None):
            log(f"{domain_name},OpenAI refusal,{msg.refusal}", curr_date, 'errors.txt', 'a')
            return {"is_scam": None, "confidence": 0, "reason": "API_Refusal", "error": True}

        # Token accounting -> lets you track spend against the $50 cap.
        # Format: domain,model,prompt_tokens,completion_tokens
        u = resp.usage
        log(f"{domain_name},{OPENAI_MODEL},{u.prompt_tokens},{u.completion_tokens}",
            curr_date, 'openai_usage.txt', 'a')

        reply = json.loads(msg.content)
        logprobs = resp.choices[0].logprobs
        tokens = [(t.token, [(c.token, c.logprob) for c in t.top_logprobs] or [(t.token, t.logprob)])
                  for t in (logprobs.content if logprobs and logprobs.content else [])]
        return {
            "is_scam": reply["answer"] == "yes",
            "confidence": answer_confidence(msg.content, tokens),
            "reason": reply["reason"],
            "model": OPENAI_MODEL,
        }
    except TimeoutError:
        raise

    except Exception as e:
        # Timeout, 429 after retries, network, auth, bad model name — all land here.
        # NOT a verdict. Do not let this look like "safe".
        log(f"{domain_name},OpenAI API Error,{type(e).__name__},{e}", curr_date, 'errors.txt', 'a')
        return {
            "is_scam": None,
            "confidence": 0,
            "reason": f"API_Error_{type(e).__name__}",
            "error": True,
        }


def evaluate_hybrid(clean_text, domain_name, curr_date):
    """
    Two-stage verdict: Ollama decides locally, OpenAI settles only what it can't.

    Returns the same dict shape as evaluate_ocr_with_openai() -- {"is_scam": bool,
    "confidence": int, "reason": str, "model": str}, or {"error": True, ...} -- so
    check() needs no special-casing for which engine produced the answer.

    Escalates when Ollama fails ('unsure'), or gives its yes/no less than
    OLLAMA_MIN_CONFIDENCE probability.
    That threshold is not arbitrary: check() files a confirmed scam only above
    confidence 80, so a 'yes' closed out locally beneath that bar would be written to
    llm_rejected.txt as a rejection despite being a positive.

    Both engines' verdicts are logged, so the local model's agreement rate with OpenAI
    can be measured from ollama_verdicts.txt vs openai_usage.txt before trusting it further.
    """
    local = evaluate_ocr_with_ollama(clean_text, domain_name, curr_date)
    answer = local.get("answer", "unsure")
    confidence = local.get("confidence", 0)
    reason = local.get("reason", "unspecified")

    log(f"{domain_name},{OLLAMA_MODEL},{answer},{confidence},{reason}",
        curr_date, 'ollama_verdicts.txt', 'a')

    # Settled locally. No API call, no cost.
    if answer != "unsure" and confidence >= OLLAMA_MIN_CONFIDENCE:
        return {
            "is_scam": answer == "yes",
            "confidence": confidence,
            "reason": reason,
            "model": OLLAMA_MODEL,
        }

    # Inconclusive -> authoritative verdict from OpenAI.
    log(f"{domain_name},{answer},{confidence},{reason}",
        curr_date, 'ollama_escalations.txt', 'a')
    verdict = evaluate_ocr_with_openai(clean_text, domain_name, curr_date)
    verdict["escalated_from"] = f"ollama:{answer}:{confidence}"
    return verdict


def OCR(url_name, curr_date, html_content):
    global ocr_domains
    ocr_domains += 1
    log(ocr_domains, curr_date, 'ocr_domains.txt', 'w')
    sspath = f'data/{SYSNO}/screenshots/{curr_date}/{url_name}/'
    if os.path.exists(sspath) and 'full_page.png' not in os.listdir(sspath): return False
    try:
        text = str(pytesseract.image_to_string(Image.open(f'{sspath}/full_page.png'))).lower().encode('utf-8')
        text = re.sub(r'\s+', ' ', re.sub(r'[^\w\s,.!?\-\'"]+', '', re.sub(r'[\n\r\t]+', ' ', re.sub(r'[^\x00-\x7F]+', ' ', text.decode('utf-8', 'replace'))))).strip()
        if text:
            return text
        return False
    except Exception as e:
        log(f"{url_name},OCR (),{e}", curr_date, 'errors.txt', 'a')
        return False

def getIPInfo(url_name):
    curr_date = str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:]
    def IPAPI(ip_address):
        try:
            response = curl_requests.get(f'http://ip-api.com/json/{ip_address}?fields=status,message,countryCode,region,city,lat,lon,isp,org,query')
            return response.json()
        except Exception as e:
            log(f"{url_name},IPAPI (),{e}", curr_date, 'errors.txt', 'a')
            return None
    try:
        return IPAPI(socket.gethostbyname(url_name))
    except Exception as e:
        log(f"{url_name},getIPInfo (),{e}", curr_date, 'errors.txt', 'a')
        return None

def getioc(url_name, curr_date, html_content):
    try:
        ioc = searcher.search_data(html_content, targets={'bitcoin', 'bitcoincash', 'cardano', 'dashcoin', 'dogecoin', 'ethereum', 'litecoin', 'monero', 'ripple', 'tezos', 'tronix', 'zcash', 'webmoney', 'onionAddress', 'email', 'phoneNumber', 'facebookHandle', 'githubHandle', 'instagramHandle', 'linkedinHandle', 'pinterestHandle', 'telegramHandle', 'twitterHandle', 'whatsappHandle', 'youtubeHandle', 'youtubeChannel'})
        ioc_dict = {}
        for item in ioc:
            item = str(item).split('\t')
            ioc_dict.update({item[0]: item[1]})
        return ioc_dict
    except Exception as e:
        log(f"{url_name},getioc (),{e}", curr_date, 'errors.txt', 'a')
        return None

def get_website_title(html_content):
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        title_tag = soup.find('title')
        if title_tag is not None:
            return title_tag.get_text()
        return "No Title."
    except curl_requests.exceptions.RequestException:
        return "No Title."

derive_schemes = ('https://', 'http://')

# Pages are fetched with curl_cffi (curl-impersonate), not requests. Its TLS
# handshake and HTTP/2 settings match a real Chrome's, and it sends Chrome's own
# default headers for the version it impersonates.
#
# requests used to do this fetch with a Chrome User-Agent bolted on, after
# elementex.tech (Cloudflare) returned 403 to python-requests' own header. That
# fixed the header only: requests' OpenSSL handshake still fingerprinted it as
# Python (JA3/JA4, HTTP/2 settings) to any bot rule that checks the connection,
# and a blocked site was filed as 'unreachable.' and never screenshotted.
#
# No headers are passed. A User-Agent that disagrees with the impersonated Chrome
# version is itself a bot signal. "chrome" tracks the newest Chrome fingerprint
# the installed curl_cffi ships; `curl-cffi update` refreshes them.
FETCH_IMPERSONATE = "chrome"

def is_domain_available(url_name, curr_date):
    """Fetch the page. HTTPS first, HTTP only as a fallback.

    This used to be hardcoded to 'http://' with no fallback, which is wrong for
    essentially the whole modern web: an HTTPS-only host either refuses the plain
    HTTP connection or answers with a redirect, and any site whose edge declines
    to serve cleartext was being filed as 'unavailable.' and never classified.

    verify=False because the target set is newly-registered scam infrastructure,
    where self-signed and expired certificates are common. We are reading hostile
    pages for classification, not establishing trust with them -- refusing to look
    at a site because its certificate is bad would drop exactly the sites most
    worth looking at.

    timeout=10 is curl's total time for the request, where requests applied it to
    each connect and read. A page that trickles in for over 10s now fails.
    """
    global less_words_domains
    def extract_js_libraries(html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        script_tags = soup.find_all('script', src=True)
        return [tag['src'] for tag in script_tags]

    response = None
    for scheme in derive_schemes:
        try:
            response = curl_requests.get(scheme + url_name, timeout=10, verify=False,
                                         impersonate=FETCH_IMPERSONATE)
            break
        except Exception:
            continue          # TLS failure, refused, timeout -> try the next scheme
    if response is None:
        return False

    try:
        if str(response.status_code)[0] not in ['4', '5']:
            html_content = response.text
            # A fresh converter per page. One module-level HTML2Text used to serve
            # every page, and html2text keeps state between handle() calls: after a
            # page with an unclosed <abbr>, every later page's text was appended to
            # abbr_data. On VM3 (Oct 15) that string reached ~195 MB, each page
            # spent the full 300s alarm copying it, and was filed 'unreachable.'
            text_converter = html2text.HTML2Text()
            text_converter.ignore_links = True
            text_converter.ignore_images = True
            plain_text = str(text_converter.handle(html_content)).split()
            if len(plain_text) < 20:
                less_words_domains += 1
                log(less_words_domains, curr_date, 'less_words_domains.txt', 'w')
                return False
            js_libraries = extract_js_libraries(html_content)
            return (js_libraries, html_content)
        else:
            log(f"{url_name}", curr_date, 'unavailable.txt', 'a')
            return False
    except Exception:
        return False

def get_domain_creation_date(url_name, curr_date):
    try:
        domain_info = whois.whois(url_name)
        if domain_info.creation_date:
            creation_dates = domain_info.creation_date
            if isinstance(creation_dates, list):
                return creation_dates[0]
            return creation_dates
        return "Creation date information not found."
    except Exception as e:
        log(f"{url_name},get_domain_creation_date (),{e}", curr_date, 'errors.txt', 'a')
        return "Creation date information not found."

def handlePositives(url_name, text, js_libraries, html_content, path, curr_date, evaluation):
    check_path = f"data/{SYSNO}/check/{curr_date}/"
    ioc = getioc(url_name, curr_date, html_content)
    ensure_directory_exists(check_path)
    os.system(f"cp {path} {check_path} -r")
    ip_info = getIPInfo(url_name)

    domain_creation_date = get_domain_creation_date(url_name , curr_date)
    if domain_creation_date == "Creation date information not found." or not isinstance(domain_creation_date, datetime):
        domain_creation_date = 'NotFound'
    else:
        domain_creation_date = str(int(domain_creation_date.timestamp()))

    log_data = {
        "url": url_name,
        "title": get_website_title(html_content),
        "creation_date": domain_creation_date,
        "ip_info": ip_info,
        "ioc": ioc,
        "js_libraries": js_libraries,
        "text": text
    }

    # Merge the LLM verdict (is_scam = is an HYIP) into the final log dictionary
    log_data.update(evaluation)

    log_result(json.dumps(log_data), curr_date, 'results.log', 'a')

def log_result(url_name, curr_date, filename, mode='a'):
    filename = f"{filename}.{curr_date}"
    ensure_directory_exists(f"results/{SYSNO}/")
    with open(os.path.join('results', SYSNO, filename), mode) as f:
        f.write(f'{datetime.now(timezone(timedelta(hours=-5))).strftime("%H:%M:%S")}:\t{url_name}\n')

# --- THE HYBRID ORCHESTRATION PIPELINE ---

def check(url_name, curr_date):
    global unavailable_domains
    global domains_checked
    UNREADABLE = {'unreadable', 'garbage', 'garbled', 'illegible'}
    domains_checked += 1
    log(domains_checked, curr_date, 'domains_checked.txt', 'w')

    start = time.time()
    domain_availability = is_domain_available(url_name, curr_date)
    if not domain_availability:
        unavailable_domains += 1
        log(unavailable_domains, curr_date, 'unavailable_domains.txt', 'w')
        end = time.time()
        log(f"{url_name},{end-start}", curr_date, 'time_availability.txt', 'a')
        return f"unreachable."
    js_libraries, html_content = domain_availability
    end = time.time()
    log(f"{url_name},{end-start}", curr_date, 'time_availability.txt', 'a')

    ensure_directory_exists(f"data/{SYSNO}/screenshots/{curr_date}")
    sspath = f"data/{SYSNO}/screenshots/{curr_date}/{url_name}/"
    ensure_directory_exists(sspath)

    # Every reachable page is screenshotted and OCR'd, as in the paper's artifact.
    # The LLM judges the OCR text, i.e. what a visitor sees: content drawn by
    # JavaScript or baked into images counts, and HTML text a visitor never sees
    # does not. The HTML text is still read, but only for the content filter.
    #
    # Until Oct 9 OCR ran only when the HTML text was under 150 characters, and the
    # LLM judged the HTML text everywhere else. That was far faster (median 0.03s
    # against 3.2s per page on VM2, Oct 9) but blind to pitches held in images.
    # As in the artifact, a page whose screenshot or OCR fails is dropped.
    dom_text = sanitize_text(extract_dom_text(html_content))

    start = time.time()
    try:
        ss = selenium_obj.take_screenshot(url_name, curr_date, sspath, SYSNO)
    except Exception as e:
        log(f"{url_name},selenium_obj.take_screenshot (),{e}", curr_date, 'errors.txt', 'a')
        ss = False
    log(f"{url_name},{time.time()-start}", curr_date, 'time_screenshot.txt', 'a')
    if not ss:
        if os.path.exists(sspath): os.system(f'rm -rf {sspath}')
        return "screenshot failure."

    start = time.time()
    raw_ocr_text = OCR(url_name, curr_date, html_content)
    if not raw_ocr_text:
        if os.path.exists(sspath): os.system(f'rm -rf {sspath}')
        log(f"{url_name},{time.time()-start}", curr_date, 'time_OCR.txt', 'a')
        return "OCR failure."
    clean_text = sanitize_text(raw_ocr_text)

    # content_filter runs first on every page: it is free, and at ~98.7% rejection
    # it keeps the Ollama call (the expensive step on CPU-only hardware) off most
    # pages. As in the artifact's OCR(), it reads the OCR text and the HTML text
    # combined; the LLM gets the OCR text only.
    filter_text = f"{clean_text} {dom_text}"
    if content_filter(filter_text):
        evaluation = evaluate_hybrid(clean_text, url_name, curr_date)
    else:
        # Pages that pass Table 11 but are dropped here are logged with the exclusion
        # words they contain, or 'no_hyip_phrase' if they have neither a phrase nor
        # a marker, so an HYIP lost here can be found and its wording added.
        if table11_filter(filter_text):
            log(f"{url_name},{'+'.join(content_exclusions(filter_text)) or 'no_hyip_phrase'}",
                curr_date, 'content_excluded.txt', 'a')
        evaluation = {"is_scam": False, "confidence": 0, "reason": "Cleared_By_Content_Filter"}

    if evaluation.get("error"):
        # FAIL CLOSED. Not classified either way. Screenshots kept for the retry pass.
        log(f"{url_name}\t{evaluation.get('reason')}",
            curr_date, 'needs_retry.txt', 'a')
        result_msg = "Needs retry (API error)."
    elif (evaluation.get("is_scam")
          and evaluation.get("reason", "").lower() in UNREADABLE):
        log(f"{url_name}\tflagged_but_unreadable\t{evaluation.get('reason')}",
            curr_date, 'needs_retry.txt', 'a')
        result_msg = "Flagged on unreadable text - needs retry."
    elif evaluation.get("is_scam") and evaluation.get("confidence", 0) > SCAM_CONFIDENCE_BAR:
        log(f"Confirmed Scam [{evaluation.get('model', '?')}]: "
            f"{url_name} - Reason: {evaluation.get('reason')}",
            curr_date, 'llm_scams.txt', 'a')
        handlePositives(url_name, clean_text, js_libraries, html_content, sspath, curr_date, evaluation)
        result_msg = "Scam Found & Confirmed!"

    else:
        log(f"Rejected [{evaluation.get('model', 'local')}]: "
            f"{url_name} - Reason: {evaluation.get('reason')}",
            curr_date, 'llm_rejected.txt', 'a')
        if os.path.exists(sspath):
            os.system(f'rm -rf {sspath}')
        result_msg = "Safe / Rejected."
    # OCR, filter and classification time; the screenshot is in time_screenshot.txt
    log(f"{url_name},{time.time()-start}", curr_date, 'time_OCR.txt', 'a')
    time.sleep(1.0)
    return result_msg
def log(url_name, curr_date, filename, mode='a'):
    ensure_directory_exists(f"logs/{SYSNO}/{curr_date}")
    with open(os.path.join('logs', SYSNO, curr_date, filename), mode) as f:
        f.write(f'{datetime.now(timezone(timedelta(hours=-5))).strftime("%H:%M:%S")}\t{SCREENNO}:\t{url_name}\n')

def mkdirs():
    if 'data' not in os.listdir('.'): ensure_directory_exists('data')
    if 'logs' not in os.listdir('.'): ensure_directory_exists('logs')
    if SYSNO not in os.listdir('data/'):
        ensure_directory_exists(f"data/{SYSNO}")
        ensure_directory_exists(f"data/{SYSNO}/screenshots")
        ensure_directory_exists(f"data/{SYSNO}/ocr")
        ensure_directory_exists(f"data/{SYSNO}/check")
    if SYSNO not in os.listdir('logs/'):
        ensure_directory_exists(f"logs/{SYSNO}")

class WorkerTimeout(BaseException):
    """The 300s per-domain alarm. A BaseException, like KeyboardInterrupt, so no
    'except Exception' can swallow it. It used to be a TimeoutError, which since
    Python 3.10 is also socket.timeout: urllib3 caught it mid-read as a socket
    timeout, and screenshots ran up to 933s on VM2 with no Worker_Timeout (Oct 27)."""

def main():
    def timeout_handler(signum, frame):
        raise WorkerTimeout("Function execution timed out")
    def callback(ch, method, properties, body):
        global visited_cache
        url_name = body.decode('utf-8')
        curr_date = str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:]
        log(f" [x] Received {url_name}", curr_date, 'worker.txt', 'a')
        if url_name in visited_cache:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            log(f"[x] Dup {url_name}", curr_date, 'worker.txt', 'a')
        else:
            visited_cache[url_name] = True
            signal.signal(signal.SIGALRM, timeout_handler)
            # Budget for the worst-case path, which is now BOTH engines in series:
            #   fetch 2x10s + Selenium 2x20s + OCR of a tall page ~30s
            #   + Ollama 90s (OLLAMA_TIMEOUT)
            #   + OpenAI 30s x3 (OPENAI_TIMEOUT x 1+OPENAI_MAX_RETRIES)  ~= 270s
            # At the old 120s an escalated page on the OCR path tripped this and was
            # filed as Worker_Timeout even though both engines answered correctly.
            # Raised from 240 when every page started going through OCR (Oct 9).
            # Lower this once the workers have AVX2 and Ollama returns in <10s.
            signal.alarm(300)
            try:
                result = check(url_name, curr_date)
                sys.stdout.flush()
            except (WorkerTimeout, TimeoutError) as e:
                log(f"{url_name},[x] Timeout occurred,{e}", curr_date, 'errors.txt', 'a')
                log(f"{url_name}\tWorker_Timeout", curr_date, 'needs_retry.txt', 'a')
                result = "Timeout"
            signal.alarm(0)
            log(f"[x] Done {url_name} {result}", curr_date, 'worker.txt', 'a')
            # results/ and data/check/ only change in handlePositives(), so syncing
            # after every domain sent ~2 ssh logins/s to the 2-core broker for nothing.
            # rsync copies the whole folder, so a failed sync is caught up by the next one.
            if result == "Scam Found & Confirmed!":
                sync(url_name, curr_date, f"results/{SYSNO}/", "/home/ubuntu/crimson/collected_results/")
                sync(url_name, curr_date, f"data/{SYSNO}/check/", "/home/ubuntu/crimson/collected_results/")
            ch.basic_ack(delivery_tag=method.delivery_tag)

    while True:
        try:
            credentials = pika.PlainCredentials(os.environ['RABBITMQ_USER'], os.environ['RABBITMQ_PASS'])
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=QUEUE_IP, credentials=credentials))
            channel = connection.channel()
            channel.queue_declare(queue='cryptoscams', durable=True)
            print(' [*] Waiting for messages. To exit press CTRL+C')
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue='cryptoscams', on_message_callback=callback)
            channel.start_consuming()
        except pika.exceptions.StreamLostError as e:
            log(f"RabbitMQ Connection Lost. Reconnecting...,{e}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'errors.txt', 'a')
            time.sleep(10)

if __name__ == '__main__':
    try:
        mkdirs()
        main()
    except Exception as e:
        log(f"exit {e}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'exit.txt')
        try:
            log(f"sys.exit(0) {e}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'exit.txt')
            sys.exit(0)
        except SystemExit as e_:
            log(f"SystemExit {e_}", str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:], 'exit.txt')
            os._exit(0)
