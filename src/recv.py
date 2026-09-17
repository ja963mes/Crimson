import pika, sys, os
import pytz
from datetime import datetime, timezone, timedelta, date
import time
import utils.keyword_utils as kw_utils
import utils.screenshot
import requests
import urllib3
# is_domain_available() fetches with verify=False (see its docstring). Without
# this, every scam site with a bad certificate logs an InsecureRequestWarning.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import html2text
import pytesseract
import whois
import re
from PIL import Image
import signal
import json
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


QUEUE_IP = '130.245.32.96'
searcher = Searcher()
os.environ['OMP_THREAD_LIMIT'] = '1'
SYSNO = sys.argv[1]
SCREENNO = sys.argv[2]
newYorkTz = pytz.timezone("America/New_York")
text_converter = html2text.HTML2Text()
text_converter.ignore_links = True
text_converter.ignore_images = True
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
    # Absolute path on the worker node (VM2/VM3)
    abs_local_path = f"/home/ubuntu/crimson/src/{local_file_path}"

    # Absolute path on the master node (VM1)
    abs_remote_path = f"/home/ubuntu/crimson/collected_results/{local_file_path}"

    # Absolute path to the SSH key
    abs_key_path = "/home/ubuntu/.ssh/id_ed25519"

    try:
        subprocess.run([
            'rsync', '-avz',
            '-e', f'ssh -i {abs_key_path} -o StrictHostKeyChecking=no',
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


def content_filter(clean_text):
    """Table 11 content filter: invest AND coin AND context, as in the artifact.

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
    return bool(len(matches[0]) and len(matches[2]) and len(matches[1]))

# --- OLLAMA LOCAL EVALUATOR (primary verdict; escalates the unsure cases to OpenAI) ---

OLLAMA_URL = "http://localhost:11434/api/chat"
# in recv.py
OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_MAX_ATTEMPTS = 1
OLLAMA_TIMEOUT = 90  # seconds, generous for CPU-only inference on longer prompts

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

OLLAMA_SYSTEM_PROMPT = (
    "You are a cryptocurrency fraud detector. You will be given text scraped from a website. "
    "Decide whether the website is a cryptocurrency scam (for example: fake investment platforms, "
    "wallet drainers, fake airdrops, guaranteed-return schemes, seed-phrase phishing). "
    "Respond ONLY with a JSON object with exactly three keys: "
    '"answer", "confidence" and "reason". '
    'Set "answer" to "yes" if the text IS a cryptocurrency scam. '
    'Set "answer" to "no" if the text is NOT a scam (legitimate site, news, blog, or unrelated content). '
    'Set "answer" to "unsure" if you genuinely cannot tell -- for example the text is too short, '
    "garbled, truncated, in a language you cannot read, or does not give you enough to judge. "
    '"unsure" is a valid and useful answer; prefer it over guessing. '
    'Set "confidence" to an integer 0-100 for how certain you are of "answer". '
    'Set "reason" to a single word summarizing why. '
    'Example scam -> {"answer": "yes", "confidence": 95, "reason": "airdrop"}. '
    'Example legitimate -> {"answer": "no", "confidence": 90, "reason": "news"}. '
    'Example unclear -> {"answer": "unsure", "confidence": 20, "reason": "garbled"}.'
)

OLLAMA_ANSWERS = {"yes", "no", "unsure"}

def validate_ollama_response(response):
    """Shape check for the local verdict, normalising in place.

    'confidence' is coerced rather than required: a 3b model drops or stringifies
    it often enough that rejecting the whole verdict over a missing int would push
    a large share of pages to OpenAI for no good reason. A missing/unparseable
    confidence becomes 0, which routes to OpenAI via the threshold instead.
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
    try:
        response['confidence'] = max(0, min(100, int(response.get('confidence', 0))))
    except (TypeError, ValueError):
        response['confidence'] = 0
    return True

def evaluate_ocr_with_ollama(clean_text, domain_name, curr_date):
    """
    Primary verdict, computed locally by Ollama (OLLAMA_MODEL). Returns
    {"answer": "yes"|"no"|"unsure", "confidence": 0-100, "reason": str}.

    Only the cases this step cannot settle -- 'unsure', or a yes/no below
    OLLAMA_MIN_CONFIDENCE -- are escalated to the paid OpenAI call by
    evaluate_hybrid(). Everything else is decided here at no API cost.

    Uses /api/chat with a proper role-structured messages array so llama3's chat
    template is applied correctly, rather than concatenating roles into a single
    raw /api/generate prompt string (which the base model may not interpret as
    an instruction to follow).
    """
    messages = [
        {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
        {"role": "user", "content": f"Domain: {domain_name}\nText Content: {clean_text}"}
    ]

    attempt = 0
    while attempt < OLLAMA_MAX_ATTEMPTS:
        attempt += 1
        try:
            resp = requests.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": -1,
                    # temperature 0: without it Ollama's default (0.8) made this
                    # return both 'yes' and 'no' for identical input across runs,
                    # which is not acceptable for the primary verdict.
                    # num_predict raised from 64 to fit the added confidence field.
                    "options": {"num_predict": 96, "temperature": 0},
                },
                timeout=OLLAMA_TIMEOUT,
            )
            resp.raise_for_status()
            raw_output = resp.json().get("message", {}).get("content", "")

            # Without 'format: json' the model reliably returns JSON, but may occasionally
            # wrap it in prose or markdown fences. Try a direct parse first, then fall back
            # to extracting the first {...} object from the text.
            try:
                parsed = json.loads(raw_output)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', raw_output, re.DOTALL)
                if not match:
                    raise
                parsed = json.loads(match.group(0))

            if validate_ollama_response(parsed):
                return parsed
            else:
                log(f"{domain_name},Ollama malformed response attempt {attempt},{raw_output}", curr_date, 'errors.txt', 'a')
                continue
        except TimeoutError:
            raise
        except requests.exceptions.RequestException as e:
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
OPENAI_SYSTEM_PROMPT = (
    "You are a Cyber Threat Intelligence Analyst. Your sole objective is to classify "
    "whether text scraped from a newly registered domain belongs to a cryptocurrency "
    "investment scam or wallet-drainer.\n\n"
    "### SCAM INDICATORS (is_scam = true):\n"
    "- Unrealistic promises of high returns, guaranteed profits, or risk-free crypto investments.\n"
    "- High-pressure tactics urging immediate account creation, deposits, or wallet connections.\n"
    "- Unverifiable claims of proprietary AI trading bots or exclusive mining pools.\n"
    "- Seed-phrase / recovery-phrase solicitation.\n\n"
    "### SAFE INDICATORS (is_scam = false):\n"
    "- Legitimate crypto news site, blog, or educational resource.\n"
    "- Standard portfolio tracker, block explorer, or validator/node infrastructure.\n"
    "- Standard login portals with no solicitation for investing.\n"
    "- Text is garbage, broken, or has no actionable crypto-investment context.\n\n"
    "### TEXT QUALITY:\n"
    "The text may be OCR output, and may be garbled or transliterated from a non-Latin "
    "script. If you cannot read it well enough to identify concrete scam content, set "
    "reason to 'unreadable'.\n\n"
    "Set confidence 0-100. Set reason to a single word."
)


OPENAI_SCAM_SCHEMA = {
    "name": "scam_verdict",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "is_scam": {
                "type": "boolean",
                "description": "True if this matches a crypto investment scam/drainer.",
            },
            "confidence": {
                "type": "integer",
                "description": "Confidence score from 0 to 100.",
            },
            "reason": {
                "type": "string",
                "description": "A single word summarizing the primary reason.",
            },
        },
        "required": ["is_scam", "confidence", "reason"],
        "additionalProperties": False,
    },
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

        verdict = json.loads(msg.content)
        verdict["model"] = OPENAI_MODEL
        return verdict
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

    Escalates when Ollama says 'unsure', or answers yes/no below OLLAMA_MIN_CONFIDENCE.
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
            response = requests.get(f'http://ip-api.com/json/{ip_address}?fields=status,message,countryCode,region,city,lat,lon,isp,org,query')
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
    except requests.exceptions.RequestException:
        return "No Title."

derive_schemes = ('https://', 'http://')

# requests announces itself as "python-requests/x.y.z", which Cloudflare's default
# bot rules reject outright. Measured on elementex.tech (server=cloudflare): the
# identical HTTPS request returns 403 / 5,426 bytes with the library's own header
# and 200 / 168,969 bytes with this one. The only variable is the User-Agent, and
# the site was being filed as 'unavailable.' despite being live and fetchable.
FETCH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}

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
    worth looking at. urllib3's per-request warning is silenced at import.
    """
    global less_words_domains
    def extract_js_libraries(html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        script_tags = soup.find_all('script', src=True)
        return [tag['src'] for tag in script_tags]

    response = None
    for scheme in derive_schemes:
        try:
            response = requests.get(scheme + url_name, timeout=10, verify=False,
                                    headers=FETCH_HEADERS)
            break
        except Exception:
            continue          # TLS failure, refused, timeout -> try the next scheme
    if response is None:
        return False

    try:
        if str(response.status_code)[0] not in ['4', '5']:
            html_content = response.text
            plain_text = str(text_converter.handle(response.text)).split()
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

    # CRITICAL FIX: Merge the Gemini evaluation into the final log dictionary
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

    # Pass 1: Extraction via Lightweight HTML DOM
    start_eval = time.time()
    raw_dom_text = extract_dom_text(html_content)
    clean_text = sanitize_text(raw_dom_text)
    used_ocr = False

    # Evasion Detection Check: Fall back to visual processing if DOM text is suspiciously thin
    if len(clean_text) < 150:
        log(f"{url_name} - DOM text too short ({len(clean_text)} chars). Running OCR Fallback.", curr_date, 'hybrid_fallback.txt', 'a')
        used_ocr = True

        # Trigger heavy Selenium process
        try:
            ss = selenium_obj.take_screenshot(url_name, curr_date, sspath, SYSNO)
            if not ss:
                if os.path.exists(sspath): os.system(f'rm -rf {sspath}')
                end_eval = time.time()
                log(f"{url_name},{end_eval-start_eval}", curr_date, 'time_screenshot.txt', 'a')
                return f"screenshot failure."
        except Exception as e:
            log(f"{url_name},selenium_obj.take_screenshot (),{e}", curr_date, 'errors.txt', 'a')
            if os.path.exists(sspath): os.system(f'rm -rf {sspath}')
            end_eval = time.time()
            log(f"{url_name},{end_eval-start_eval}", curr_date, 'time_screenshot.txt', 'a')
            return "screenshot failure"

        # Trigger heavy Tesseract extraction
        raw_ocr_text = OCR(url_name, curr_date, html_content)
        if not raw_ocr_text:
            if os.path.exists(sspath): os.system(f'rm -rf {sspath}')
            end_eval = time.time()
            log(f"{url_name},{end_eval-start_eval}", curr_date, 'time_OCR.txt', 'a')
            return f"OCR failure."

        clean_text = sanitize_text(raw_ocr_text)
   # Pass 2: Classification Engine Call with Content Filter
    # content_filter still runs first: it is free, and at ~98.7% rejection it keeps
    # the Ollama call (the expensive step on CPU-only hardware) off most pages.
    if used_ocr or content_filter(clean_text):
        evaluation = evaluate_hybrid(clean_text, url_name, curr_date)
    else:
        evaluation = {"is_scam": False, "confidence": 0, "reason": "Cleared_By_Content_Filter"}

    if evaluation.get("error"):
        # FAIL CLOSED. Not classified either way. Screenshots kept for the retry pass.
        log(f"{url_name}\t{evaluation.get('reason')}\t(OCR Fallback: {used_ocr})",
            curr_date, 'needs_retry.txt', 'a')
        result_msg = "Needs retry (API error)."
    elif (evaluation.get("is_scam")
          and evaluation.get("reason", "").lower() in UNREADABLE):
        log(f"{url_name}\tflagged_but_unreadable\t{evaluation.get('reason')}",
            curr_date, 'needs_retry.txt', 'a')
        result_msg = "Flagged on unreadable text - needs retry."
    elif evaluation.get("is_scam") and evaluation.get("confidence", 0) > SCAM_CONFIDENCE_BAR:
        log(f"Confirmed Scam [{evaluation.get('model', '?')}] (OCR Fallback: {used_ocr}): "
            f"{url_name} - Reason: {evaluation.get('reason')}",
            curr_date, 'llm_scams.txt', 'a')
        handlePositives(url_name, clean_text, js_libraries, html_content, sspath, curr_date, evaluation)
        result_msg = "Scam Found & Confirmed!"

    else:
        log(f"Rejected [{evaluation.get('model', 'local')}] (OCR Fallback: {used_ocr}): "
            f"{url_name} - Reason: {evaluation.get('reason')}",
            curr_date, 'llm_rejected.txt', 'a')
        if os.path.exists(sspath):
            os.system(f'rm -rf {sspath}')
        result_msg = "Safe / Rejected."
    end_eval = time.time()
    # Log execution times back to standard diagnostic endpoints
    metric_file = 'time_OCR.txt' if used_ocr else 'time_screenshot.txt'
    log(f"{url_name},{end_eval-start_eval}", curr_date, metric_file, 'a')
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

def main():
    def timeout_handler(signum, frame):
        raise TimeoutError("Function execution timed out")
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
            #   fetch 10s + Selenium 2x20s + Ollama 90s (OLLAMA_TIMEOUT)
            #   + OpenAI 30s x3 (OPENAI_TIMEOUT x 1+OPENAI_MAX_RETRIES)  ~= 230s
            # At the old 120s an escalated page on the OCR path tripped this and was
            # filed as Worker_Timeout even though both engines answered correctly.
            # Lower this once the workers have AVX2 and Ollama returns in <10s.
            signal.alarm(240)
            try:
                result = check(url_name, curr_date)
                sys.stdout.flush()
            except TimeoutError as e:
                log(f"{url_name},[x] Timeout occurred,{e}", curr_date, 'errors.txt', 'a')
                log(f"{url_name}\tWorker_Timeout", curr_date, 'needs_retry.txt', 'a')
                result = "Timeout"
            signal.alarm(0)
            log(f"[x] Done {url_name} {result}", curr_date, 'worker.txt', 'a')
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
