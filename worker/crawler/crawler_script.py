
import argparse
import base64
import html
import json
import logging
import os
import random
import re
from curl_cffi import requests as curl_requests
import string
import sys
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    ElementNotInteractableException, NoSuchElementException,
    TimeoutException, WebDriverException, NoAlertPresentException,
    UnexpectedAlertPresentException, StaleElementReferenceException
)
from hyip_classifier import MAX_PAGES, SiteEvidence, classify, clear_b, verdict_line


# Global variables
screenshot_counter = 1
should_continue = True

#Directory for logs
logs_directory = 'logs'
os.makedirs(logs_directory, exist_ok=True)
log_file_path = os.path.join(logs_directory, 'logfile.log')
email_used = None
blacklisted_urls = ["facebook.com", "example.com","linkedin.com","paypal.com"]

# --- Paths & credentials --------------------------------------------------
# Resolve input files relative to THIS file so the crawler works no matter what
# working directory systemd/cron launches it from, and so it always reads the
# same urls.txt that feeder.py appends to.
HERE = os.path.dirname(os.path.abspath(__file__))
NAMES_FILE = os.path.join(HERE, 'other', 'names.txt')        # dictionary of common names
URLS_FILE = os.path.join(HERE, 'urls.txt')                   # crawler input, produced by feeder.py
CRAWLED_URLS_FILE = os.path.join(HERE, 'crawled_urls.txt')   # single-URL resume checkpoint
# Mailinator email-confirmation is optional; set these env vars to enable it.
# On the Crimson VMs they come from ~/.crimson_env.systemd via the unit's
# EnvironmentFile, so the API token never lives in this file or the repo.
EMAIL_DOMAIN = "<MAILINATOR_DOMAIN>"
MAILINATOR_API_KEY =  "<MAILINATOR_API_KEY>"
# "private" addresses the account's private domain (EMAIL_DOMAIN) in the v2 API.
MAILINATOR_API = 'https://api.mailinator.com/api/v2/domains/private'
MAILINATOR_WAIT_SECONDS = 60   # how long to poll the sign-up inbox for a confirmation email
# The password every sign-up form gets, and therefore the one login must use.
# Login used to send the literal string "PASSWORD", so no login could succeed.
# 'Pass@1234' was refused as too weak by sign-up checks against breached-password
# lists (swifttradinginc.live, Oct 24); this one is long and not a common password.
ACCOUNT_PASSWORD = '<CRAWLER_PASSWORD>'
# Chromium location. The default matches the Crimson worker nodes (snap Chromium shim), the
# same path recv.py uses. Override via env for other environments. SeleniumBase downloads a
# chromedriver that matches the browser, so no chromedriver path is set.
CHROME_BINARY = os.environ.get('CHROME_BIN', '/usr/bin/chromium-browser')
# Chrome runs headed, not headless: headless Chrome announces itself as "HeadlessChrome" in its
# User-Agent, which is what most Cloudflare walls stopped (Oct 20). On Linux the headed window
# goes to an Xvfb virtual display (see start_virtual_display); --show-browser skips Xvfb.
WINDOW_SIZE = (1920, 1080)
use_xvfb = True
# --------------------------------------------------------------------------

# Configure logging
logging.basicConfig(filename=log_file_path, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
sys.stdout = open(log_file_path, 'a', buffering=1)
sys.stderr = open(log_file_path, 'a', buffering=1)
logging.info("Script started.")

# Xvfb virtual display for the headed browser on Linux. SeleniumBase's Driver() does not start
# one itself (only its pytest and SB() runners do), so it is started here, once per run, before
# the first browser. Windows has no Xvfb; there the browser window is shown.
virtual_display = None

def start_virtual_display():
    global virtual_display
    if not use_xvfb or not sys.platform.startswith('linux'):
        return
    from sbvirtualdisplay import Display
    virtual_display = Display(visible=False, size=WINDOW_SIZE, backend="xvfb")
    virtual_display.start()
    logging.info(f"Xvfb virtual display started ({WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}, DISPLAY={os.environ.get('DISPLAY')})")

def stop_virtual_display():
    if virtual_display is not None:
        try:
            virtual_display.stop()
        except Exception as e:
            logging.error(f"Failed to stop the Xvfb display: {e}")

# Handle pop ups and alerts. alert/confirm/prompt are replaced so they never block the browser; each
# message is also kept in sessionStorage (the last 10), which page_state() reads, because some sites
# report the sign-up result only in an alert: profitpulsecapitals.com (ASP.NET) answered the sign-up
# with alert("Your Registration was successful."), and the unhandled alert ended the crawl (Oct 29).
# The override used to be set on the first page only; prepare_tab() installs it for every new page.
DIALOG_OVERRIDE_JS = r"""
(() => {
  const note = (kind, message) => {
    try {
      const saved = JSON.parse(sessionStorage.getItem('__crawler_dialogs') || '[]');
      saved.push(String(message).slice(0, 300));
      sessionStorage.setItem('__crawler_dialogs', JSON.stringify(saved.slice(-10)));
    } catch (e) {}
    console.log(kind + ' called with message: ' + message);
  };
  window.alert = message => { note('Alert', message); };
  window.confirm = message => { note('Confirm', message); return true; };
  window.prompt = (message, defaultText) => { note('Prompt', message); return defaultText; };
})();
"""

def handle_alert(driver):
    try:
        driver.execute_script(DIALOG_OVERRIDE_JS)
        logging.info("Alert functions overridden to log messages to the console.")
    except Exception as e:
        logging.error(f"Failed to override alert functions: {e}")

# Initialize driver function
#
# A SeleniumBase Driver, headed (headed=True also stops SeleniumBase from switching to headless
# on Linux), in UC mode. It is a Selenium WebDriver, so the rest of the script uses it unchanged.
# The flags are the ones the plain Selenium setup used; SeleniumBase sets most of them too.
# On ipfighter.com/bot-detection (Oct 29) plain headed SeleniumBase scored 94%, flagged for
# navigator.webdriver, domAutomationController and chrome.runtime. UC mode cleared the first two
# but made document.hasFocus() false; focus emulation (emulate_focus) fixed that: 98%, only
# chrome.runtime left.
#
# UC mode replaces driver.get(): it fetches the URL with Python requests first and, on a
# redirect, an error status or a captcha, opens the page in a new tab and closes the current
# one. The crawler tracks its tabs by handle (original_window), so the plain get() is put back;
# bot walls are handled where pages load instead (pass_bot_wall).
def initialize_driver():
    driver = Driver(
        browser="chrome",
        uc=True,
        headed=True,
        binary_location=CHROME_BINARY if os.path.exists(CHROME_BINARY) else None,
        locale_code="en-US",
        window_size=f"{WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
        chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-popup-blocking,--disable-translate",
    )
    if hasattr(driver, "default_get"):
        driver.get = driver.default_get
    prepare_tab(driver)
    return driver

# document.hasFocus() is false in UC mode, a sign bot checks look for. Chrome's focus emulation
# makes the page report focus. It holds per tab, so every new tab gets it too.
def emulate_focus(driver):
    try:
        driver.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
    except Exception as e:
        logging.info(f"Could not turn on focus emulation: {e}")

# Settings every tab needs: focus emulation, and the dialog override for the page already shown
# (handle_alert) and for every page loaded in the tab from now on. Both last only as long as the
# driver session, so reconnect_reload() calls this again on every tab. A tab opened with
# window.open() gets them once the crawler switches to it, after its first page has started loading.
def prepare_tab(driver):
    emulate_focus(driver)
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": DIALOG_OVERRIDE_JS})
    except Exception as e:
        logging.info(f"Could not install the dialog override for new pages: {e}")
    handle_alert(driver)

# --- Bot walls and Cloudflare Turnstile -------------------------------------------------------
# A bot wall is a whole page put in front of the site ("Just a moment...", "Verify you are human",
# "Performing security verification"); a Turnstile can also sit inside the sign-up form, which
# check_captcha() used to skip the site for. A wall is passed in up to three steps, stopping as
# soon as the site shows: wait for it to clear on its own; reload the page with chromedriver
# disconnected (the UC mode trick: the check runs while no driver is attached); click the
# Turnstile box with SeleniumBase's uc_gui_click_captcha(), which moves the real mouse (on Linux
# inside Xvfb; on Windows the desktop mouse). A Cloudflare block page ("Sorry, you have been
# blocked", error 1020) has nothing to pass and is only logged.
BOT_WALL_JS = r"""
const title = (document.title || '').trim();
const text = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim();
const html = document.documentElement ? document.documentElement.outerHTML : '';
const cf = /\/cdn-cgi\/challenge-platform\/|challenges\.cloudflare\.com|cf-chl|cf_chl_opt/i.test(html);
if (text.length < 3000 && /sorry, you have been blocked|error 1020|you are unable to access/i.test(text) && (cf || /cloudflare|ray id/i.test(text)))
  return {kind: 'blocked', title: title, text: text.slice(0, 200)};
if (/^(just a moment|please wait|attention required|one more step|ddos-guard|checking your browser)/i.test(title)
    || (text.length < 1500 && /verify(ing)? (that )?you are (a )?human|checking (if the site connection is secure|your browser)|enable javascript and cookies to continue|performing security verification/i.test(text)))
  return {kind: 'challenge', title: title, cf: cf, text: text.slice(0, 200)};
return null;
"""
TURNSTILE_TOKEN_JS = "return Array.from(document.querySelectorAll('input[name=\"cf-turnstile-response\"]')).some(e => e.value);"

def bot_wall(driver):
    try:
        return driver.execute_script(BOT_WALL_JS)
    except Exception:
        return None  # mid-navigation; the next read decides

def wait_for_js(driver, script, seconds):
    """True once script returns something truthy, polled every second for up to `seconds`."""
    for _ in range(seconds):
        try:
            if driver.execute_script(script):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False

def wall_cleared(driver, seconds):
    for _ in range(seconds):
        time.sleep(1)
        if bot_wall(driver) is None:
            return True
    return False

def reconnect_reload(driver, wait_seconds=6):
    """Reload the current tab while chromedriver is disconnected, then reconnect to the same tab.
    Reconnecting starts a new driver session, which drops focus emulation on every tab."""
    handle = driver.current_window_handle
    driver.execute_script("setTimeout(() => location.reload(), 50);")
    driver.reconnect(wait_seconds)
    for h in driver.window_handles:
        driver.switch_to.window(h)
        prepare_tab(driver)
    driver.switch_to.window(handle)

def pass_bot_wall(driver):
    """True when the current page is not (or no longer) a bot wall."""
    wall = bot_wall(driver)
    if wall is None:
        return True
    url = driver.current_url
    if wall['kind'] == 'blocked':
        logging.info(f"Bot wall on {url}: blocked page ({wall['title']!r}: {wall['text'][:120]!r}), nothing to pass")
        return False
    logging.info(f"Bot wall on {url}: {wall['title']!r}" + (" (Cloudflare)" if wall.get('cf') else "") + f": {wall['text'][:120]!r}")
    if wall_cleared(driver, 10):
        logging.info(f"Bot wall on {url}: passed on its own, now at {driver.current_url}")
        return True
    try:
        reconnect_reload(driver)
        if wall_cleared(driver, 8):
            logging.info(f"Bot wall on {url}: passed after a reload with chromedriver disconnected, now at {driver.current_url}")
            return True
        driver.uc_gui_click_captcha()
        if wall_cleared(driver, 10):
            logging.info(f"Bot wall on {url}: passed after clicking its checkbox, now at {driver.current_url}")
            return True
    except Exception as e:
        logging.info(f"Bot wall on {url}: error while passing it: {e}")
    logging.info(f"Bot wall on {url}: still there, giving up ({bot_wall(driver) or 'page unreadable'})")
    return False

def solve_turnstile(driver):
    """(solved, how) for a Turnstile widget on the current page, judged by its response token."""
    if wait_for_js(driver, TURNSTILE_TOKEN_JS, 6):
        return True, "passed on its own"
    try:
        driver.uc_gui_click_captcha()
    except Exception as e:
        return False, f"clicking its checkbox failed ({e})"
    if wait_for_js(driver, TURNSTILE_TOKEN_JS, 10):
        return True, "passed after clicking its checkbox"
    return False, "no token after clicking its checkbox"

driver = None  # created in main(); fill_form() and the URL loop use this global
# The urls.txt line being crawled. login_and_take_screenshot() writes it to
# source_url.txt in the site's folder, because the folder is named after the login
# URL, which can be on another host than the site feeder.py gave the crawler.
# build_wallets.py reads it to tie each wallet to its domain.
current_site_url = None
# Set by login_and_take_screenshot() when the login is not judged failed, so the
# per-site "Success" line means logged in, not merely that a form was filled.
logged_in = False

# HYIP (A) or other scam (B), decided in hyip_classifier.py from what the crawl sees. The crawl
# hands it each page it loads and what it finds (sign-up form fields, lead forms, off-site sign-up,
# login, back-office links, deposit plan controls, wallets); log_site_result() writes the verdict.
# A site with hard evidence of another product or a hand-off (lead form, prop-firm challenge,
# subscription, hash rate) is stopped before an account is made. --classify-only stops every site
# there, so a labelled CSV can be checked without making accounts.
site_evidence = SiteEvidence('')
classify_only = False
single_site = False  # --url: the evidence file goes in the run folder as hyip_evidence.json

def read_page(driver, kind=None):
    """Give the page the browser is on to the HYIP evidence (the first MAX_PAGES pages of the site)."""
    url = driver.current_url
    if url.startswith('chrome-error') and not site_evidence.read:
        site_evidence.unreachable = site_evidence.unreachable or "the browser showed its own error page"
    if not url.startswith('http') or not site_evidence.wants_page(url):
        return
    try:
        site_evidence.add_page(driver.page_source, url, kind or ('home' if not site_evidence.pages else 'page'))
    except Exception as e:
        logging.info(f"HYIP check: could not read {url}: {str(e).splitlines()[0] if str(e) else e!r}")

def stop_if_clearly_b(where, at_form=False):
    """True (and logged) when the evidence so far says B for sure. At a sign-up form (at_form), a
    classify-only run stops too."""
    why = clear_b(site_evidence)
    if why:
        site_evidence.stopped_early = why
        logging.info(f"HYIP check: clearly B ({'; '.join(why)}); not signing up ({where})")
        return True
    if classify_only and at_form:
        site_evidence.stopped_early = ['classify-only run']
        logging.info(f"HYIP check: classify-only run; not signing up ({where})")
        return True
    return False

# Generate random strings and emails for filling forms
def generate_random_string(length=8, names_file=NAMES_FILE): #names.txt contains a dictionary of common names
    with open(names_file, 'r') as file:
        names = [line.strip() for line in file.readlines()]
    random_name = random.choice(names)
    result_string = f"{random_name}"
    return result_string[:length]

def generate_random_email():
    random_number = random.randint(60, 99)
    username = generate_random_string().lower()
    return f"{username}{random_number}@{EMAIL_DOMAIN}"

# Generate crypto addresses
def generate_btc_address():
    return random.choice(['1', '3']) + ''.join(random.choices(string.digits + string.ascii_letters, k=33))

def generate_eth_address():
    return '0x' + ''.join(random.choices('0123456789abcdef', k=40))

def generate_ltc_address():
    return random.choice(['L', 'M']) + ''.join(random.choices(string.digits + string.ascii_letters, k=33))

def generate_xrp_address():
    return 'r' + ''.join(random.choices(string.digits + string.ascii_letters, k=33))

def generate_xmr_address():
    return '4' + ''.join(random.choices(string.digits + string.ascii_letters, k=94))

def generate_ada_address():
    return 'addr1' + ''.join(random.choices(string.digits + string.ascii_letters, k=58))

# --- Captcha check -----------------------------------------------------------------------
# has_captcha() used to give up on a site whenever the word "captcha" appeared anywhere in the
# HTML. Re-checking the 93 sites it stopped between Oct 17 and Oct 20 (87 loaded): 28 showed no
# captcha at all (the word sat in comments, CSS, config strings or class names), 14 had a
# site-built text or math captcha whose answer is on the page ("8 × 8 = ?", "Enter Captcha:
# B730FF"), 30 had a math captcha in a hidden later step of a multi-step form, 6 had an image
# captcha and 9 a third-party widget. Every real one sat inside the sign-up form. So now only
# what the sign-up form shows counts, and text and math captchas are answered. Image captchas
# (OCR) and multi-step forms are left for later; both are logged when seen.
CAPTCHA_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const forms = Array.from(document.forms);
const scope = forms.find(f => f.querySelectorAll('input[type=password]').length >= 2)
           || forms.find(f => f.querySelector('input[type=password]')) || document;
const q = s => Array.from(scope.querySelectorAll(s));
const widgets = [];
if (q('iframe[src*="recaptcha/api2/anchor"],iframe[src*="recaptcha/enterprise/anchor"]').some(e => vis(e) && !/size=invisible/.test(e.src))
    || q('.g-recaptcha[data-sitekey]').some(e => vis(e) && e.getAttribute('data-size') !== 'invisible')) widgets.push('reCAPTCHA v2');
if (q('.h-captcha,iframe[src*="hcaptcha.com"]').some(vis)) widgets.push('hCaptcha');
if (q('.cf-turnstile,iframe[src*="challenges.cloudflare.com"]').some(vis)) widgets.push('Cloudflare Turnstile');
if (q('altcha-widget').some(vis)) widgets.push('ALTCHA');
const invisible = !!document.querySelector('.grecaptcha-badge,iframe[src*="size=invisible"]')
               || /recaptcha\/(api|enterprise)\.js\?[^"']*render=(?!explicit)/i.test(document.documentElement.innerHTML);
const images = q('img').filter(e => vis(e) && /captcha|turing|validation_image|securimage|antibot/i.test(
    (e.getAttribute('src') || '') + ' ' + e.id + ' ' + String(e.className) + ' ' + (e.alt || '')));
const texts = e => {
    const out = [e.placeholder || ''];
    if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) out.push(l.innerText || l.textContent); }
    for (let p = e.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) out.push(p.innerText || p.textContent || '');
    return out.map(t => t.replace(/\s+/g, ' ').trim().slice(0, 300)).filter(Boolean);
};
const inputs = q('input').filter(e => e.type !== 'hidden'
    && /captcha|validation_number|turing|security.?code|verif\w*.?code|antibot|math/i.test((e.name || '') + ' ' + e.id + ' ' + (e.placeholder || '')));
return {widgets: widgets, invisible: invisible, images: images.length,
        inputs: inputs.map(e => ({name: e.name, id: e.id, visible: vis(e), texts: texts(e)}))};
"""
CAPTCHA_MATH_RE = re.compile(r"(\d{1,3})\s*(\+|-|−|x|×|\*|÷|/|plus|minus|times)\s*(\d{1,3})\s*(?:=|\?)", re.I)
CAPTCHA_SPACED_RE = re.compile(r"(?<!\w)\d(?: \d){3,7}(?!\w)")                    # "8 9 5 7 1 3"
CAPTCHA_CODE_RE = re.compile(r"\b(?=[A-Z0-9]*\d)[A-Z0-9]{4,8}\b")                 # "B730FF", "0EAAB3"
# A code right after "code/captcha/token ...:" may be letters only: fxtastytrades.com showed
# "Enter the code below: PCULLW" (another load: "X7DPDX"), which CAPTCHA_CODE_RE needs a digit for.
CAPTCHA_CUED_RE = re.compile(r"(?i:code|captcha|token)(?:\s+(?i:below|above))?\s*[:：]\s*([A-Z0-9]{4,8})\b")
CAPTCHA_OPS = {"+": lambda a, b: a + b, "plus": lambda a, b: a + b, "-": lambda a, b: a - b, "−": lambda a, b: a - b,
               "minus": lambda a, b: a - b, "x": lambda a, b: a * b, "×": lambda a, b: a * b, "*": lambda a, b: a * b,
               "times": lambda a, b: a * b, "÷": lambda a, b: a // b, "/": lambda a, b: a // b}

def solve_text_captcha(texts):
    """(answer, challenge) for a text or math captcha, read from the texts around its input
    (placeholder, label, then up to four parent elements, nearest first), or (None, None)."""
    for t in texts:
        m = CAPTCHA_MATH_RE.search(t)
        if m and not (m.group(2) in ("÷", "/") and int(m.group(3)) == 0):
            return str(CAPTCHA_OPS[m.group(2).lower()](int(m.group(1)), int(m.group(3)))), m.group(0)
        m = CAPTCHA_SPACED_RE.search(t)
        if m:
            return m.group(0).replace(" ", ""), m.group(0)
        m = CAPTCHA_CUED_RE.search(t)
        if m:
            return m.group(1), m.group(0)
        m = CAPTCHA_CODE_RE.search(t)
        if m:
            return m.group(0), m.group(0)
    return None, None

def check_captcha(driver):
    """What captcha the sign-up form shows. Returns (blocking, summary, answer, target): blocking
    means the site is skipped; answer and target ((By, value) of the input) are set when a text or
    math captcha was read, and the answer is typed in just before the form is submitted."""
    time.sleep(2)  # third-party widgets render after load
    try:
        c = driver.execute_script(CAPTCHA_JS)
    except Exception as e:
        return False, f"could not inspect the page ({e}), trying anyway", None, None
    if c["widgets"] == ["Cloudflare Turnstile"]:
        solved, how = solve_turnstile(driver)
        if solved:
            return False, f"Cloudflare Turnstile on the sign-up form, {how}", None, None
        return True, f"Cloudflare Turnstile on the sign-up form, {how}, skipping the site", None, None
    if c["widgets"]:
        return True, f"{' and '.join(c['widgets'])} on the sign-up form, skipping the site", None, None
    visible = [i for i in c["inputs"] if i["visible"]]
    if visible:
        i = visible[0]
        answer, challenge = solve_text_captcha(i["texts"])
        if answer is not None:
            target = (By.ID, i["id"]) if i["id"] else (By.NAME, i["name"])
            return False, f"text captcha {challenge!r}, answering {answer!r}", answer, target
        if c["images"]:
            return True, "image captcha on the sign-up form (OCR not implemented), skipping the site", None, None
        return True, f"captcha input {i['name'] or i['id']!r} whose challenge could not be read {i['texts'][:2]}, skipping the site", None, None
    if c["images"]:
        return True, "image captcha on the sign-up form (OCR not implemented), skipping the site", None, None
    notes = (["a captcha input in a hidden part of the form (multi-step form?)"] if c["inputs"] else []) + \
            (["an invisible reCAPTCHA"] if c["invisible"] else [])
    return False, (f"none visible ({' and '.join(notes)}), trying anyway" if notes else "none"), None, None

def answer_captcha(driver, answer, target):
    """Type the captcha answer, replacing whatever predict_values() put in the field."""
    try:
        field = driver.find_element(*target)
        field.clear()
        field.send_keys(answer)
        logging.info(f"Captcha answer {answer!r} entered")
    except Exception as e:
        logging.info(f"Could not enter the captcha answer: {e}")

# The fields that belong to a form: form.elements, not the elements inside its markup.
# amvinvestmentltd.com's register page has broken HTML that closes the <form> after its first
# two fields, so the other ten (password and submit button included) sit outside the <form>
# element but still belong to the form; the sign-up form was skipped as having no password field.
# "form" can also be the box of a sign-up without a <form> element (see signup_box), which has no
# .elements; then the controls inside it count. CONTROLS_JS is that rule as a JavaScript expression.
CONTROLS_JS = "(f => f.elements ? Array.from(f.elements) : Array.from(f.querySelectorAll('input, select, textarea, button')))"

def form_controls(form, css='input, select, textarea, button'):
    return form.parent.execute_script(
        f"return {CONTROLS_JS}(arguments[0]).filter(e => e.matches(arguments[1]));", form, css)

# Fields are keyed by name, or "#id", or "@n" (position among the form's inputs) when they have
# neither: duduinvest.com's and expacraftcapitals.org's sign-up fields have no names (filled by the
# page's own script, Livewire on expacraftcapitals), so nothing was typed and the form went out empty.
FIELD_BY_KEY_JS = f"const els = {CONTROLS_JS}(arguments[0]), k = arguments[1];" + r"""
if (k.startsWith('#')) return els.find(e => e.id === k.slice(1)) || null;
if (k.startsWith('@')) return els.filter(e => e.tagName === 'INPUT')[parseInt(k.slice(1))] || null;
return els.find(e => e.name === k) || null;
"""
# name, id, placeholder, autocomplete, aria-label and label text of each input, for choosing values.
FIELD_HINTS_JS = f"return {CONTROLS_JS}(arguments[0])" + r""".filter(e => e.tagName === 'INPUT').map(e => {
  const l = (e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]')) || e.closest('label');
  return [e.name || '', e.id || '', e.type || 'text', [e.name, e.id, e.placeholder, e.autocomplete, e.getAttribute('aria-label'),
          l ? l.innerText : ''].join(' ').replace(/\s+/g, ' ').toLowerCase(), e.required];
});
"""
TEXT_LIKE_TYPES = ('text', 'email', 'password', 'tel', 'number', 'url', 'search', '')
# Whole words, now that label text is part of the hints ("Tell us about you" is not a phone field).
PHONE_HINT_RE = re.compile(r"phone|mobile|whatsapp|\btel\b|\bcell\b|telephone|cellphone")

def field_by_key(form, key):
    return form.parent.execute_script(FIELD_BY_KEY_JS, form, key)

# A captcha gate: a page whose only form is a captcha (elitesterlingglobal.com/register shows
# "Security Check: 16 + 10 = ?" and the sign-up form only after it is answered). Answer it, submit
# that form, and report whether a password field is visible afterwards. Other empty required text
# boxes in the gate get the token the page shows: elitesterlingglobal.com also asks for "Your
# Authentication Token PEMJ RGJX" (typed without the space, maxlength 8).
GATE_TOKEN_RE = re.compile(r"(?i:token|code)\s*:?\s*([A-Z0-9]{2,8}(?: [A-Z0-9]{2,8})?)\b")

def pass_captcha_gate(driver, answer, target):
    try:
        field = driver.find_element(*target)
        gate = driver.execute_script("return arguments[0].form;", field)
        if gate is None or form_controls(gate, 'input[type="password"]'):
            return False
        field.clear()
        field.send_keys(answer)
        page_text = driver.execute_script("return document.body.innerText;")
        token = GATE_TOKEN_RE.search(page_text)
        for other in form_controls(gate, 'input[type="text"], input:not([type])'):
            if other != field and other.is_displayed() and not other.get_attribute('value') and other.get_attribute('required') and token:
                other.send_keys(token.group(1).replace(" ", ""))
                logging.info(f"Captcha gate: typed the token {token.group(1)!r} shown on the page")
        buttons = [b for b in form_controls(gate, 'button[type="submit"], input[type="submit"], button:not([type])') if b.is_displayed()]
        if buttons:
            driver.execute_script("arguments[0].click();", buttons[0])
        else:
            driver.execute_script("arguments[0].requestSubmit ? arguments[0].requestSubmit() : arguments[0].submit();", gate)
        time.sleep(3)
        shown = driver.execute_script("return Array.from(document.querySelectorAll('input[type=password]')).some(e => e.offsetWidth || e.offsetHeight);")
        logging.info(f"Captcha gate: answered {answer!r}, now at {driver.current_url}; "
                     + ("a password field is shown" if shown else "still no password field"))
        return bool(shown)
    except Exception as e:
        logging.info(f"Captcha gate could not be answered: {e}")
        return False

# Predict form values
def predict_values(form):
    predictions = {}
    predicted_password = password_for(form)
    # Lower case: aziminer.com refused "Florence95@..." with "The email field must be lowercase."
    # Mailinator inboxes ignore case, and the login username derived from the address stays the same.
    predicted_username = generate_random_string().lower()
    predicted_email = f"{predicted_username}{random.randint(60, 99)}@{EMAIL_DOMAIN}"

    for position, (name, field_id, input_type, hints, required) in enumerate(form.parent.execute_script(FIELD_HINTS_JS, form)):
        input_type = (input_type or 'text').lower()
        if name:
            key = name
        elif input_type in TEXT_LIKE_TYPES or input_type in DATE_INPUT_FORMATS:
            key = f"#{field_id}" if field_id else f"@{position}"
        else:
            continue  # unnamed checkboxes, buttons, hidden fields: nothing to type
        if REFERRAL_FIELD_RE.search(hints) and not required:
            continue  # left empty; see REFERRAL_FIELD_RE
        if "username" in hints or "user name" in hints:
            predictions[key] = predicted_username
        elif input_type == "password":
            predictions[key] = predicted_password
        elif input_type == "email" or "email" in hints or "e-mail" in hints:
            predictions[key] = predicted_email
        elif input_type == "tel" or PHONE_HINT_RE.search(hints):
            predictions[key] = generate_phone_number()
        elif input_type in DATE_INPUT_FORMATS:
            predictions[key] = generate_date_value(input_type)
        elif (place := place_value(hints)) is not None:
            predictions[key] = place
        else:
            predictions[key] = generate_random_string()
    return predictions

# Postal-address fields get a US address that matches the US phone numbers, not a name from
# names.txt: uphilltradex.com refused a sign-up with "The zip field is required." (Nov 1), and the
# profile form mail.nexarioncapitalventure.com puts in front of its back office asks for address,
# state, zip and city (see PROFILE_GATE_JS). A field about a wallet or payment account ("Bitcoin
# address") is not a postal address.
PAYMENT_ACCOUNT_HINT_RE = re.compile(r"wallet|btc|bitcoin|usdt|eth|tron|trx|ltc|crypto|perfect|payeer|account", re.I)

def place_value(hints):
    if PAYMENT_ACCOUNT_HINT_RE.search(hints):
        return None
    for pattern, value in ((r"zip|postal|post.?code", "62704"), (r"country|nationality", "United States"),
                           (r"\bcity\b|\btown\b", "Springfield"), (r"\bstate\b|province", "IL"),
                           (r"address|street|addr\b", "1458 Maple Avenue")):
        if re.search(pattern, hints):
            return value
    return None

# Passwords. ACCOUNT_PASSWORD has symbols and is 14 characters long. Sites that allow only
# letters and digits, 8-12 characters, get ACCOUNT_PASSWORD_ALT: tradevistavelocityinvestmentmark.com
# checks /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z\d]{8,12}$/ in its own script and keeps "Create
# Account" disabled until it passes. The alternative is used when a password field's maxlength or
# pattern refuses ACCOUNT_PASSWORD, or (see fill_sign_up_form) when the submit button stays disabled.
# password_used is the one the account got, for the login.
ACCOUNT_PASSWORD_ALT = '<CRAWLER_PASSWORD_ALT>'
password_used = ACCOUNT_PASSWORD

def password_for(form):
    refused = form.parent.execute_script(f"const pw = arguments[1]; return {CONTROLS_JS}(arguments[0])" + r""".filter(e => e.type === 'password').some(e => {
  if (e.maxLength > 0 && pw.length > e.maxLength) return true;
  if (e.pattern) { try { return !new RegExp('^(?:' + e.pattern + ')$').test(pw); } catch (x) { return false; } }
  return false;
});""", form, ACCOUNT_PASSWORD)
    return ACCOUNT_PASSWORD_ALT if refused else ACCOUNT_PASSWORD

def submit_disabled(form):
    """True when the form's submit buttons are all disabled (a script is holding the form back)."""
    return form.parent.execute_script(f"const b = {CONTROLS_JS}(arguments[0])" + r""".filter(e => e.matches('button[type=submit], input[type=submit], button:not([type])'));
return b.length > 0 && b.every(e => e.disabled);""", form)

# Optional referral-type fields stay empty. A made-up value is checked against real codes:
# beacondigital.ltd answered POST /api/register with 422 "The selected referral code is invalid."
# because "referralCode" got a name from names.txt. A required one still gets a value. Words are
# matched from their start, since label text is in the hints ("Preferred currency" is not one).
REFERRAL_FIELD_RE = re.compile(r"\brefer|\bref_?by|\bref_?code|\bref\b|promo|invite|invitation|sponsor|coupon|voucher|upline", re.I)

# Phone fields used to get a name from names.txt, which forms with phone validation refuse
# (swifttradinginc.live: name="phone", type="tel"); predict_values() uses type and PHONE_HINT_RE.
# Digits only: tradevistavelocityinvestmentmark.com requires /^\d{10,15}$/ and "+1 555 ..." failed.
# Eleven digits still read as a US number where the "+1 555" form was accepted.
def generate_phone_number():
    return f"1555{random.randint(200, 999)}{random.randint(1000, 9999)}"

# Date inputs (minnercoinex.org's required type="date" "dob") used to get a name from names.txt,
# which a date field ignores, so the form was refused with "dob: Please fill out this field".
# They get an adult's birth date in the value format the input type takes; fill_form() sets it
# with JavaScript because typed dates depend on the browser's locale.
DATE_INPUT_FORMATS = {'date': '%Y-%m-%d', 'datetime-local': '%Y-%m-%dT%H:%M', 'month': '%Y-%m', 'week': '%G-W%V', 'time': '%H:%M'}

def generate_date_value(input_type):
    moment = datetime(random.randint(1965, 1998), random.randint(1, 12), random.randint(1, 28), 10, 30)
    return moment.strftime(DATE_INPUT_FORMATS[input_type])

# Pick the form to sign up with. The first <form> on the page used to be taken, which on
# pages holding both a login and a sign-up form (cryptocoinprop.com /login/ and /register/,
# where one is a hidden tab) was the login form. Prefer a visible form with a password field
# that looks like sign-up: more visible fields, a second password, a terms box, a sign-up
# word in its action/id/class or button, and no login word. A form that looks like login
# (a login word, no sign-up word, one password field) is skipped, so on /login/ with the
# sign-up tab hidden the crawl moves on to /register/ instead of filling the login form.
def pick_signup_form(driver):
    best, best_score = None, None
    for form in driver.find_elements(By.TAG_NAME, 'form'):
        try:
            visible = [e for e in form_controls(form, 'input, select, textarea')
                       if e.get_attribute('type') != 'hidden' and e.is_displayed()]
            passwords = [e for e in visible if e.get_attribute('type') == 'password']
            if not passwords:
                # A multi-step form shows its password fields only after "Next"
                # (account.securenovatrade.com/register: step 1 is username, name, email, phone).
                if not (visible and form_controls(form, 'input[type="password"]') and next_step_buttons(form)):
                    continue
                passwords = form_controls(form, 'input[type="password"]')
            words = ' '.join((form.get_attribute(a) or '') for a in ('action', 'id', 'class', 'name')).lower()
            words += ' ' + ' '.join(((b.text or b.get_attribute('value') or '')).lower() for b in form_controls(form, 'button, input[type="submit"]'))
            signup_word = any(k in words for k in ('register', 'signup', 'sign-up', 'sign up', 'create', 'join'))
            login_word = any(k in words for k in ('login', 'log in', 'signin', 'sign-in', 'sign in'))
            if login_word and not signup_word and len(passwords) < 2:
                continue
            score = len(visible) + 3 * (len(passwords) >= 2) + 3 * signup_word - 3 * login_word
            score += 1 * any(e.get_attribute('type') == 'checkbox' for e in visible)
            if best_score is None or score > best_score:
                best, best_score = form, score
        except StaleElementReferenceException:
            continue
    return best

# Sign-ups without a <form> element: aegisinvestmentgroup.com/auth is a React page whose fields
# (firstName ... password, password2, agreeTerms) and "Create account" button sit in plain <div>s,
# and the crawler waited 5 s for a <form> that never came, on six URL variants. The box is the
# nearest ancestor of the first visible password field outside any form that also holds three or
# more visible inputs and a button labelled like sign-up. The rest of the sign-up code takes it as
# the form (form_controls falls back to the inputs inside it).
SIGNUP_BUTTON_RE = re.compile(r"^\s*(create( an| my| your)? account|create|sign ?up|register|join( now)?|get started|open( an)? account|submit)\b", re.I)
SIGNUP_BOX_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const button = new RegExp(arguments[0], 'i');
const pw = Array.from(document.querySelectorAll('input[type=password]')).find(e => vis(e) && !e.form);
for (let box = pw ? pw.parentElement : null, i = 0; box && i < 12; box = box.parentElement, i++) {
  const inputs = Array.from(box.querySelectorAll('input')).filter(e => vis(e) && e.type !== 'hidden');
  const send = Array.from(box.querySelectorAll('button, input[type=submit], input[type=button]'))
      .some(b => vis(b) && button.test((b.innerText || b.value || '').trim()));
  if (inputs.length >= 3 && send) return box;
}
return null;
"""

def signup_box(driver):
    return driver.execute_script(SIGNUP_BOX_JS, SIGNUP_BUTTON_RE.pattern)

# Sign-up forms that appear only after a click on the same page (Oct 30): cryptoffsize.com keeps its
# register form in a hidden #registerModal opened by "Create Free Account" / "Get Started"
# (onclick="openModal('register')"); tradingpreview.pro/register shows OAuth buttons and a
# "Register with Email" button that unhides the e-mail form; fedtradez.com/auth has "Sign in" /
# "Sign up" tabs (Radix) and builds the sign-up form only when its tab is clicked. Candidates are
# visible buttons, tabs and links labelled like sign-up that stay on the page (no href, "#",
# javascript:); submit buttons of forms are left out so no form is sent. Returned in page order.
REVEAL_TEXT_RE = (r"^(sign ?up|register|registration|join( now| us)?|get started|new account"
                  r"|open (an? )?(free |new |live )?account|create (an? |your |my )?(free |new |live )?account"
                  r"|(sign ?up|register|continue|join) with e ?mail)( now| here| free)?$")
REVEAL_TRIGGERS_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const text = e => (e.innerText || e.value || '').replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim();
const re = new RegExp(arguments[0], 'i');
const stays = e => {
  if (e.tagName !== 'A') return !(e.form && (e.type || '').toLowerCase() === 'submit');
  const h = (e.getAttribute('href') || '').trim();
  return h === '' || h.startsWith('#') || /^javascript:/i.test(h);
};
return Array.from(document.querySelectorAll('button, a, [role=tab], [role=button], input[type=button]'))
  .filter(e => vis(e) && !e.disabled && re.test(text(e)) && stays(e)).slice(0, 4);
"""

# A full press, not element.click(): Radix tabs (fedtradez.com) switch on pointerdown/mousedown and
# ignore a bare click event.
PRESS_JS = r"""
const e = arguments[0];
['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(n => e.dispatchEvent(
  new (n.startsWith('pointer') ? PointerEvent : MouseEvent)(n, {bubbles: true, cancelable: true, view: window, button: 0})));
"""

def reveal_signup_form(driver):
    """Press same-page sign-up triggers (see REVEAL_TRIGGERS_JS) until a sign-up form shows; return
    it, else None. One visible password field is enough here: pick_signup_form() still skips
    login-looking forms, and fedtradez.com's sign-up tab asks only for e-mail and one password."""
    try:
        triggers = driver.execute_script(REVEAL_TRIGGERS_JS, REVEAL_TEXT_RE) or []
        # Only a form the press brought out counts, not one that was already there.
        before = pick_signup_form(driver) if triggers else None
    except Exception as e:
        logging.info(f"Reveal step on {driver.current_url}: could not look for triggers: {str(e).splitlines()[0] if str(e) else e!r}")
        return None
    if not triggers:
        try:
            seen = driver.execute_script(
                "return Array.from(document.querySelectorAll('button, [role=tab]')).slice(0, 12)"
                ".map(b => (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 30) + (b.offsetWidth || b.offsetHeight ? '' : ' (hidden)'))")
        except Exception:
            seen = []
        logging.info(f"Reveal step on {driver.current_url}: no sign-up button or tab to press; buttons on the page: {seen}")
    for trigger in triggers:
        try:
            label = (trigger.text or trigger.get_attribute('value') or '').strip()[:40]
            driver.execute_script(PRESS_JS, trigger)
            time.sleep(1.5)
            form = pick_signup_form(driver) or signup_box(driver)
            shown = driver.execute_script(VISIBLE_PASSWORDS_JS)
            new = form is not None and shown >= 1 and (before is None or form != before)
            logging.info(f"Reveal step on {driver.current_url}: pressed {label!r}; {shown} visible password field(s)"
                         + ("; a sign-up form is shown" if new else ""))
            if new:
                return form
        except (StaleElementReferenceException, WebDriverException) as e:
            logging.info(f"Reveal step: a trigger could not be clicked: {str(e).splitlines()[0] if str(e) else e!r}")
    return None

# Multi-step sign-up forms: visible buttons that move to the next step. Submit buttons are left
# out so this never sends the form early.
NEXT_STEP_RE = re.compile(r"^\s*(next|continue|next step|proceed)\b", re.I)

# In a box without a <form> (signup_box) a button with no type attribute submits nothing, so it can
# be a step button: aegisinvestmentgroup.com's "Continue" has no type, Selenium reports the default
# "submit" for it, and it was skipped. Only an explicit type="submit" is left out there.
def next_step_buttons(form):
    is_box = form.tag_name.lower() != 'form'
    def submits(b):
        return ((b.get_dom_attribute('type') or '').lower() == 'submit') if is_box else ((b.get_attribute('type') or '').lower() == 'submit')
    return [b for b in form_controls(form, 'button, input[type="button"]') + form.find_elements(By.CSS_SELECTOR, '[role="button"]')
            if not submits(b) and b.is_displayed()
            and NEXT_STEP_RE.match(b.text or b.get_attribute('value') or '')]

# A hidden password field of a login form in the same box (aegisinvestmentgroup.com's #loginPassword)
# is not a sign-up step still to come.
def is_login_field(e):
    return bool(re.search(r"log-?in|sign-?in", ' '.join((e.get_attribute(a) or '') for a in ('name', 'id')), re.I))

def advance_form_steps(driver, form, predictions, max_steps=3):
    """While the chosen form still hides its password fields or its submit button, click its
    Next button and fill the fields the new step shows. securenovatrade.com has three steps:
    contact details, passwords, then country and the submit button. Returns the steps clicked."""
    clicked = 0
    for step in range(2, max_steps + 2):
        hidden_passwords = [e for e in form_controls(form, 'input[type="password"]') if not e.is_displayed() and not is_login_field(e)]
        submit_shown = any(b.is_displayed() for b in form_controls(form, 'button[type="submit"], input[type="submit"]'))
        if not hidden_passwords and (submit_shown or not next_step_buttons(form)):
            return clicked
        buttons = next_step_buttons(form)
        if not buttons:
            logging.info("Multi-step sign-up form: password fields still hidden and no Next button")
            return clicked
        label = (buttons[0].text or buttons[0].get_attribute('value') or '').strip()
        driver.execute_script("arguments[0].click();", buttons[0])
        logging.info(f"Multi-step sign-up form: clicked {label!r} for step {step}")
        clicked += 1
        time.sleep(1.5)
        fill_form(form, predictions, only_empty=True)
        handle_dropdowns(form, driver)
    return clicked

# Fill form. Fields are looked up among the chosen form's own fields (see form_controls and
# field_by_key); a page-wide lookup by name found the hidden login form's "email" and "password"
# first on pages with two forms. only_empty is for later steps of a multi-step form, so fields
# filled in an earlier step are not typed twice.
def fill_form(form, predictions, only_empty=False):
    for input_name, prediction in predictions.items():
        input_field = field_by_key(form, input_name)
        if input_field is None:
            logging.info(f"Element {input_name} is no longer on the page")
            continue
        if only_empty and (not input_field.is_displayed() or (input_field.get_attribute('value') or '')):
            continue
        try:
            if input_field.is_displayed() and input_field.is_enabled() and (input_field.get_attribute('type') or '').lower() in DATE_INPUT_FORMATS:
                driver.execute_script("arguments[0].value = arguments[1];"
                                      "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                                      "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                                      input_field, prediction)
            elif input_field.is_displayed() and input_field.is_enabled():
                input_field.send_keys(prediction)
            else:
                logging.info(f"Element {input_name} is not interactable")
        except ElementNotInteractableException:
            logging.info(f"Element {input_name} is not interactable")

# Required <textarea> fields. predict_values() only looks at <input>s, so no text area was ever
# filled: fundamints.online/register was refused with "address: Please fill out this field" for
# its <textarea name="address" required> (Oct 30). Address-like ones get a street address.
ADDRESS_HINT_RE = re.compile(r"address|street|addr\b|residen", re.I)

def fill_textareas(form):
    for area in form_controls(form, 'textarea'):
        try:
            if not (area.is_displayed() and area.is_enabled() and area.get_attribute('required') is not None):
                continue
            if area.get_attribute('value'):
                continue
            hints = ' '.join((area.get_attribute(a) or '') for a in ('name', 'id', 'placeholder', 'aria-label'))
            area.send_keys("1458 Maple Avenue, Springfield, IL 62704" if ADDRESS_HINT_RE.search(hints)
                           else "I would like to open an account.")
            logging.info(f"Filled the required text area {area.get_attribute('name') or area.get_attribute('id') or '(unnamed)'!r}")
        except (StaleElementReferenceException, ElementNotInteractableException) as e:
            logging.info(f"Text area could not be filled: {str(e).splitlines()[0] if str(e) else e!r}")

# Handle checkboxes in the chosen form. The form is already loaded, so there is nothing to
# wait for; a 10 s wait for checkboxes used to stall every form that has none.
def handle_checkbox(driver, form):
    try:
        checkboxes = form_controls(form, 'input[type="checkbox"]')
        if not checkboxes:
            logging.info("No checkboxes in the form.")
        # A hidden (custom-styled) checkbox is ticked with a JavaScript click on the box itself, not
        # by clicking its label: hubguardfxz.com's label is "I agree with <a href="?a=rules">Terms
        # and conditions</a>", so the label click opened the rules page and the form went stale.
        # An already ticked box is left alone (clicking its label used to untick it).
        for checkbox in checkboxes:
            try:
                if checkbox.is_selected() or not checkbox.is_enabled():
                    continue
                if checkbox.is_displayed():
                    checkbox.click()
                    logging.info("Checkbox clicked directly.")
                else:
                    driver.execute_script("arguments[0].click();", checkbox)
                    logging.info("Hidden checkbox ticked using JavaScript.")
            except Exception:
                driver.execute_script("if (!arguments[0].checked) arguments[0].click();", checkbox)
                logging.info("Checkbox clicked using JavaScript.")
    except Exception as e:
        logging.error(f"An error occurred while handling checkboxes: {e}")

# Ensure driver function with enhanced error handling. A replacement is also
# stored in the module-level `driver`, which fill_form() and the URL loop use;
# otherwise they kept working with the dead browser.
def ensure_driver(drv):
    global driver
    try:
        drv.current_url
        return drv
    except (WebDriverException, Exception) as e:
        logging.info(f"WebDriver not responding, reinitializing due to error: {e}")
        try:
            logging.info("Quitting driver now")
            drv.quit()
        except Exception as inner_e:
            logging.error(f"Failed to quit the driver: {inner_e}")
        driver = initialize_driver()
        return driver

# Handle dropdowns
# Placeholder options ("Select…", "Choose…", "--") used to be picked at random like real ones.
# On swifttradinginc.live, where "Select…" is the disabled first option of country (50 options)
# and gender (5), 4 of 5 sign-ups were refused with "Please select an item in the list".
PLACEHOLDER_OPTION_RE = re.compile(r"^\s*(-+.*|select\b.*|choose\b.*|please (select|choose)\b.*|\.\.\.|…)?\s*$", re.I)

# A first option that repeats the dropdown's own name is a placeholder too, even when it is
# enabled and has a value (minnercoinex.org: "Gender", "Plan", and "Security Question" on
# name="question "), so "Gender" was submitted as a gender.
def usable_options(options, select_element=None):
    names = {re.sub(r"\W+", "", (select_element.get_attribute(a) or "")).lower()
             for a in ("name", "id")} - {""} if select_element is not None else set()
    def names_itself(i, o):
        return i == 0 and any(n in re.sub(r"\W+", "", o.text or "").lower() for n in names)
    usable = [o for i, o in enumerate(options) if o.is_enabled() and (o.get_attribute("value") or "").strip()
              and not PLACEHOLDER_OPTION_RE.match(o.text or "") and not names_itself(i, o)]
    return usable or [o for o in options if o.is_enabled()] or options

def handle_dropdowns(form, driver):
    select_elements = form_controls(form, 'select')
    for select_element in select_elements:
        options = []
        try:
            select = Select(select_element)
            options = usable_options(select.options, select_element)
            if options:
                random_option = random.choice(options)
                select.select_by_value(random_option.get_attribute("value"))
        except WebDriverException:
            try:
                if options:
                    random_value = random.choice(options).get_attribute("value")
                    # The change event lets script-driven forms (React) see the new value.
                    driver.execute_script("arguments[0].value = arguments[1];"
                                          "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                                          select_element, random_value)
            except WebDriverException as e:
                logging.error(f"Failed to select dropdown option using JavaScript: {e}")

# Dropdowns drawn by a script over a hidden text box. coinrulefx.com/register (ARMember) keeps the
# country in <input type="text" name="country" required value="0">, hidden, and shows a <dl> whose
# <li data-value="..."> items set it when clicked; the first item, value 0, is "Country/Region". The
# crawler could not type into the hidden box ("Element country is not interactable") and the site
# refused the form: "Country/Region Please select atleast one option." (Nov 1). Done the way a
# person does it: open the widget beside the box and click an item, preferring United States for a
# country (the phone numbers are US ones), else the first real item; the item's data-value is set
# directly only if the click did not change the box. Only a hidden text box whose own field wrapper
# is visible and holds a list of items counts, so hidden steps of a multi-step form and honeypot
# fields are left alone. Native <select>s are handle_dropdowns()'s.
CUSTOM_CHOICES_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
// Hidden the way Selenium sees it: coinrulefx's box is full size but has opacity 0.
const shown = e => vis(e) && getComputedStyle(e).opacity !== '0' && getComputedStyle(e).visibility !== 'hidden';
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const PLACEHOLDER = /^(-+.*|select\b.*|choose\b.*|please (select|choose)\b.*|\.\.\.|…)?$/i;
const f = arguments[0];
const els = f.elements ? Array.from(f.elements) : Array.from(f.querySelectorAll('input, select, textarea'));
const done = [];
for (const e of els) {
  if (e.tagName !== 'INPUT' || !['text', ''].includes((e.getAttribute('type') || '').toLowerCase()) || shown(e) || e.disabled) continue;
  let wrap = e.parentElement;
  for (let i = 0; wrap && wrap !== f && !vis(wrap) && i < 3; i++) wrap = wrap.parentElement;
  if (!wrap || wrap === f || !vis(wrap)) continue;
  if (wrap.querySelectorAll('input:not([type=hidden]), select, textarea').length > 3) continue;
  const items = Array.from(wrap.querySelectorAll('li, [role=option], [data-value]'))
      .filter(x => !x.contains(e) && x.closest('ul, ol, dl, [role=listbox], [class*=dropdown], [class*=options]')
              && clean(x.textContent) && !x.querySelector('li, [role=option]'));
  const hint = [e.name, e.id, e.getAttribute('aria-label'), e.getAttribute('placeholder')].join(' ');
  const usable = items.filter(x => !PLACEHOLDER.test(clean(x.textContent)) && (x.getAttribute('data-value') ?? clean(x.textContent)) !== e.value
                               && clean(x.textContent).toLowerCase() !== clean(e.getAttribute('aria-label')).toLowerCase());
  if (!usable.length) continue;
  const us = /country|nation|region/i.test(hint) && usable.find(x => /^(united states( of america)?|usa|us)$/i.test(clean(x.textContent)));
  const pick = us || usable[0];
  const before = e.value;
  const head = wrap.querySelector('dt, [role=combobox], [class*=selection], [class*=head], [class*=toggle], [class*=selected]');
  if (head && vis(head)) head.click();
  pick.click();
  if (e.value === before && pick.getAttribute('data-value') !== null) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, pick.getAttribute('data-value'));
    ['input', 'change'].forEach(t => e.dispatchEvent(new Event(t, {bubbles: true})));
    if (window.jQuery) { try { window.jQuery(e).trigger('change'); } catch (x) {} }
  }
  if (head && vis(head) && wrap.querySelector('ul, [role=listbox]') && vis(wrap.querySelector('ul, [role=listbox]'))) head.click();
  done.push({field: e.name || e.id || '(unnamed)', picked: clean(pick.textContent).slice(0, 40), value: e.value, was: before});
}
return done;
"""

def handle_custom_choices(driver, form):
    """Pick an item in script-drawn dropdowns whose hidden text box could not be typed into (see CUSTOM_CHOICES_JS)."""
    try:
        for choice in driver.execute_script(CUSTOM_CHOICES_JS, form) or []:
            logging.info(f"Custom dropdown {choice['field']!r}: picked {choice['picked']!r} "
                         f"(the field went from {choice['was']!r} to {choice['value']!r})")
    except Exception as e:
        logging.info(f"Custom dropdowns could not be set: {str(e).splitlines()[0] if str(e) else e!r}")

# --- Sign-up and login outcome checks --------------------------------------------------
# Until Oct 20 a sign-up counted as successful whenever a button was clicked (the URL was
# compared with a module-level start_url that was always ''), and a login whenever the URL
# changed. Over Oct 6-20, 149 of 288 "successful" sign-ups never left the sign-up page (140 of
# them then failed to log in), and 9 of 53 "logged-in" sites were not logged in; 3 of those
# had been redirected to the site's own ?a=login&say=invalid_login page. The outcome is now
# read from the page. Only messages that appear after the submit count, so a notice that was
# already on the form ("Please enter your details") is not mistaken for an error.

PAGE_STATE_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const errSel = '[class*="error" i],[class*="alert" i],[class*="invalid" i],[class*="warning" i],[class*="danger" i],' +
               '[role="alert"],.help-block,font[color="red" i],font[color="#ff0000" i],[style*="color:red" i],[style*="color: red" i]';
const errors = Array.from(document.querySelectorAll(errSel))
    .filter(e => vis(e) && !/success|info/i.test(String(e.className || '')))
    .map(e => (e.innerText || '').trim().replace(/\s+/g, ' ')).filter(t => t && t.length < 300);
const invalid = Array.from(document.querySelectorAll('input,select,textarea'))
    .filter(e => vis(e) && e.willValidate && !e.checkValidity())
    .map(e => (e.name || e.id || e.type) + ': ' + e.validationMessage);
// A form that posts to a logout URL counts too, visible or not: Laravel sites log out with
// <form action=".../logout"> and an icon-only button (opextrade.com's dashboard) or a hidden
// "logout-form" (swantradecapital.com), so no Logout link was seen while logged in.
const logout = Array.from(document.querySelectorAll('a,button')).filter(vis).some(e =>
    /log-?out|sign-?out|logoff|a=logout/i.test(e.getAttribute('href') || '') ||
    /^\s*(log ?out|sign ?out|log off)\s*$/i.test(e.innerText || ''))
  || Array.from(document.forms).some(f => /log-?out|sign-?out|logoff/i.test(f.getAttribute('action') || ''));
// alert()/confirm() messages kept by DIALOG_OVERRIDE_JS count as page messages and page text.
let dialogs = [];
try { dialogs = JSON.parse(sessionStorage.getItem('__crawler_dialogs') || '[]'); } catch (e) {}
return {url: location.href, errors: [...new Set(dialogs.concat(errors))].slice(0, 8), invalid: invalid.slice(0, 5), logout: logout,
        passwords: Array.from(document.querySelectorAll('input[type=password]')).filter(vis).length,
        dialogs: dialogs, text: (dialogs.join(' \n') + ' \n' + (document.body ? document.body.innerText : '')).slice(0, 20000)};
"""
SIGNUP_OK_RE = re.compile(r"registration (was |is )?(complete|completed|successful)|successfully (registered|signed up|created)|registered successfully|"
                          r"account (has been |was )?(created|registered)|thank you for (registering|signing up|joining|your registration)|"
                          r"check your (e-?mail|inbox)|(confirmation|verification|activation) (link|e-?mail|code) (has been |was )?sent|verify your e-?mail", re.I)
FORM_ERROR_RE = re.compile(r"already (exists|registered|in use|taken)|required|invalid|incorrect|do(es)? not match|must (be|contain|have)|"
                           r"please (enter|fill|provide|select|check|agree|type|correct|choose)|captcha|not valid|too (short|long)|wrong|error|failed", re.I)
LOGIN_FAILED_URL_RE = re.compile(r"say=invalid|invalid_login|login_?error|[?&]error=", re.I)
PASSWORD_RECOVERY_URL_RE = re.compile(r"forgot|reset-?password|password-?reset|recover|lost-?password|a=forgot", re.I)
# The page after a sign-up asks for the e-mail address to be confirmed (text or URL).
VERIFY_EMAIL_RE = re.compile(r"verify[-_ ]?(your )?e-?mail|confirm (your )?e-?mail|check your (e-?mail|inbox)|e-?mail[-_ ]?verification|"
                             r"(confirmation|verification|activation) (link|e-?mail|code) (has been |was )?sent", re.I)

def page_state(driver):
    """Logout link, visible password fields, visible error messages, invalid form fields and body text of the current page."""
    try:
        return driver.execute_script(PAGE_STATE_JS)
    except Exception as e:
        logging.info(f"Could not read the page state: {e}")
        return {"url": "", "errors": [], "invalid": [], "logout": False, "passwords": 0, "dialogs": [], "text": ""}

def new_errors(before, after):
    """Error-like messages on the page after a submit that were not on it before."""
    return [t for t in after["errors"] if t not in before["errors"] and FORM_ERROR_RE.search(t)]

def wait_for_outcome(driver, before, seconds=10):
    """After a submit, poll until the page shows something decisive (a new URL, a Logout link, a
    new message, invalid fields, the password fields gone) or `seconds` pass, and return the page
    state. A read that fails mid-navigation (empty url) is never taken as the outcome."""
    state = page_state(driver)
    for _ in range(seconds):
        new_ok = SIGNUP_OK_RE.search(state["text"]) and not SIGNUP_OK_RE.search(before["text"])
        if state["url"] and (state["url"] != before["url"] or state["logout"] or new_errors(before, state)
                             or state["invalid"] or not state["passwords"] or new_ok):
            time.sleep(1)  # let a redirect or an AJAX update finish
            settled = page_state(driver)
            return settled if settled["url"] else state
        time.sleep(1)
        state = page_state(driver)
    return state

def assess_signup(before, after, form_url):
    """('success' | 'rejected' | 'unclear', evidence) for the page shown after a sign-up submit.
    Only 'rejected' changes what the crawler does next (no e-mail wait, no login attempt), so it
    needs explicit evidence: a new error message or a field the browser refused, on a page that
    still shows the form. 'unclear' carries on as before: 9 of the 149 sign-ups that never left
    the sign-up page did end in a login."""
    errors = new_errors(before, after)
    ok = SIGNUP_OK_RE.search(after["text"])
    if after["logout"]:
        return "success", "a Logout link is on the page"
    if ok and not SIGNUP_OK_RE.search(before["text"]):
        return "success", f"success message {ok.group(0)!r}"
    # Empty required fields only count while the sign-up form is still shown. On minnercoinex.org
    # the sign-up went on to signin.php, whose untouched login fields ("email: Please fill out this
    # field") were read as the sign-up being refused.
    invalid = after["invalid"] if after["url"] == form_url else []
    if after["passwords"] and (errors or invalid):
        return "rejected", "; ".join(errors + invalid)[:300]
    if after["url"] != form_url and LOGIN_URL_RE.search(after["url"]):
        return "success", f"moved to the login page {after['url']} with no error message"
    if after["url"] != form_url and not after["passwords"]:
        return "success", f"moved to {after['url']} and the form is gone"
    where = "URL unchanged" if after["url"] == form_url else f"now at {after['url']}"
    return "unclear", f"{where}, {after['passwords']} password field(s) on the page, no message found"

def assess_login(before, after):
    """('success' | 'failed' | 'unclear', evidence) for the page shown after a login submit.
    'unclear' (no Logout link, no password field) carries on as logged in, which also covers a
    login done by JavaScript without a URL change; the old URL-change test missed those."""
    errors = new_errors(before, after)
    if LOGIN_FAILED_URL_RE.search(after["url"]):
        return "failed", f"redirected to {after['url']}"
    if after["logout"]:
        return "success", "a Logout link is on the page"
    if after["passwords"]:
        return "failed", "; ".join(errors)[:300] or f"still on a page with {after['passwords']} password field(s) and no Logout link"
    return "unclear", f"no Logout link and no password field at {after['url']}"

# Submit form
def click_and_submit(driver, button, button_type):
    # True once the button has been clicked. Whether that worked is decided afterwards from the
    # page (assess_signup); the URL comparison that used to be here compared with '' and so
    # reported every click as a successful sign-up.
    try:
        before = driver.current_url
        driver.execute_script("arguments[0].click();", button)
        time.sleep(3)
        after = driver.current_url
        logging.info(f"Form submitted - URL {'changed to ' + after if after != before else 'unchanged: ' + after}")
        return True
    except (ElementNotInteractableException, TimeoutException):
        button.send_keys(Keys.RETURN)
    return False

def submit_form(driver, selectors, form=None):
    # The chosen form's own submit button first: the page-wide search below clicked the first
    # matching button on the page ("Log in" on cryptocoinprop.com) and waited 10 s for each
    # selector the page does not use. A <button> without a type attribute submits too.
    if form is not None:
        # A box without a <form> (signup_box) has no submit buttons of its own; its typeless buttons
        # can be tab switches ("Create account" / "Log in" on aegisinvestmentgroup.com).
        is_box = form.tag_name.lower() != 'form'
        for button in ([] if is_box else form_controls(form, 'button[type="submit"], input[type="submit"], button:not([type])')):
            try:
                if button.is_displayed() and button.is_enabled() and click_and_submit(driver, button, By.CSS_SELECTOR):
                    return True
            except StaleElementReferenceException:
                continue
        # Script-driven sign-ups (a box without a <form>, or type="button") are sent by a button
        # labelled like sign-up. In a box the last one is taken: the tab switch comes first, the
        # button that sends the fields comes after them.
        labelled = [b for b in form_controls(form, 'button, input[type="button"], input[type="submit"]')
                    if b.is_displayed() and b.is_enabled() and SIGNUP_BUTTON_RE.match(b.text or b.get_attribute('value') or '')]
        for button in (labelled[::-1] if is_box else labelled):
            try:
                if click_and_submit(driver, button, By.CSS_SELECTOR):
                    return True
            except StaleElementReferenceException:
                continue
    for selector, selector_type in selectors:
        try:
            submit_button = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((selector_type, selector)))
            if click_and_submit(driver, submit_button, selector_type):
                return True
        except TimeoutException:
            logging.info(f"Timed out waiting for the submit button to be clickable: {selector}")
    try:
        any_button = driver.find_element(By.TAG_NAME, 'button')
        any_button.click()
        return True
    except NoSuchElementException:
        logging.info("No buttons found on the page.")
    return False

# Check if the URL is blacklisted
def is_blacklisted(url):
    for blacklisted_url in blacklisted_urls:
        if blacklisted_url in url:
            return True
    return False

# Fill sign-up form
OFFSITE_SIGNUP_TEXT_RE = re.compile(r"^(sign ?up|register|registration|join( now)?|get started|open (an? )?(free |new |live |real |trading )?account"
                                    r"|create (an? |your |my )?(free |new |live |real |trading )?account)( now| here)?$", re.I)

def fill_sign_up_form(driver, start_url):
    global should_continue, email_used, password_used
    driver = ensure_driver(driver)
    base_domain = get_base_domain(start_url)
    site_domains = {base_domain}  # plus the domain the start page redirects to, if another one
    visited_urls = set()
    urls_to_check = [start_url]
    form_filled = False
    offsite_signup, offsite_tried = [], False  # sign-up links that leave the site (see below)

    # Stops once an account is made. After an error past that point (swantradecapital.com, Oct 26)
    # the loop carried on and loaded every queued page again, about 55 s of page loads for nothing.
    while should_continue and not form_filled:
        if classify_only and len(site_evidence.pages) >= MAX_PAGES:
            logging.info(f"HYIP check: classify-only run; {MAX_PAGES} pages read, ending the sign-up search")
            break
        if not urls_to_check:
            # Every sign-up link of the site leads to another domain: investings24.com's "Register" /
            # "Create Live Account" go to track.dmx.tradecore.io/?t=YXrfy49758UT, which redirects to
            # clients.brokerstechnologies.com/r/yxrfy49758ut and on to its /signup/demo/ (Oct 30); the
            # search only followed the site's own links and gave up. Once the site's own pages are
            # used up, the first such link is followed, and the domain it lands on counts as the site.
            if not offsite_signup or offsite_tried:
                break
            offsite_tried = True
            link = offsite_signup[0]
            try:
                driver.get(link)
                pass_bot_wall(driver)
                landed = driver.current_url
            except Exception as e:
                logging.info(f"Off-site sign-up link {link} could not be opened: {str(e).splitlines()[0] if str(e) else e!r}")
                break
            if not landed.startswith('http'):
                break
            site_domains.add(get_base_domain(landed))
            site_evidence.add('crawl_offsite_signup', landed)
            logging.info(f"Off-site sign-up: {start_url}'s sign-up link {link} leads to {landed}; "
                         f"pages on {get_base_domain(landed)} count as the same site")
            urls_to_check.append(landed)
        current_url = urls_to_check.pop(0)
        if current_url in visited_urls or is_blacklisted(current_url):
            continue
        visited_urls.add(current_url)
        try:
            found = has_two_or_more_password_fields(driver, current_url)
            # A site whose first page redirects to another domain (fxcapital3-pro.com -> fxcapital3.com,
            # arbitragetrading.io -> arbitragescanner.io, Oct 29) had every link on it counted as off-site,
            # so the search ended on the first page. The domain the start page lands on counts too.
            landed = driver.current_url
            if current_url == start_url and landed.startswith('http') and not same_site(landed, base_domain):
                site_domains.add(get_base_domain(landed))
                logging.info(f"{start_url} redirected to {landed}; pages on {get_base_domain(landed)} count as the same site")
            # A lead form or another product on the start page ends the site before any sign-up.
            if current_url == start_url and stop_if_clearly_b("start page"):
                return False
            if not found and (gated := agent_gated_signup(driver)):
                logging.info(f"Agent-gated sign-up: {gated}; stopping the sign-up search")
                break
            # A page with a login form may build its sign-up form only when a tab is clicked
            # (fedtradez.com/auth); then it counts as the sign-up page. The form is revealed again
            # after the page is reloaded below.
            if (not found and should_continue and not form_filled and current_url.startswith('http')
                    and driver.execute_script(VISIBLE_PASSWORDS_JS) >= 1 and reveal_signup_form(driver) is not None):
                found = current_url
            if found and not form_filled and should_continue:
                driver = ensure_driver(driver)
                driver.get(current_url)
                pass_bot_wall(driver)
                read_page(driver, 'register')
                captcha_blocks, captcha_summary, captcha_answer, captcha_target = check_captcha(driver)
                logging.info(f"Captcha check on {driver.current_url}: {captcha_summary}")
                if captcha_blocks:
                    return False

                wait = WebDriverWait(driver, 5)
                try:
                    # A <form>, or a password field outside any form (see signup_box).
                    wait.until(lambda d: d.find_elements(By.TAG_NAME, 'form') or d.find_elements(By.CSS_SELECTOR, 'input[type="password"]'))
                    form = pick_signup_form(driver)
                    if form is None:
                        form = signup_box(driver)
                        if form is not None:
                            logging.info("Sign-up fields are not in a <form>; using the box around them and its sign-up button")
                    if form is None:
                        # Before moving on to other pages: the form may be on this page behind a click.
                        form = reveal_signup_form(driver)
                    if form is None and captcha_answer is not None and should_continue and pass_captcha_gate(driver, captcha_answer, captcha_target):
                        captcha_blocks, captcha_summary, captcha_answer, captcha_target = check_captcha(driver)
                        logging.info(f"Captcha check after the gate on {driver.current_url}: {captcha_summary}")
                        if captcha_blocks:
                            return False
                        form = pick_signup_form(driver)
                    if form and should_continue:
                        logging.info(f"Sign-up form chosen: action={form.get_attribute('action')!r}")
                        try:
                            site_evidence.add_signup_form(form.parent.execute_script(FIELD_HINTS_JS, form) or [], driver.current_url)
                        except Exception as e:
                            logging.info(f"HYIP check: could not read the sign-up form's fields: {str(e).splitlines()[0] if str(e) else e!r}")
                        if stop_if_clearly_b("sign-up form", at_form=True):
                            return False
                        predictions = predict_values(form)
                        password_used = next((v for v in predictions.values() if v in (ACCOUNT_PASSWORD, ACCOUNT_PASSWORD_ALT)), ACCOUNT_PASSWORD)
                        if password_used == ACCOUNT_PASSWORD_ALT:
                            logging.info(f"The password fields' maxlength/pattern refuse the usual password; using {ACCOUNT_PASSWORD_ALT!r}")
                        fill_form(form, predictions)
                        fill_textareas(form)
                        time.sleep(1)

                        if not should_continue:
                            return False

                        handle_dropdowns(form, driver)
                        handle_custom_choices(driver, form)
                        time.sleep(1)
                        # A captcha in a later step was hidden when the page was first checked:
                        # fxtastytrades.com's step 3 is "Enter the code below: X7DPDX". Check again.
                        if advance_form_steps(driver, form, predictions) and captcha_answer is None:
                            captcha_blocks, captcha_summary, captcha_answer, captcha_target = check_captcha(driver)
                            logging.info(f"Captcha check after the form steps: {captcha_summary}")
                            if captcha_blocks:
                                return False

                        if not should_continue:
                            return False

                        handle_checkbox(driver, form)

                        # After the fields are filled, so predict_values() cannot overwrite it.
                        if captcha_answer is not None:
                            answer_captcha(driver, captcha_answer, captcha_target)

                        # The page's own script can hold the submit button disabled until its rules pass;
                        # a password rule that refuses symbols (tradevistavelocityinvestmentmark.com) is
                        # the one this can change. Retry with the letters-and-digits password.
                        if submit_disabled(form) and password_used != ACCOUNT_PASSWORD_ALT:
                            password_used = ACCOUNT_PASSWORD_ALT
                            for key, value in predictions.items():
                                if value == ACCOUNT_PASSWORD:
                                    predictions[key] = ACCOUNT_PASSWORD_ALT
                                    field = field_by_key(form, key)
                                    if field is not None and field.is_displayed():
                                        field.clear()
                                        field.send_keys(ACCOUNT_PASSWORD_ALT)
                            time.sleep(0.5)
                            logging.info(f"Submit button disabled with the usual password; retried with {ACCOUNT_PASSWORD_ALT!r}: "
                                         + ("now enabled" if not submit_disabled(form) else "still disabled"))

                        selectors = [
                            ('input[type="submit"]', By.CSS_SELECTOR),
                            ('button[type="submit"]', By.CSS_SELECTOR),
                            ('button[type="button"]', By.CSS_SELECTOR),
                            # A CSS selector: as By.NAME it was an invalid locator that raised and
                            # ended the attempt (tradevistavelocityinvestmentmark.com, Oct 26).
                            ('input[name="submit"]', By.CSS_SELECTOR)
                        ]

                        form_url = driver.current_url
                        before_submit = page_state(driver)
                        if submit_form(driver, selectors, form):
                            after_submit = wait_for_outcome(driver, before_submit)
                            outcome, evidence = assess_signup(before_submit, after_submit, form_url)
                            logging.info(f"Sign-up result: {outcome} - {evidence}")
                            if outcome in ("rejected", "unclear"):
                                save_failed_page(driver, f"signup_{outcome}")
                            if outcome == "rejected":
                                # The site refused the form, so there is no account to confirm
                                # or log in to; skip the 60 s inbox wait and the login search.
                                return False
                            # predictions is keyed by input name, so .get("email") only
                            # worked when the field was literally named "email". Take the
                            # generated address from whichever field received it.
                            email_used = next((v for v in predictions.values()
                                               if isinstance(v, str) and v.endswith(f"@{EMAIL_DOMAIN}")), None)
                            form_filled = True
                            if outcome == "success":
                                logging.info(f"Sign-up succeeded with {email_used}")
                            else:
                                logging.info(f"Sign-up submitted with {email_used}, but the page gave no sign of success; trying to log in anyway")
                            want_code = bool(driver.execute_script(CODE_INPUTS_JS))
                            if want_code:
                                logging.info(f"The page after sign-up ({driver.current_url}) asks for a code; waiting for it by e-mail")
                            email_checked, email_code = check_mailinator_inbox_and_click_links(
                                MAILINATOR_API_KEY, driver, email_used, signup_time=time.time(),
                                site_url=form_url, want_code=want_code)
                            if email_checked:
                                logging.info("Successfully interacted with new email.")
                            elif MAILINATOR_API_KEY and MAILINATOR_API_KEY != "YOUR_API_KEY" and not email_code:
                                logging.info(f"No confirmation email clicked within {MAILINATOR_WAIT_SECONDS} seconds.")
                            if email_code:
                                code_state, code_url = enter_email_code(driver, email_code, after_submit["url"])
                                if code_state:
                                    email_checked = True
                                    if code_state["logout"] or (code_state["url"] != code_url and not code_state["passwords"]):
                                        # The code page let go of the session: the account is in, no login needed.
                                        logging.info(f"E-mail code accepted; logged in at {code_state['url']}")
                                        crawl_logged_in(driver, code_state["url"], get_base_domain(code_state["url"]),
                                                        driver.current_window_handle)
                                        break
                                    after_submit = code_state  # the login search starts where the code step ended
                            if not email_checked and (VERIFY_EMAIL_RE.search(after_submit["text"]) or VERIFY_EMAIL_RE.search(after_submit["url"])):
                                # The login used to be skipped here, since cryptocoinprop.com (/verify-email/)
                                # and swifttradinginc.live refuse it until the address is confirmed. But
                                # itrademagnet.com asked for verification, sent no e-mail at all and let the
                                # account log in without it (Oct 29), so the login is tried; a refusal shows
                                # up as "Login result: failed".
                                logging.info("The page asks for e-mail verification and no confirmation link was clicked; trying to log in anyway")

                            if crawl_if_already_logged_in(driver, "after sign-up"):
                                break
                            login_url, logged_in_url = find_login_url_with_single_password_field(
                                driver, current_url, signup_url=form_url, landed_url=after_submit["url"])
                            if login_url:
                                login_and_take_screenshot(driver, login_url, password_used, email_used)
                            elif logged_in_url:
                                # find_login_url_with_single_password_field() saw a Logout link, or a login
                                # page that sends the browser on to an account page; the driver is there.
                                logging.info(f"Already logged in (during the login search): {logged_in_url}")
                                crawl_logged_in(driver, logged_in_url, get_base_domain(logged_in_url), driver.current_window_handle)
                            else:
                                # break, not return False: the account exists, so the site's result
                                # line should say "Signed up but not logged in", not "failure".
                                logging.info("Could not find a login URL to proceed with login.")
                            break

                        else:
                            logging.info("Form submission unsuccessful.")
                    else:
                        gated = agent_gated_signup(driver)
                        if gated:
                            logging.info(f"Agent-gated sign-up: {gated}; stopping the sign-up search")
                            break
                        logging.info("No sign-up form found on the page (no visible form with a password field that is not a login form).")
                except Exception as e:
                    logging.error(f"An error occurred: {str(e)}")

            if not form_filled:
                # The hrefs are read in one script call. Reading them link by link failed when the page
                # changed in between: on olymptrade-wid.com and blog.investax.io (Oct 29) the
                # StaleElementReferenceException was not caught here and ended the site's crawl.
                links = driver.execute_script("return Array.from(document.querySelectorAll('a[href]'))"
                                              ".map(a => [a.href, (a.innerText || '').trim()]);") or []
                # Off-site sign-up links only count on the site's own pages: gozmining.com's /login/github
                # went on to github.com, whose own "Sign up" link was taken for gozmining's (Oct 31).
                on_site = any(same_site(driver.current_url, d) for d in site_domains)
                for href, link_text in links:
                    absolute_link = urljoin(current_url, href)
                    # same_site() also takes subdomains: securenovatrade.com's sign-up and login are on
                    # account.securenovatrade.com, which used to count as another site.
                    # Language links are skipped as in the login search: gozmining.com's /lang/en ... /lang/gr
                    # each switch the session's language, so /register came up in Greek ("Συνέχεια με Email")
                    # and no English sign-up label matched (Oct 31).
                    if LANGUAGE_LINK_RE.search(absolute_link):
                        continue
                    if any(same_site(absolute_link, d) for d in site_domains) and not is_blacklisted(absolute_link):
                        if absolute_link not in visited_urls and absolute_link not in urls_to_check and should_continue:
                            urls_to_check.append(absolute_link)
                    elif (on_site and absolute_link.startswith('http') and OFFSITE_SIGNUP_TEXT_RE.match(re.sub(r"[^\w\s]", " ", link_text).strip())
                          and absolute_link not in offsite_signup and not is_blacklisted(absolute_link)):
                        offsite_signup.append(absolute_link)
        except TimeoutException:
            logging.info(f"Sign-up unsuccessful for {current_url}. Website not reachable")
        except Exception as e:
            # Anything else ends this page, not the site: the next queued page is tried.
            logging.error(f"An error occurred on {current_url} during the sign-up search: {str(e).splitlines()[0] if str(e) else e!r}")
    if not form_filled:
        logging.info("No page with two or more password fields found.")
    return form_filled

# Check for multiple password fields. Each page loaded is logged with where it ended up, so a site
# with no sign-up page found shows what the search saw (Oct 29: six such sites logged nothing).
def has_two_or_more_password_fields(driver, url):
    if url.startswith('javascript:'):
        return None
    keywords = ['joinus', 'register', 'signup','createaccount', 'newuser', 'registration'] #Keywords to detect signup pages in DOM
    if any(keyword in url.lower() for keyword in keywords):
        logging.info(f"Registration URL is {url}")
        return url
    try:
        driver.get(url)
        pass_bot_wall(driver)
        password_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]')
        landed = driver.current_url
        logging.info(f"Sign-up search: {url}" + (f" -> {landed}" if landed != url else "")
                     + f"; {len(password_inputs)} password field(s)")
        read_page(driver)
        if len(password_inputs) >= 2:
            return url
    except Exception as e:
        logging.error(f"An error occurred while fetching {url}: {str(e)}")
        if not site_evidence.pages:  # the start page itself did not load
            site_evidence.unreachable = site_evidence.unreachable or (str(e).splitlines()[0] if str(e) else repr(e))[:200]

# Agent-gated sign-up: the page the site's sign-up links lead to asks only for name, e-mail and
# phone, with no password anywhere on the page, so the account (if any) is set up by a sales agent
# or in a later step the crawler cannot reach. kestrel-fundast.ca/auth (first and last name, e-mail,
# phone, "Join Now") and coinvektaai.uk/landers/vmpt/auth/ (the same fields, "Open an account")
# took the whole 5-minute limit while the search walked every other page (Oct 29). These sites can
# still be HYIPs; this only ends the sign-up search and says why. Checked on sign-up-like URLs only,
# so a call-back form elsewhere on a site does not count. A form with a message box is a contact form.
SIGNUP_LIKE_URL_RE = re.compile(r"regist|sign-?up|join|auth|account|open|lander|get-?started|create", re.I)
LEAD_FORM_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
if (Array.from(document.querySelectorAll('input[type=password]')).some(vis)) return null;
const hint = e => [e.name, e.id, e.placeholder, e.getAttribute('autocomplete'), e.getAttribute('aria-label')].join(' ').toLowerCase();
const isEmail = e => e.type === 'email' || /e-?mail/.test(hint(e));
const isPhone = e => e.type === 'tel' || /phone|mobile|whatsapp|\btel\b/.test(hint(e));
const isName = e => /first.?name|last.?name|full.?name|fname|lname|surname|\bname\b/.test(hint(e));
const BUTTON = /join|open|regist|sign ?up|create|get started|start|continue|submit|apply|claim|reserve|account|next|send/i;
const boxes = new Set(Array.from(document.forms));
Array.from(document.querySelectorAll('input')).filter(e => vis(e) && isEmail(e) && !e.form).forEach(e => {
  for (let b = e.parentElement, i = 0; b && i < 6; b = b.parentElement, i++)
    if (b.querySelector('button, input[type=submit]')) { boxes.add(b); break; }
});
for (const box of boxes) {
  const inputs = Array.from(box.querySelectorAll('input')).filter(e => vis(e) && !['hidden', 'checkbox', 'radio', 'submit', 'button', 'image'].includes(e.type));
  if (!inputs.length || inputs.length > 8 || Array.from(box.querySelectorAll('textarea')).some(vis)) continue;
  if (!inputs.some(isEmail) || !(inputs.some(isPhone) || inputs.some(isName))) continue;
  const buttons = Array.from(box.querySelectorAll('button, input[type=submit]')).filter(vis);
  const send = buttons.map(b => (b.innerText || b.value || '').trim()).find(t => BUTTON.test(t))
            || (buttons.some(b => (b.type || '').toLowerCase() === 'submit') ? '(submit)' : undefined);
  if (send === undefined) continue;
  return {fields: inputs.map(e => e.name || e.id || e.type), button: send.slice(0, 40)};
}
return null;
"""

def agent_gated_signup(driver):
    """A log message when the current page is a sign-up-like URL whose only account form is a
    name/e-mail/phone lead form (see LEAD_FORM_JS), else None."""
    url = driver.current_url
    if not SIGNUP_LIKE_URL_RE.search(urlparse(url).path + "?" + urlparse(url).query):
        return None
    try:
        lead = driver.execute_script(LEAD_FORM_JS)
    except Exception:
        return None
    if not lead:
        return None
    site_evidence.add('crawl_lead_form', url, site_evidence.is_onsite(url))
    return (f"{url} has a form with {lead['fields']} and a {lead['button']!r} button but no password field "
            f"(an agent or a later step sets up the account)")

# "Success" used to be logged whenever a sign-up form was submitted, including sites where
# the login then failed (swifttradinginc.live). It now needs a login that was not judged failed.
def log_site_result(url, email):
    if logged_in:
        logging.info(f"Success: {url} (logged in as {email})")
    elif email:
        logging.info(f"Signed up but not logged in: {url} ({email})")
    else:
        logging.info(f"Timeout or failure: {url}")
    log_hyip_verdict(url)

# The HYIP verdict line (run_regression.py reads it) and every feature behind it, with the page it
# was seen on, in hyip_evidence.json (hyip_evidence_<domain>.json when crawling urls.txt).
def log_hyip_verdict(url):
    try:
        verdict = classify(site_evidence)
        logging.info(verdict_line(verdict))
        name = 'hyip_evidence.json' if single_site else f"hyip_evidence_{windows_safe(site_evidence.domain)}.json"
        with open(name, 'w', encoding='utf-8') as f:
            json.dump(site_evidence.to_json(verdict), f, indent=1, ensure_ascii=False)
    except Exception as e:
        logging.error(f"HYIP check failed for {url}: {e}")

# Process URLs from a list
def fill_sign_up_urls_in_list(urls_file_path, last_processed_file_path):
    global current_site_url
    if not os.path.exists(urls_file_path):
        logging.info(f"No {urls_file_path} yet (feeder.py has not added any domains); nothing to crawl.")
        return
    last_processed_url = get_last_processed_url(last_processed_file_path)
    start_processing = last_processed_url is None

    with open(urls_file_path, 'r') as file:
        for line in file:
            url = line.strip()
            if start_processing:
                try:
                    current_site_url = url
                    email_used = fill_sign_up_form_with_timeout(driver, url)
                    log_site_result(url, email_used)
                    save_last_processed_url(last_processed_file_path, url)
                except Exception as e:
                    logging.error(f"An error occurred while processing {url}: {e}")
                    break
            if url == last_processed_url:
                start_processing = True

# Check for single password field on the page the caller has just loaded. A login keyword
# anywhere in the URL used to be enough on its own, without looking at the page, and the
# keywords were matched as substrings: 'account' took cryptocoinprop.com's article
# /academy/library/five-habits-that-keep-a-funded-account-alive/ as the login page. Keywords now
# match whole words and only decide whether to give the page a few seconds to render its form;
# the page must show exactly one visible password field. Hidden ones do not count, so a
# /login/ page that also holds a hidden sign-up tab still qualifies.
LOGIN_URL_RE = re.compile(r"(?<![a-z])(log-?in|sign-?in|auth|authenticate|userlogin|member)(?![a-z])", re.I)
LOGIN_TEXT_RE = re.compile(r"^\s*(member )?(log ?in|sign ?in)\s*$", re.I)
VISIBLE_PASSWORDS_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
return Array.from(document.querySelectorAll('input[type=password]')).filter(vis).length;
"""

def has_single_password_field(driver, url):
    if url.startswith('javascript:'):
        return None
    try:
        if LOGIN_URL_RE.search(url):
            try:
                WebDriverWait(driver, 5).until(lambda d: d.execute_script(VISIBLE_PASSWORDS_JS) > 0)
            except TimeoutException:
                pass
        if driver.execute_script(VISIBLE_PASSWORDS_JS) == 1:
            return url
    except Exception as e:
        logging.error(f"An error occurred while checking {url} for a login form: {str(e)}")
    return None

# Get base domain from URL. A leading "www." is dropped so that a site which
# redirects example.com -> www.example.com still counts as one site; otherwise
# every link on the redirected page looked off-site and the crawl stopped on
# the first page (seen on crystaltradeinvestment.net).
def get_base_domain(url):
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith('www.') else netloc

# Login and take screenshot
def login_and_take_screenshot(driver, login_url, predicted_password, email_used):
    base_domain = get_base_domain(login_url)
    original_window = None  # the finally block below reads it even if driver.get() fails
    try:
        if not email_used:
            # The username below is derived from the e-mail, and re.sub() on None used to crash
            # here ("Unexpected error during login") after every such sign-up.
            logging.info("Login result: skipped - the sign-up form had no e-mail field, so there is no account e-mail to log in with")
            return
        driver.get(login_url)
        logging.info(f"Navigating to URL: {login_url}")
        pass_bot_wall(driver)
        original_window = driver.current_window_handle

        try:
            username_input = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="username"],input[type="email"],input[type="text"]')))
            input_type = username_input.get_attribute('type')
            # A text box named or labelled e-mail gets the address: fxtastytrades.com's login answered
            # "The email must be a valid email address" when it got the derived username. A box whose
            # name or id says user keeps the username even if its placeholder offers e-mail too:
            # hubguardfxz.com's name="username" placeholder="Username or Email" refused the address
            # (say=invalid_login) and accepted the username.
            email_box = re.search(r"mail", ' '.join((username_input.get_attribute(a) or '') for a in ('name', 'id', 'placeholder', 'autocomplete')), re.I)
            user_box = re.search(r"user", ' '.join((username_input.get_attribute(a) or '') for a in ('name', 'id')), re.I)
            if input_type == "text" and (user_box or not email_box):
                modified_email = re.sub(r'\d+', '', email_used).replace(f"@{EMAIL_DOMAIN}", "")
                username_input.send_keys(modified_email)
            else:
                username_input.send_keys(email_used)
        except TimeoutException:
            logging.error("Timeout occurred while trying to find the username/email input field.")

        password_input = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="password"]')))
        password_input.send_keys(predicted_password)

        before_submit = page_state(driver)
        click_login_submit(driver, password_input)

        # Judged from the page, not from a URL change: a failed login that redirects to
        # ?a=login&say=invalid_login changes the URL too.
        after_login = wait_for_outcome(driver, before_submit)
        outcome, evidence = assess_login(before_submit, after_login)
        # Ending up on another site is not a login: tradeglobalast.com.dbxubite87.world sends the
        # logged-in browser to recaptcha.cloud (a bot wall), which has no password field and no
        # Logout link, so it read as "unclear", was crawled as the dashboard and reported Success.
        if outcome != "failed" and after_login["url"] and not same_site(after_login["url"], base_domain):
            outcome, evidence = "failed", f"sent off the site to {after_login['url'][:150]}"
        # Nor is landing on the site's password-recovery page: gccbrokers.com sent the login to
        # /forgot-password?email=... (Oct 30), read as "unclear" and reported as logged in. Other
        # "unclear" logins stay logged in: 22 of 23 in the Oct 24-30 logs were real (they landed on
        # ?a=account, /dashboard, /user/ ...; accionwealth, kalderacapital, matoxtrade gave wallets).
        if outcome == "unclear" and PASSWORD_RECOVERY_URL_RE.search(after_login["url"] or ""):
            outcome, evidence = "failed", f"landed on the password-recovery page {after_login['url'][:150]}"
        logging.info(f"Login result: {outcome} - {evidence}")
        if outcome == "failed":
            save_failed_page(driver, "login_failed")
            return
        crawl_logged_in(driver, login_url, base_domain, original_window)
    except NoSuchElementException as e:
        logging.error(f"Element not found during login process: {e}")
    except TimeoutException as e:
        logging.error("Timeout occurred, login might have failed or the URL did not change.")
    except Exception as e:
        logging.error(f"Unexpected error during login and page capturing: {e}")
    finally:
        if original_window and original_window in driver.window_handles:
            driver.switch_to.window(original_window)

# The login form's own send button: a submit button, or one without a type, of the form that holds
# the password field. duduinvest.com's "Log in" has no type attribute; the old search for
# button[type=submit] found none and fell back to the first button in a form, the "Show password"
# toggle, so the login was never sent.
def click_login_submit(driver, password_input):
    login_form = driver.execute_script("return arguments[0].form;", password_input)
    if login_form is not None:
        own = [b for b in form_controls(login_form, 'button[type="submit"], input[type="submit"], button:not([type])')
               if b.is_displayed() and b.is_enabled()]
        if own:
            driver.execute_script("arguments[0].click();", own[0])
            return
    try:
        submit_button = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'],input[type='submit']")))
        submit_button.click()
    except TimeoutException:
        submit_button = driver.find_element(By.CSS_SELECTOR, "form button, form input[type='button']")
        driver.execute_script("arguments[0].click();", submit_button)

# Session lost during the crawl. fxtastytrades.com logged the crawler out between deposit pass 5
# and 6 (the cause is not visible); passes 6-8 and all 26 pages saved afterwards were its "Sign In"
# page. A page that shows a password field and no Logout link, on a login URL or titled like a
# login page, means the session is gone: log in again on that page with the account's address and
# password (up to MAX_RELOGINS times per site), else stop saving pages for the site.
MAX_RELOGINS = 2
relogins_done = 0
session_lost = False

def looks_logged_out(driver):
    state = page_state(driver)
    return (state["passwords"] >= 1 and not state["logout"]
            and bool(LOGIN_URL_RE.search(state["url"]) or re.search(r"sign ?in|log ?in", driver.title or "", re.I)))

def relogin_here(driver):
    """Log in again on the login page the browser is showing. True when it worked."""
    global relogins_done, session_lost
    if session_lost or relogins_done >= MAX_RELOGINS or not email_used:
        session_lost = True
        return False
    relogins_done += 1
    logging.info(f"Session lost (now at {driver.current_url}); logging in again as {email_used} (attempt {relogins_done})")
    try:
        passwords = [e for e in driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]') if e.is_displayed()]
        login_form = driver.execute_script("return arguments[0].form;", passwords[0])
        boxes = [e for e in (form_controls(login_form, 'input[type="email"], input[type="text"], input:not([type])') if login_form is not None
                             else driver.find_elements(By.CSS_SELECTOR, 'input[type="email"], input[type="text"]')) if e.is_displayed()]
        if boxes:
            box = boxes[0]
            email_box = re.search(r"mail", ' '.join((box.get_attribute(a) or '') for a in ('name', 'id', 'placeholder', 'autocomplete')), re.I)
            user_box = re.search(r"user", ' '.join((box.get_attribute(a) or '') for a in ('name', 'id')), re.I)
            box.clear()
            box.send_keys(re.sub(r'\d+', '', email_used).replace(f"@{EMAIL_DOMAIN}", "")
                          if box.get_attribute('type') == 'text' and (user_box or not email_box) else email_used)
        passwords[0].clear()
        passwords[0].send_keys(password_used)
        before = page_state(driver)
        click_login_submit(driver, passwords[0])
        outcome, evidence = assess_login(before, wait_for_outcome(driver, before))
        logging.info(f"Logging in again: {outcome} - {evidence}")
        if outcome == "failed":
            session_lost = True
            return False
        return True
    except Exception as e:
        logging.info(f"Logging in again failed: {e}")
        session_lost = True
        return False

# "Complete your profile" gates after sign-up. mail.nexarioncapitalventure.com (HyipLab template)
# sends every back-office page (/user/dashboard, /user/invest/statistics) to /user/user-data, a form
# for first and last name, address, state, zip and city, until it is filled in; the crawler never
# filled it and found no deposit link (Nov 1); its address, state, zip and city boxes are inside an
# HTML comment, so the live form is first and last name only. A visible form on the page after login
# counts as such a gate when the page has no password field, 2 or more of the form's visible fields
# and 60% of them are name/address/contact fields, none is an amount, search or message box, and the
# URL or the form's action reads like a set-up step or the page has 8 links or fewer. "Profile" and
# "settings" do not count as a set-up step: the back-office settings forms of trade-earnerxsenergy.com
# (21 links) and assetgain.org (20) have the same fields.
PROFILE_GATE_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
if (Array.from(document.querySelectorAll('input[type=password]')).some(vis)) return null;
const PROFILE = /first.?name|last.?name|full.?name|surname|\bname\b|address|street|city|state|province|zip|postal|post.?code|country|phone|mobile|birth|dob|gender/i;
const STEP = /user-?data|complete|onboard|setup|kyc/i;
const hint = e => [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), e.labels && e.labels.length ? e.labels[0].innerText : ''].join(' ');
const links = Array.from(document.querySelectorAll('a[href]')).filter(vis).length;
for (let i = 0; i < document.forms.length; i++) {
  const f = document.forms[i];
  if (!vis(f)) continue;
  const fields = Array.from(f.elements).filter(e => vis(e) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName)
      && !['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'image', 'reset'].includes((e.type || '').toLowerCase()));
  if (fields.length < 2 || fields.some(e => /amount|\bsum\b|search|message|subject|comment/i.test(hint(e)))) continue;
  const profile = fields.filter(e => PROFILE.test(hint(e)));
  if (profile.length < 2 || profile.length < 0.6 * fields.length) continue;
  const action = f.getAttribute('action') || '';
  if (!(STEP.test(location.pathname) || STEP.test(action) || links <= 8)) continue;
  return {index: i, fields: profile.map(e => e.name || e.id || e.type).slice(0, 10), action: action, links: links};
}
return null;
"""

def pass_profile_gate(driver):
    """Fill in and submit a profile form that stands in front of the back office (see PROFILE_GATE_JS)."""
    try:
        gate = driver.execute_script(PROFILE_GATE_JS)
        if not gate:
            return
        start = driver.current_url
        form = driver.execute_script("return document.forms[arguments[0]];", gate['index'])
        fill_form(form, predict_values(form), only_empty=True)
        fill_textareas(form)
        handle_dropdowns(form, driver)
        handle_custom_choices(driver, form)
        handle_checkbox(driver, form)
        before = page_state(driver)
        driver.execute_script("const f = arguments[0];"
                              "const b = Array.from(f.querySelectorAll('button, input[type=submit]')).find(x => (x.getAttribute('type') || 'submit').toLowerCase() === 'submit');"
                              "setTimeout(() => { if (b) b.click(); else if (f.requestSubmit) f.requestSubmit(); else f.submit(); }, 0);", form)
        after = wait_for_outcome(driver, before)
        still = driver.execute_script(PROFILE_GATE_JS)
        logging.info(f"Profile gate on {start}: fields {gate['fields']}, {gate['links']} links on the page; filled in and submitted; "
                     f"now at {after['url']}" + (f"; messages {new_errors(before, after)}" if new_errors(before, after) else "")
                     + ("; the profile form is still there" if still and after['url'] == start else ""))
    except Exception as e:
        logging.info(f"Profile gate could not be passed: {str(e).splitlines()[0] if str(e) else e!r}")

# Save and crawl the site from the current, logged-in page. The folder is named after
# folder_url: the login page, or the page that was already logged in when no login was needed.
def crawl_logged_in(driver, folder_url, base_domain, original_window):
    global logged_in
    logged_in = True
    dismiss_blocking_popups(driver)  # before the links are read: an overlay can hide or replace them
    pass_profile_gate(driver)
    site_evidence.add('crawl_logged_in', folder_url, site_evidence.is_onsite(folder_url))
    try:  # the page after login and the back-office pages it links to (deposit, withdraw, referral, plans)
        site_evidence.add_page(driver.page_source, driver.current_url, 'account', count=False)
        site_evidence.add_backoffice(driver.execute_script(
            "return Array.from(document.querySelectorAll('a[href]')).map(a => [a.href, (a.innerText || '').trim()]);") or [],
            driver.current_url)
    except Exception as e:
        logging.info(f"HYIP check: could not read the page after login: {str(e).splitlines()[0] if str(e) else e!r}")

    directory_name = windows_safe(folder_url.replace('://', '_').replace('/', '_'))
    os.makedirs(directory_name, exist_ok=True)
    if current_site_url:
        with open(os.path.join(directory_name, 'source_url.txt'), 'w') as f:
            f.write(current_site_url + '\n')
    processed_links = set()

    links = driver.find_elements(By.TAG_NAME, 'a')
    process_links(driver, links, processed_links, base_domain, directory_name, original_window, max_links=25)

# Some sites log the new account in when the confirmation link is clicked (likely on
# cryptocoinprop.com, where the login search then found no login form in 25 pages, which
# fits a logged-in visitor being sent away from /login/). A visible Logout link on the
# current page means no login is needed.
def crawl_if_already_logged_in(driver, where):
    state = page_state(driver)
    if not state["logout"]:
        return False
    logging.info(f"Already logged in ({where}): {state['url']} has a Logout link; skipping the login step")
    crawl_logged_in(driver, state["url"], get_base_domain(state["url"]), driver.current_window_handle)
    return True

#Validate links to avoid clicking on logout/signout buttons
# Language switchers lead back to the same page in another language; on swantradecapital.com
# 16 of the login search's 25 pages were /locale/fr, /locale/es, ...
# Bare language paths too (aziminer.com/de, /it, /es, /fr), by known language code only, so a
# two-letter page such as /fx is still followed.
LANGUAGE_LINK_RE = re.compile(r"/(locale|lang|language)/[a-z]{2}([_-][a-z]{2})?/?$|[?&](lang|locale|hl|language)="
                              r"|^https?://[^/]+/(en|de|fr|es|it|pt|ru|ar|zh|ja|ko|tr|nl|pl|hi|id|vi|th|fa|ur|bn|sv|no|da|fi"
                              r"|cs|sk|ro|hu|el|bg|uk|he|ms|tl|sw)([_-][a-z]{2})?/?$", re.I)

# Links that end the session. Only "logout" and "signout" were skipped, and fxtastytrades.com's
# /user/logoff logged the crawl out: every page saved after it was the "Sign In" page.
SESSION_END_RE = re.compile(r"log-?out|log-?off|sign-?out|sign-?off|/exit(?:[/?#.]|$)|a=logout", re.I)

def validate_link(href, processed_links, base_domain):
    return (href is not None and href not in processed_links and not SESSION_END_RE.search(href)
            and same_site(href, base_domain) and not LANGUAGE_LINK_RE.search(href))

# File and folder names are built from URLs. Windows rejects characters such as
# ':' and '?' in them and limits paths to 260 characters, so a local test run
# replaces those characters and shortens the names. Unchanged on the VMs.
def windows_safe(name):
    if os.name != 'nt':
        return name
    return re.sub(r'[<>:"\\|?*]', '_', name)[:100]

# --- Popups that cover the page after login ---------------------------------------------------
# swifttradinginc.live puts a full-screen terms dialog (a fixed overlay, no role="dialog") over
# every page until "I have read and accept" is ticked and "Accept & continue" is pressed, so every
# screenshot showed the dialog. Only checkboxes and buttons inside a fixed overlay or a dialog are
# touched: the accept box, an Accept/Agree/Continue/OK button, and "Not now" on install prompts.
POPUP_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const overlay = e => { for (let p = e; p && p !== document.body; p = p.parentElement) {
    if (p.tagName === 'DIALOG' || p.getAttribute('role') === 'dialog' || p.getAttribute('role') === 'alertdialog' ||
        p.getAttribute('aria-modal') === 'true' || getComputedStyle(p).position === 'fixed') return true; }
  return false; };
const text = e => (e.innerText || e.value || '').trim().replace(/\s+/g, ' ');
const boxes = Array.from(document.querySelectorAll('input[type=checkbox]')).filter(b => vis(b) && !b.checked && overlay(b) &&
    /\b(accept|agree|terms|i have read)\b/i.test(text(b.closest('label') || b.parentElement || b)));
const buttons = Array.from(document.querySelectorAll('button,[role=button],input[type=button],input[type=submit]')).filter(b => {
    const t = text(b);
    return vis(b) && overlay(b) && t.length <= 40 &&
        (/^(i )?(accept|agree)\b|^(continue|got it|ok|okay|i understand)$/i.test(t) || /^(not now|no,? thanks|maybe later|later)$/i.test(t)); });
return {boxes: boxes, buttons: buttons};
"""

def dismiss_blocking_popups(driver, rounds=3):
    for _ in range(rounds):
        try:
            found = driver.execute_script(POPUP_JS)
            if found['boxes']:
                for box in found['boxes']:
                    try:
                        box.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", box)
                    logging.info("Popup: ticked the accept box")
                time.sleep(0.5)  # the Accept button is disabled until the box is ticked
                found = driver.execute_script(POPUP_JS)
            clicked = False
            for button in found['buttons']:
                if not button.is_enabled():
                    continue
                label = (button.text or button.get_attribute('value') or '').strip()
                driver.execute_script("arguments[0].click();", button)
                logging.info(f"Popup: clicked {label!r}")
                clicked = True
            if not clicked:
                return
            time.sleep(1)
        except Exception as e:
            logging.info(f"Could not dismiss a popup: {e}")
            return

# --- Invest links ---------------------------------------------------------------------------
# swifttradinginc.live has no deposit page; each of its 32 plans has an "Invest" link
# (/invest?sub=cash-elite&cycle=168) that leads to the payment step. The deposit step matched
# only URLs with "deposit" in them, so it never ran there. Links whose path is /invest (or
# ?a=invest) are collected with the plan minimum shown on their card, and only the cheapest
# one is opened, as a deposit page: one plan is enough to reach the payment options, and
# opening all 32 would use up the link limit.
INVEST_LINKS_JS = r"""
const out = [];
for (const a of document.querySelectorAll('a[href]')) {
  let u; try { u = new URL(a.href); } catch (e) { continue; }
  if (!/(^|\/)invest(\/|$)/i.test(u.pathname) && !/[?&]a=invest(&|$)/i.test(u.search)) continue;
  let min = null;
  for (let e = a, i = 0; e && i < 6 && min === null; e = e.parentElement, i++) {
    const m = (e.innerText || '').match(/\bmin(?:imum)?\b[^\d$]{0,20}\$?\s*([\d,]+(?:\.\d+)?)/i);
    if (m) min = parseFloat(m[1].replace(/,/g, ''));
  }
  out.push([a.href, min]);
}
return out;
"""

def page_links(driver):
    """Absolute hrefs of the links on the current page, and its invest links as [href, minimum]."""
    hrefs = driver.execute_script("return Array.from(document.querySelectorAll('a[href]')).map(a => a.href);")
    return hrefs, driver.execute_script(INVEST_LINKS_JS)

# Opens href in a new tab, saves it, closes the tab, and returns the links on that page so the
# second layer of process_links() follows them. The second layer used to read the links again
# from the post-login page the crawler had switched back to, so it never went deeper than the
# first layer: swifttradinginc.live's /plans page and its Invest links were never looked at.
def process_link(driver, href, directory_name, original_window):
    if session_lost:
        return [], []  # logged out and could not log in again: the pages would all be the login page
    driver.execute_script("window.open(arguments[0]);", href)
    driver.switch_to.window(driver.window_handles[-1])
    prepare_tab(driver)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    time.sleep(3)
    pass_bot_wall(driver)
    if looks_logged_out(driver) and not SESSION_END_RE.search(href) and not LOGIN_URL_RE.search(href):
        if not relogin_here(driver):
            logging.info(f"Not saving {href}: the session is lost; no further pages are saved for this site")
            driver.close()
            driver.switch_to.window(original_window)
            return [], []
        driver.get(href)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time.sleep(3)
    dismiss_blocking_popups(driver)

    html_content = driver.page_source
    html_file_path = os.path.join(directory_name, f"page_{windows_safe(href.replace('://', '_').replace('/', '_').replace('?', '_'))}.html")
    with open(html_file_path, 'w', encoding='utf-8') as file:
        file.write(html_content)

    result = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
    screenshot_data = base64.b64decode(result['data'])
    screenshot_path = os.path.join(directory_name, f"screenshot_{windows_safe(href.replace('://', '_').replace('/', '_').replace('?', '_'))}.png")
    with open(screenshot_path, 'wb') as file:
        file.write(screenshot_data)

    logging.info(f"Captured and saved data for {href}")
    links_on_page = page_links(driver)
    driver.close()
    driver.switch_to.window(original_window)
    return links_on_page

# Wallet-looking strings on a page, for the deposit-stage log lines only. A hint for reading
# the log: iocs_searcher.sh still does the real extraction. Script and style blocks are
# dropped first so tokens inside them are not reported (see wallet_like for keyed scripts).
# addr1... is a Cardano (Shelley) address, as on swantradecapital.com's Cardano payment page.
# XRP (r...) and Dogecoin (D...) as on digital4xtrade.com, whose deposit passes showed
# rJn2zAPdFA193sixJwuFixRkYDUtx3apQh and DDHZqHFipkxPXxTS8MryJNfrUjeQNh9jCD that the log missed.
# Solana's bare base58 cannot be told apart from other long tokens on its own (see SOL_LIKE_RE).
WALLET_LIKE_RE = re.compile(r"\b(bc1[02-9ac-hj-np-z]{25,60}|[13][1-9A-HJ-NP-Za-km-z]{25,34}|0x[0-9a-fA-F]{40}|T[1-9A-HJ-NP-Za-km-z]{33}"
                            r"|addr1[02-9ac-hj-np-z]{50,110}|r[1-9A-HJ-NP-Za-km-z]{24,34}|D[5-9A-HJ-NP-U][1-9A-HJ-NP-Za-km-z]{32})\b")

# Litecoin (L.../M... legacy, ltc1 bech32): lscominer.com's LTC deposit page showed
# LcETEXr6t7TXVXihdU2FexyH8BM6vjdfTS (Oct 30) and the log had no pattern for it.
LTC_LIKE_RE = re.compile(r"\b(ltc1[02-9ac-hj-np-z]{25,60}|[LM][1-9A-HJ-NP-Za-km-z]{26,33})\b")
# Solana's bare base58 (32-44 characters, decoding to a 32-byte key) is only taken right after an
# address/wallet word: orvezantrades.com's <input id="walletAddress" value="Hwd..."> under "Solana
# Deposit Address", lscominer.com's "Wallet address:" box, coinprofx.live's "address":"7SnW...".
SOL_LIKE_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
ADDRESS_WORD_RE = re.compile(r"address|wallet", re.I)
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58_bytes(s):
    n = 0
    for c in s:
        n = n * 58 + B58_ALPHABET.index(c)
    return len(s) - len(s.lstrip('1')) + (n.bit_length() + 7) // 8

def mixed_case(w):
    """Digits, upper and lower case all present, as in real base58 addresses."""
    return bool(re.search(r"\d", w) and re.search(r"[A-Z]", w) and re.search(r"[a-z]", w))

def near_address_word(text, start, window):
    return bool(ADDRESS_WORD_RE.search(text[max(0, start - window):start]))

# Wallet-like strings in one piece of text. Solana only after an address/wallet word within
# `window` characters; the other patterns anywhere unless keyed is set.
def wallets_in(text, window=300, keyed=False):
    found = set()
    for m in WALLET_LIKE_RE.finditer(text):
        w = m.group(1)
        if ((w.startswith('0x') or not re.fullmatch(r"[0-9a-fA-F]+", w)) and (w[0] not in 'rD' or mixed_case(w))
                and (not keyed or near_address_word(text, m.start(), window))):
            found.add(w)
    for m in LTC_LIKE_RE.finditer(text):
        w = m.group(1)
        if (w.startswith('ltc1') or mixed_case(w)) and (not keyed or near_address_word(text, m.start(), window)):
            found.add(w)
    for m in SOL_LIKE_RE.finditer(text):
        w = m.group(0)
        if (w not in found and mixed_case(w) and not any(w in f or f in w for f in found)
                and b58_bytes(w) == 32 and near_address_word(text, m.start(), window)):
            found.add(w)
    return found

# Hidden inputs are dropped too, and all-hex strings are skipped: bitvestwise.net's hidden
# form_token 388d8db1cc6a514b9dafc5a8bb51c484 (an MD5) fits the legacy Bitcoin pattern.
# Script blocks are not scanned as page text, but an address in a script right after an
# address/wallet key counts: coinprofx.live's /deposit carries all its wallets in Next.js data
# ("asset":"BTC","network":"Bitcoin","address":"bc1qsz24...") and shows one only once a coin
# is picked, so its three wallets were missed (Oct 30).
def wallet_like(html_content):
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html_content, flags=re.S | re.I)
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", html_content, flags=re.S | re.I)
    visible = re.sub(r"<input\b[^>]*\btype=[\"']?hidden\b[^>]*>", " ", visible, flags=re.I)
    # Embedded data: URIs (base64 images) hold random letter runs; opextrade.com's pages gave
    # "rH1t9WP7Y1JjsfP9nnuHTUS9MxAAA" and similar r... strings out of them.
    visible = re.sub(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]+", " ", visible)
    # An r.../D... match must mix digits, upper and lower case, as real addresses do, so long
    # lower-case words starting with r are not taken for XRP addresses.
    found = wallets_in(visible)
    for script in scripts:
        script = re.sub(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]+", " ", script.replace('\\"', '"'))
        found |= wallets_in(script, window=40, keyed=True)
    return sorted(found)

# Deposit pages: "deposit"/"deposits" as a word in the URL, as before, or an add-funds path part
# (beacondigital.ltd's "Add Funds" is /dashboard/fund, minnercoinex.org's "Fund wallet" is
# fund.php), or a post-login link labelled like one. /transfer-funds and /withdraw-funds do not
# match: the path part has to start with the word.
DEPOSIT_URL_RE = re.compile(r"\bdeposits?\b|/(funds?|add-?funds?|fund-?(account|wallet)|top-?up|recharge)(?=[/?#.]|$)", re.I)
DEPOSIT_LINK_TEXT_RE = re.compile(r"^\s*(add funds|fund (your )?(account|wallet)|top ?up|deposit( funds)?|make a deposit)\s*$", re.I)

def process_links(driver, links, processed_links, base_domain, directory_name, original_window, max_links=25):
    if len(processed_links) >= max_links:
        logging.info(f"Reached maximum link processing threshold of {max_links}. Stopping further processing.")
        return

    deposit_page_counter = 0
    deposit_hrefs = []

    # The hrefs are read once, up front. Reading them from the link elements as the loop got to
    # them failed on swantradecapital.com: the dashboard had changed by then, a link was stale,
    # and the StaleElementReferenceException (raised outside the loop's try) ended the crawl.
    hrefs, deposit_by_text = [], set()
    for link in links:
        try:
            href = link.get_attribute('href')
            hrefs.append(href)
            if href and DEPOSIT_LINK_TEXT_RE.match(link.text or ''):
                deposit_by_text.add(href)
        except StaleElementReferenceException:
            continue

    def is_deposit_link(href):
        return bool(href) and (bool(DEPOSIT_URL_RE.search(href)) or href in deposit_by_text)

    # Logged so a site whose deposit page was never opened can be told apart from one whose
    # deposit page was opened but produced nothing (process_deposit_page logs that case).
    deposit_hrefs = sorted({h for h in hrefs if is_deposit_link(h)})
    logging.info(f"Post-login page has {len(hrefs)} links; deposit links: {deposit_hrefs or 'none'}")

    invest_links_seen = set()  # never opened as ordinary pages; one of them goes to the deposit step
    invest_done = False

    def wanted(href):
        return validate_link(href, processed_links, base_domain) and href not in invest_links_seen

    def open_page(href):
        """Open one link: deposit links go to the deposit step. Returns the links on the page
        (empty for deposit pages) as (hrefs, invest links)."""
        nonlocal deposit_page_counter
        processed_links.add(href)
        is_deposit = is_deposit_link(href)
        if is_deposit and deposit_page_counter < 5: # and within limit
            logging.info(f"Opening deposit page {href} (deposit link {deposit_page_counter + 1} of at most 5)")
            process_deposit_page(driver, href, directory_name, original_window)
            deposit_page_counter += 1
            return [], []
        if is_deposit:
            logging.info(f"Deposit link {href} handled as an ordinary page: the limit of 5 deposit pages was reached")
        return process_link(driver, href, directory_name, original_window)

    def try_invest(entries, where):
        nonlocal invest_done, deposit_page_counter
        entries = [e for e in entries if wanted(e[0])]
        invest_links_seen.update(e[0] for e in entries)
        if invest_done or not entries or deposit_page_counter >= 5:
            return
        # The lowest minimum first; links whose card shows no minimum come last.
        href, minimum = min(entries, key=lambda e: (e[1] is None, e[1] or 0))
        logging.info(f"Opening invest page {href} as a deposit page: "
                     + (f"minimum ${minimum:g}, " if minimum is not None else "")
                     + f"the cheapest of {len(entries)} invest link(s) on {where}")
        invest_done = True
        deposit_page_counter += 1
        processed_links.add(href)
        process_deposit_page(driver, href, directory_name, original_window)

    try:
        try_invest(driver.execute_script(INVEST_LINKS_JS), "the post-login page")
    except Exception as e:
        logging.info(f"Could not look for invest links on the post-login page: {e}")

    for href in hrefs:
        if wanted(href):
            try:
                second_hrefs, second_invest = open_page(href)
                try_invest(second_invest, href)

                for second_href in second_hrefs:
                    if wanted(second_href) and len(processed_links) < max_links:
                        _, third_invest = open_page(second_href)
                        try_invest(third_invest, second_href)
                    if len(processed_links) >= max_links:
                        logging.info("Reached max links limit during second-layer processing.")
                        break
            except UnexpectedAlertPresentException as e:
                alert = driver.switch_to.alert
                logging.warning(f"Unexpected alert present while processing {href}: {alert.text}")
                alert.accept()
                logging.info(f"Accepted unexpected alert for {href}. Moving to next link.")
            except Exception as e:
                logging.error(f"Failed to process {href}: {e}")
            finally:
                if original_window not in driver.window_handles:
                    logging.error("Original window has been closed.")
                else:
                    driver.switch_to.window(original_window)

    # Last, because clicking can navigate the post-login page away and leave `links` stale.
    if not deposit_hrefs and original_window in driver.window_handles:
        try:
            click_deposit_button(driver, directory_name, base_domain, original_window, processed_links)
        except Exception as e:
            logging.error(f"Deposit button step failed: {e}")
        finally:
            if original_window in driver.window_handles:
                driver.switch_to.window(original_window)

# --- Deposit buttons --------------------------------------------------------------------------
# swifttradinginc.live's dashboard has a "Deposit" <button> and no deposit link, and the crawler
# only followed <a href>. When the post-login page has no deposit link, the first visible button
# labelled like a deposit is clicked on that page and whatever it opens (a modal, a new route
# or a new tab) is saved. If it led to another page of the site, the deposit step runs there.
# Invest buttons count when there is no deposit button: aiblockchainmining.com's dashboard only has
# "Invest Now" / "Create Your First Investment", which open a "New Investment" dialog (plan cards,
# "Cryptocurrency" select, amount, "Proceed to Payment") that leads to a NOWPayments invoice.
# Symbols and icon glyphs around the words are dropped first: primedgetrading.com's button reads
# "+ Deposit" (a "+" span before the word) and was not taken for a deposit button (Oct 30).
DEPOSIT_BUTTONS_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const label = b => (b.innerText || b.value || '').replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim();
const all = Array.from(document.querySelectorAll('button,[role=button],input[type=button],input[type=submit]')).filter(b =>
    vis(b) && !b.disabled && b.tagName !== 'A');
const deposit = all.filter(b => /^(deposit|deposit funds|make a deposit|fund|fund account|fund wallet|add funds|top ?up)$/i.test(label(b)));
return deposit.length ? deposit : all.filter(b =>
    /^(invest now|invest|new investment|start investing|make an investment|create (your )?(first )?investment)$/i.test(label(b)));
"""

# A payment processor's hosted invoice (NOWPayments on aiblockchainmining.com: "Payment ID:
# 5296811223", BTC 3DEBx5XYNBS8MZ9jMjJLcGtXQF7rj2kCNn) shows an address the processor likely issues
# per invoice, not one the site reuses, so the log names the processor host and payment ID.
PAYMENT_ID_RE = re.compile(r"payment\s*id\s*[:#]?\s*([A-Za-z0-9-]{6,40})", re.I)

def save_page(driver, directory_name, stem):
    with open(os.path.join(directory_name, f"{stem}.html"), 'w', encoding='utf-8') as file:
        file.write(driver.page_source)
    result = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
    with open(os.path.join(directory_name, f"{stem}.png"), 'wb') as file:
        file.write(base64.b64decode(result['data']))

# The page after a sign-up the site refused or gave no sign of success to, and after a failed login,
# is saved in failed_pages/ (HTML and screenshot). On the Nov 1 blind run the cause of four failures
# could not be read from the log: oneexclusivecapitalbitfx.com's and brightpathwealthassets.com's
# GoldCoders sign-ups came back with no message, uphilltradex.com's zip and answer fields stayed
# empty, and tradecoineasily.com's login failed after a sign-up that looked successful.
def save_failed_page(driver, what):
    try:
        os.makedirs('failed_pages', exist_ok=True)
        stem = windows_safe(f"{what}_{urlparse(driver.current_url).netloc}_{datetime.now().strftime('%H%M%S')}")
        save_page(driver, 'failed_pages', stem)
        logging.info(f"Saved the page after the {what.replace('_', ' ')} as failed_pages/{stem}.html and .png")
    except Exception as e:
        logging.info(f"Could not save the page after the {what.replace('_', ' ')}: {str(e).splitlines()[0] if str(e) else e!r}")

def deposit_passes_in_dialog(driver, start_url, label, directory_name, stem):
    """Run the deposit passes on a dialog that the deposit/invest button opened on start_url: fill
    and submit the form in it, save where that lands (on another site too), and reopen the dialog
    for the next payment option."""
    found = driver.execute_script(DEPOSIT_FORM_JS)
    form, plan, payment = found.get('form'), found.get('plan'), found.get('payment')
    if not form:
        logging.info(f"Deposit button {label!r}: no deposit form in what it opened")
        return
    plan_choice = plan['options'][0] if plan else None
    options = (payment['options'] if payment and payment['kind'] != 'button' else [None])[:MAX_DEPOSIT_PASSES]
    logging.info(f"Deposit button {label!r}: form #{form['index']} {form['name']!r} in the dialog; amount field {form['amount']!r}; "
                 + (f"payment {payment['kind']} {payment['name']!r} with {len(payment['options'])} options" if payment else "no payment-method control")
                 + f"; {len(options)} passes planned")
    site = get_base_domain(start_url)
    for counter, option in enumerate(options, start=1):
        if counter > 1:
            driver.get(start_url)
            time.sleep(3)
            again = driver.execute_script(DEPOSIT_BUTTONS_JS)
            if not again:
                logging.info(f"Deposit button {label!r}: not on the page again for pass {counter}, stopping")
                break
            driver.execute_script("arguments[0].click();", again[0])
            time.sleep(3)
        state = driver.execute_script(
            DEPOSIT_FILL_JS, form['index'],
            {'kind': plan['kind'], 'name': plan['name'], 'value': plan_choice['value']} if plan else None,
            {'kind': payment['kind'], 'name': payment['name'], 'value': option['value']} if option else None,
            form['amount'], (plan_choice or {}).get('range') or (option or {}).get('range'))
        if state.get('error'):
            logging.info(f"Deposit button {label!r} pass {counter}: {state['error']}, stopping")
            break
        time.sleep(CHOICE_WAIT)  # the address may show once the option is chosen (see CHOICE_WAIT)
        before = wallet_like(driver.page_source + " " + (driver.execute_script(INPUT_VALUES_JS) or ""))
        if before:
            save_page(driver, directory_name, f"{stem}_{counter}_chosen")
            site_evidence.add_wallets(before, start_url)
            logging.info(f"Deposit button {label!r} pass {counter}: after choosing {(option or {}).get('label', 'site default')!r}, "
                         f"before submitting: wallet-like strings {before}")
        how = driver.execute_script(DEPOSIT_SUBMIT_JS, form['index'])
        found_wallets = []
        for _ in range(6):  # a hosted invoice renders its address a few seconds after loading
            time.sleep(2)
            found_wallets = wallet_like(driver.page_source)
            if found_wallets:
                break
        landed = driver.current_url
        save_page(driver, directory_name, f"{stem}_{counter}")
        processor = ""
        if not same_site(landed, site):
            pid = PAYMENT_ID_RE.search(driver.execute_script("return document.body ? document.body.innerText : '';") or "")
            processor = (f"; payment processor page on {urlparse(landed).netloc}" + (f", payment ID {pid.group(1)}" if pid else "")
                         + " (its address may be issued for this invoice only)")
        logging.info(f"Deposit button {label!r} pass {counter}: payment {(option or {}).get('label', 'site default')!r}; "
                     f"submitted with {state} via {how}; now at {landed}; wallet-like strings {found_wallets or 'none'}{processor}")
    driver.get(start_url)

# Plan pages as the way in when the post-login page has neither a deposit link nor a deposit or
# invest button: chainfundcapital.com's "Deposit Fund" links (/user/user/add-fund) are inside HTML
# comments, and buying a plan is the only way to pay: /user/plan has an "Invest Now" button per
# plan that opens a dialog with an amount and "Invest" (checkout) (Oct 30). The same-site links on
# the post-login page whose path names plans or investing are opened in turn (at most 2) and the
# first one with an invest button goes through the deposit-button step.
PLAN_PAGE_LINKS_JS = r"""
const seen = new Set(), here = location.href.split('#')[0];
return Array.from(document.querySelectorAll('a[href]')).map(a => a.href.split('#')[0]).filter(h => {
  let u; try { u = new URL(h); } catch (e) { return false; }
  if (u.host !== location.host || h === here || seen.has(h)) return false;
  if (!/(^|\/)(plans?|investments?|invest|packages?|pricing|tariffs?)(\/|\.php|\.html|$)/i.test(u.pathname)) return false;
  if (/histor|log|record/i.test(u.pathname)) return false;
  seen.add(h);
  return true;
});
"""

def click_deposit_button(driver, directory_name, base_domain, original_window, processed_links):
    buttons = driver.execute_script(DEPOSIT_BUTTONS_JS)
    if not buttons:
        post_login = driver.current_url
        plan_pages = (driver.execute_script(PLAN_PAGE_LINKS_JS) or [])[:2]
        if not plan_pages:
            logging.info("No deposit link or deposit button on the post-login page")
            return
        for plan_page in plan_pages:
            driver.get(plan_page)
            time.sleep(3)
            buttons = driver.execute_script(DEPOSIT_BUTTONS_JS)
            if buttons:
                logging.info(f"No deposit link or deposit button on the post-login page; the plan page {plan_page} has "
                             f"{len(buttons)} deposit/invest button(s)")
                break
            logging.info(f"Plan page {plan_page}: no deposit or invest button")
        if not buttons:
            logging.info("No deposit link or deposit button on the post-login page or its plan pages")
            driver.get(post_login)
            return
    button = buttons[0]
    # Whitespace collapsed: primedgetrading.com's "+\nDeposit" put a line break into the file name.
    label = re.sub(r"\s+", " ", button.text or button.get_attribute('value') or '').strip()
    start_url = driver.current_url
    handles_before = set(driver.window_handles)
    driver.execute_script("arguments[0].click();", button)
    time.sleep(3)
    new_tabs = [h for h in driver.window_handles if h not in handles_before]
    if new_tabs:
        driver.switch_to.window(new_tabs[0])
        prepare_tab(driver)

    stem = windows_safe(f"deposit_button_{label}_" + start_url.replace('://', '_').replace('/', '_').replace('?', '_'))
    save_page(driver, directory_name, stem)
    now_url = driver.current_url
    logging.info(f"Deposit button {label!r} on {start_url}: "
                 + (f"opened a new tab at {now_url}" if new_tabs else
                    f"now at {now_url}" if now_url != start_url else "same URL (a dialog or an in-page panel)")
                 + f"; saved as {stem}; wallet-like strings {wallet_like(driver.page_source) or 'none'}")
    if not new_tabs and now_url == start_url:
        deposit_passes_in_dialog(driver, start_url, label, directory_name, stem)
        return

    if new_tabs:
        driver.close()
        driver.switch_to.window(original_window)
    elif now_url != start_url:
        driver.get(start_url)
    if now_url != start_url and get_base_domain(now_url) == base_domain and now_url not in processed_links:
        processed_links.add(now_url)
        logging.info(f"Opening {now_url} (reached with the deposit button) as a deposit page")
        process_deposit_page(driver, now_url, directory_name, original_window)

# --- Deposit step ----------------------------------------------------------------------------
# One pass per payment option that the site's own deposit form lists (dropdown or radios),
# never a built-in coin list. Each pass picks the option, submits, and saves the page that
# comes back (on HYIP Manager sites, "Please confirm your deposit" with the wallet address).
#
# Until Oct 20 the loop ran over itertools.product(every radio value on the page, each
# <select>'s option list, ['1000']). On the HYIP Manager form (plan <select name="h_id">,
# payment radios name="type", amount) that failed three ways, seen in the 186 deposit pages
# saved on VM1-3 by Oct 20:
#   - set_dropdown_options() selected one option value character by character, so the plan
#     stayed on its blank first option. On 6 of the 19 sites every pass came back as the form,
#     with no wallet (fundamentaltradegroup, metrospacefinance, lotech-global, ...).
#   - clear_input_fields() clicked every radio, so passes that changed the plan submitted the
#     last payment radio: bitvestwise spent 4 of its 8 passes on USDT TRC20.
#   - A form without a radio or without a <select> got zero passes and left no trace.
# Now the deposit form is the one holding the amount field. The plan control is the choice
# control whose name or options read like plans (h_id, "plan", %, days, min/max); the payment
# control is the other one. Each pass reloads the page and sets the controls by name in
# JavaScript, which also reaches hidden radios and select2-wrapped selects, so nothing goes stale.
MAX_DEPOSIT_PASSES = 12
# Seconds to wait after choosing a payment option before reading the page and submitting. Some
# sites fetch the method's details when it is chosen: trade-earnerxsenergy.com's gateway dropdown
# loads /user/deposit/gateway/<code> and inserts its "credentials" into the form, and the crawler
# submitted at once; the form came back twice with no message and nothing was read in between (Nov 1).
CHOICE_WAIT = 2

# Shared by the deposit-step scripts below. formAt(i) is document.forms[i], or for i = -1 the
# deposit box of a page without a deposit <form>: beacondigital.ltd's "Add Funds" page is a React
# box with a coin <select>, an amount input and a "Proceed to Deposit" button, none of them in a
# form or with a name or id. The box is the nearest ancestor of a visible deposit-like button
# (outside any form and outside menus) that also holds a visible amount input and a dropdown or
# radios. Controls are keyed by name, else id, else tag and position ("@select0"), which is the
# same on every reload. React keeps its own copy of input values, so values are set through the
# native value setter and announced with input/change events.
DEPOSIT_HELPERS_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const BOX_BUTTON = /deposit|proceed|pay|continue|submit|fund|confirm/i;
function depositBox() {
  const buttons = Array.from(document.querySelectorAll('button, [role=button], input[type=button], input[type=submit]'))
    .filter(b => vis(b) && BOX_BUTTON.test(clean(b.innerText || b.value))
            && !b.closest('form, nav, header, aside, footer, [class*=sidebar], [class*=menu], [class*=navbar]'));
  for (const b of buttons) {
    let box = b.parentElement;
    for (let i = 0; box && i < 6; i++, box = box.parentElement) {
      const amount = Array.from(box.querySelectorAll('input')).some(e => vis(e) && ['number', 'text', 'tel'].includes(e.type));
      if (amount && box.querySelector('select, input[type=radio]')) return box;
    }
  }
  return null;
}
function formAt(index) { return index >= 0 ? document.forms[index] : depositBox(); }
function controlsOf(f) { return f.elements ? Array.from(f.elements) : Array.from(f.querySelectorAll('input, select, textarea, button')); }
function keyOf(e, els) {
  return e.name || e.id || ('@' + e.tagName.toLowerCase() + els.filter(x => x.tagName === e.tagName).indexOf(e));
}
function setValue(e, v) {
  const proto = e.tagName === 'SELECT' ? HTMLSelectElement.prototype : e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, v);
}
"""

# Payment methods chosen with a button per method. On swantradecapital.com the deposit form holds
# only the amount and an empty hidden payment_method; a "Select Deposit Method" table outside the
# form has a "Deposit" button per method (onclick="checkpamethd('1')") that fetches the method and
# writes it into the hidden field, and the site refuses the form while it is empty ("Please choose
# a payment method by clicking on it"). securenovatrade.com has the same template with the "Select"
# buttons inside the form, so buttons inside the form count too, except its submit button. Menus
# are left out, the buttons must be labelled like a choice, and when several groups qualify the
# largest group of buttons that call the same onclick function (or share a label) wins. Each gets
# the label of its table row or card, and the row text for the amount range ("Min: $50.00").
METHOD_BUTTONS_JS = r"""
function methodButtons(form) {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const LABEL = /^(deposit|deposit now|select|choose|pay|pay now|use|use this|proceed|continue|fund)$/i;
  const outside = b => !b.closest('nav, header, aside, footer, [class*=sidebar], [class*=menu], [class*=navbar]');
  const fnOf = b => ((b.getAttribute('onclick') || '').match(/^\s*([\w$.]+)\s*\(/) || [])[1];
  let cands = Array.from(document.querySelectorAll('button, a, [role=button], input[type=button]')).filter(b =>
      vis(b) && !(form && form.contains(b) && (b.type || '').toLowerCase() === 'submit') && LABEL.test(clean(b.innerText || b.value))
      && outside(b));
  // Method cards labelled with the coin itself: megatradeoptions.com's 10 cards are
  // <a data-method="Bitcoin" onclick="checkpamethd(this.id)"> reading "Bitcoin", "USDT", ...,
  // which the label test above never matched, so its deposit went out with no method (Oct 30).
  // Two or more visible elements calling the same onclick function count then.
  let fromCards = false;
  if (!cands.length) {
    cands = Array.from(document.querySelectorAll('a[onclick], button[onclick], [role=button][onclick], div[onclick], label[onclick]'))
        .filter(b => vis(b) && fnOf(b) && outside(b) && !(form && form.contains(b) && (b.type || '').toLowerCase() === 'submit'));
    fromCards = true;
  }
  const groups = {};
  cands.forEach(b => {
    const fn = fnOf(b);
    const key = fn ? 'fn:' + fn : 'text:' + clean(b.innerText || b.value).toLowerCase();
    (groups[key] = groups[key] || []).push(b);
  });
  let best = Object.values(groups).sort((a, b) => b.length - a.length)[0] || [];
  if (fromCards && best.length < 2) best = [];
  return best.map(b => {
    if (b.getAttribute('data-method')) {
      const row = b.closest('tr, li, [class*=card], [class*=method], [class*=item]') || b;
      return {el: b, label: clean(b.getAttribute('data-method')).slice(0, 60), text: clean(row.innerText).slice(0, 300)};
    }
    const row = b.closest('tr, li, [class*=card], [class*=method], [class*=item]') || b.parentElement;
    let label = row && row.tagName === 'TR' && row.cells.length ? clean(row.cells[0].innerText) : '';
    if (!label) { const img = row && row.querySelector('img[alt]'); label = img ? clean(img.alt) : ''; }
    if (!label) label = clean((row ? row.innerText : '').replace(b.innerText || '', ''));
    return {el: b, label: label.slice(0, 60), text: clean(row ? row.innerText : '').slice(0, 300)};
  });
}
"""

# Clicks method button number arguments[1] (in methodButtons() order) on the reloaded page.
METHOD_CLICK_JS = DEPOSIT_HELPERS_JS + METHOD_BUTTONS_JS + r"""
const f = formAt(arguments[0]);
const b = methodButtons(f)[parseInt(arguments[1])];
if (!b) return {error: 'method button ' + arguments[1] + ' is not on the reloaded page'};
window.alert = () => {}; window.confirm = () => true;
// megatradeoptions.com's method cards are <a href=""> links: following the href reloads the page
// while the onclick's fetch is filling the payment field. Only the onclick should run.
if (b.el.tagName === 'A' && /^(#.*|javascript:.*|)$/i.test((b.el.getAttribute('href') || '').trim())) {
  b.el.addEventListener('click', e => e.preventDefault(), {once: true});
}
b.el.click();
return {label: b.label};
"""

# The value of the form's field arguments[1], which the method button should have filled.
FIELD_VALUE_JS = DEPOSIT_HELPERS_JS + r"""
const f = formAt(arguments[0]);
if (!f) return null;
const els = controlsOf(f);
const e = els.find(x => keyOf(x, els) === arguments[1]);
return e ? e.value : null;
"""

# Deposit forms that sit in a hidden dialog until a button outside it opens them. assetgain.org's
# /deposit lists 4 gateways, each with a ".paynow" button (data-href="/paynow/34", data-id="34");
# the page's script copies those into the dialog form's empty action and hidden id and shows it.
# Submitting the form without that click posted to /deposit and got "405 Method Not Allowed" (Nov 1),
# as trillionssystemslimited.pro's ".paynow" did (Nov 1). The openers are the visible buttons that
# point at the dialog (data-bs-target / data-target / href="#id"), else the largest group of
# visible buttons of one class that each carry their own data-href/-id/-method/... value. Each gets
# the label of its card or row. One pass per opener: click it, wait for the form to show, fill, submit.
OPENERS_JS = r"""
function openers(f) {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const box = f.closest('.modal, [role=dialog], [class*=modal], [id*=modal], [class*=popup]') || f;
  const outside = b => !b.closest('nav, header, aside, footer, [class*=sidebar], [class*=menu], [class*=navbar]');
  const cands = Array.from(document.querySelectorAll('button, a, [role=button], input[type=button]'))
      .filter(b => vis(b) && !box.contains(b) && outside(b));
  let found = [];
  if (box.id) {
    found = cands.filter(b => ['data-bs-target', 'data-target', 'href'].some(a => (b.getAttribute(a) || '') === '#' + box.id));
  }
  if (!found.length) {
    const groups = {};
    cands.forEach(b => {
      const data = Array.from(b.attributes).map(a => a.name)
          .filter(n => /^data-(href|id|method|gateway|currency|coin|code|url|action|value|name)$/i.test(n)).sort().join(',');
      if (!data) return;
      const key = String(b.className || b.tagName) + '|' + data;
      (groups[key] = groups[key] || []).push(b);
    });
    found = Object.values(groups).sort((a, b) => b.length - a.length)[0] || [];
  }
  return found.map(b => {
    const row = b.closest('tr, li, [class*=card], [class*=method], [class*=gateway], [class*=item]') || b.parentElement;
    let label = '';
    const img = row && row.querySelector('img[alt]');
    if (img && clean(img.alt)) label = clean(img.alt);
    if (!label) label = clean((row ? row.innerText : '').replace(b.innerText || '', '')) || clean(b.innerText || b.value);
    return {el: b, label: label.slice(0, 60), text: clean(row ? row.innerText : '').slice(0, 300)};
  });
}
"""

# Clicks opener number arguments[1] (in openers() order) of the hidden form arguments[0].
OPENER_CLICK_JS = DEPOSIT_HELPERS_JS + OPENERS_JS + r"""
const f = formAt(arguments[0]);
if (!f) return {error: 'the deposit form is not on the reloaded page'};
const o = openers(f)[parseInt(arguments[1])];
if (!o) return {error: 'opener ' + arguments[1] + ' is not on the reloaded page'};
window.alert = () => {}; window.confirm = () => true;
if (o.el.tagName === 'A' && /^(#.*|javascript:.*|)$/i.test((o.el.getAttribute('href') || '').trim())) {
  o.el.addEventListener('click', e => e.preventDefault(), {once: true});
}
o.el.click();
return {label: o.label};
"""

# Whether the deposit form arguments[0] is visible (its dialog has opened).
FORM_SHOWN_JS = DEPOSIT_HELPERS_JS + r"""
const f = formAt(arguments[0]);
return !!f && vis(f);
"""

DEPOSIT_FORM_JS = DEPOSIT_HELPERS_JS + METHOD_BUTTONS_JS + OPENERS_JS + r"""
const PLACEHOLDER = /^(select|choose|please|--|-)/i;
const PLANLIKE = /%|\bdaily\b|\bhours?\b|\bdays?\b|\bweeks?\b|\bprofit|\bmin(imum)?\b|\bmax(imum)?\b|\bplan\b|\bpackage\b|\blevel\b/i;
const PLANNAME = /h_id|plan|package|scheme|tariff|level/i;
const PAYNAME = /type|pay|method|gateway|currency|coin|wallet|processor|system|ps$|ec$/i;
const AMOUNTNAME = /amount|sum/i;
// An option naming a coin or an e-currency ("BTC", "USDT (TRC20)", "Perfect Money"); a trading pair
// ("USDT/BTC" in a trade panel) is not one.
const COIN_OPTION = /^\s*(bitcoin|btc|ethereum|eth|usdt|tether|usdc|bnb|binance|tron|trx|litecoin|ltc|dogecoin|doge|xrp|ripple|solana|sol|xlm|stellar|bch|bitcoin cash|cardano|ada|dash|ton|matic|polygon|busd|dai|shib|perfect ?money|payeer|advcash|epaycore)\b/i;
const PAIR = /^\s*[a-z]{2,6}\s*\/\s*[a-z]{2,6}\s*$/i;
const DEPOSIT_WORD = /deposit|fund|\bpay|recharge|top.?up|add.?money|checkout|invest/i;
const OTHER_FORM = /withdraw|transfer|exchange|swap|convert|send.?money|logout|log.?out|search|subscribe|newsletter|\blog ?in\b|sign ?in/i;
function range(s) {
  const m = (s || '').match(/\$?\s?([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*\$?\s?([\d,]+(?:\.\d+)?)/i);
  if (m) return [parseFloat(m[1].replace(/,/g, '')), parseFloat(m[2].replace(/,/g, ''))];
  const n = (s || '').match(/\bmin(?:imum)?\b[^0-9$]{0,15}\$?\s?([\d,]+(?:\.\d+)?)/i);
  return n ? [parseFloat(n[1].replace(/,/g, '')), null] : null;
}
function radioText(r) {
  // The radio's own row or label, else the text right after it; then the enclosing table
  // or card, where plan tables put the "$50.00 - $549.00" range.
  // A row holding several radios belongs to none of them; then the text up to the next
  // input or <br> is the label.
  let row = r.closest('tr, li, label');
  if (row && row.querySelectorAll('input[type=radio]').length > 1) row = null;
  let label = clean(row ? row.innerText || row.textContent : '');
  if (label.length < 2) {
    let n = r.nextSibling, s = '';
    while (n && s.length < 120 && !(n.nodeType === 1 && (['BR', 'INPUT'].includes(n.tagName) || n.querySelector('input')))) {
      s += ' ' + (n.textContent || ''); n = n.nextSibling;
    }
    label = clean(s);
  }
  const box = r.closest('table, .card, .plan, .package, div');
  return [label.slice(0, 160), clean(box ? box.innerText || box.textContent : '').slice(0, 400)];
}
// index -1 describes the deposit box of a page without a deposit form (see DEPOSIT_HELPERS_JS);
// there a visible number input counts as the amount field even without a telling name.
function describe(f, index) {
  const els = controlsOf(f);
  const amount = els.find(e => e.tagName === 'INPUT'
      && ['text', 'number', 'tel', ''].includes((e.getAttribute('type') || '').toLowerCase())
      && (AMOUNTNAME.test([e.name, e.id, e.placeholder].join(' ')) || (index < 0 && e.type === 'number' && vis(e))));
  // Fields are keyed by name, or by id when they have none (digital4xtrade.com's crypto deposit
  // form is <input id="cryptoAmount"> and <select id="cryptoMethod">), else by position.
  const controls = [];
  els.filter(e => e.tagName === 'SELECT' && !e.disabled).forEach(s => {
    const options = Array.from(s.options)
      .filter(o => !o.disabled && o.value !== '' && !PLACEHOLDER.test(clean(o.text)))
      .map(o => ({value: o.value, label: clean(o.text).slice(0, 80), range: range(o.text)}));
    controls.push({kind: 'select', name: keyOf(s, els), options});
  });
  const groups = {};
  els.filter(e => e.type === 'radio' && !e.disabled && e.name && e.value !== '').forEach(r => {
    const [label, box] = radioText(r);
    (groups[r.name] = groups[r.name] || []).push({value: r.value, label: label.slice(0, 80), range: range(label) || range(box)});
  });
  Object.entries(groups).forEach(([name, options]) => controls.push({kind: 'radio', name, options}));
  controls.forEach(c => {
    c.plan = PLANNAME.test(c.name)
      || (c.options.length > 0 && 2 * c.options.filter(o => PLANLIKE.test(o.label)).length >= c.options.length);
  });
  const submits = els.filter(e => (e.tagName === 'BUTTON' && (e.getAttribute('type') || 'submit').toLowerCase() === 'submit')
      || (e.tagName === 'INPUT' && ['submit', 'image'].includes(e.type)));
  // What the form says about itself: its action, id, name, class and submit labels. (getAttribute,
  // not f.id: a field named "id" shadows the form's id, and assetgain.org's form name came out as a
  // WebElement in the log.)
  const own = [f.getAttribute('action'), f.getAttribute('id'), f.getAttribute('name'), f.getAttribute('class')]
      .concat(submits.map(b => b.innerText || b.value)).join(' ');
  const labels = submits.map(b => clean(b.innerText || b.value).toLowerCase());
  const coins = controls.some(c => PAYNAME.test(c.name) || c.options.filter(o => COIN_OPTION.test(o.label) && !PAIR.test(o.label)).length >= 2);
  return {index, name: index < 0 ? 'deposit box without a form' : (f.getAttribute('name') || f.getAttribute('id') || ''),
          amount: amount ? keyOf(amount, els) : null, controls: controls.filter(c => c.options.length), submit: submits.length > 0,
          coins, depositWord: DEPOSIT_WORD.test(own),
          other: OTHER_FORM.test(own) || (labels.includes('buy') && labels.includes('sell'))};
}
// The page's own deposit form, not the form with the most controls. vintageprofessionaloptions.com's
// deposit page also carries the "New Trade" side panel that is on every page of its back office
// (trade_amount, six dropdowns, Buy/Sell); it won on control count, and the pass clicked "Sell"
// (Nov 1). mail.keystonescapital.com's withdraw form #transferCrypto beat its #deposit form the same
// way (Nov 1). A form whose choices are coins or payment methods, or that calls itself a deposit,
// ranks first; a withdraw, transfer, exchange or Buy/Sell trade form is only used when nothing else is there.
const described = Array.from(document.forms).map((f, index) => describe(f, index));
const rank = d => (d.amount ? 4 : 0) + (d.coins ? 6 : 0) + (d.depositWord ? 3 : 0) - (d.other && !d.depositWord ? 12 : 0)
    + 0.5 * d.controls.length;
const candidates = described.filter(d => d.amount || (d.controls.length && d.submit)).sort((a, b) => rank(b) - rank(a));
let form = candidates.find(d => !(d.other && !d.depositWord)) || candidates[0];
if (!form) {
  const box = depositBox();
  const d = box ? describe(box, -1) : null;
  // An amount and a send button are enough: aiblockchainmining.com's "New Investment" dialog has a
  // disabled "Cryptocurrency" select (fixed BTC per plan), so it had no choice control and was refused.
  if (d && d.amount && (d.controls.length || d.submit)) form = d;
}
if (!form) return {forms: described.length};
const plan = form.controls.find(c => c.plan) || null;
const rest = form.controls.filter(c => c !== plan);
let payment = rest.find(c => PAYNAME.test(c.name)) || rest.sort((a, b) => b.options.length - a.options.length)[0] || null;
if (!payment) {
  // No radio or dropdown for the payment: an empty hidden payment field filled by method buttons.
  const f = formAt(form.index);
  const hidden = controlsOf(f).find(e => e.type === 'hidden' && !e.value && (e.name || e.id)
      && PAYNAME.test(e.name || e.id) && !/token|csrf/i.test(e.name || e.id));
  const buttons = hidden ? methodButtons(f) : [];
  if (buttons.length) {
    payment = {kind: 'button', name: hidden.name || hidden.id,
               options: buttons.map((b, i) => ({value: String(i), label: b.label, range: range(b.text)}))};
  }
}
// A form that is not shown is opened by its buttons (see OPENERS_JS); those become the passes.
let opener = [];
if (form.index >= 0 && !vis(formAt(form.index))) {
  opener = openers(formAt(form.index)).map((o, i) => ({value: String(i), label: o.label, range: range(o.text)}));
}
return {forms: described.length, form, plan, payment, openers: opener};
"""

# Sets the plan, the amount and one payment option on the reloaded page, and returns what the
# form holds afterwards. The plan goes first because some sites refill the amount when the plan
# changes; that amount is kept when it lies inside the plan's range.
DEPOSIT_FILL_JS = DEPOSIT_HELPERS_JS + r"""
const [index, plan, payment, amountName, range] = arguments;
const f = formAt(index);
if (!f) return {error: 'the deposit form is not on the reloaded page'};
// The crawler's alert/confirm override (handle_alert) only covered the first page; a
// "Are you sure?" confirm() on submit would otherwise block the pass.
window.alert = () => {}; window.confirm = () => true;
const els = controlsOf(f);
function set(c) {
  if (!c) return null;
  if (c.kind === 'select') {
    const s = els.find(e => e.tagName === 'SELECT' && keyOf(e, els) === c.name);
    if (!s) return 'missing';
    setValue(s, c.value);
    Array.from(s.options).forEach(o => { o.selected = o.value === c.value; });
    ['input', 'change'].forEach(t => s.dispatchEvent(new Event(t, {bubbles: true})));
    if (window.jQuery) { try { window.jQuery(s).trigger('change'); } catch (e) {} }
    return s.value;
  }
  const r = els.find(e => e.type === 'radio' && e.name === c.name && e.value === c.value);
  if (!r) return 'missing';
  r.click();
  if (!r.checked) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); }
  const now = els.find(e => e.type === 'radio' && e.name === c.name && e.checked);
  return now ? now.value : '';
}
const state = {plan: set(plan)};
const a = amountName ? els.find(e => keyOf(e, els) === amountName) : null;
if (a) {
  const [min, max] = range || [null, null];
  let v = parseFloat((a.value || '').replace(/[^\d.]/g, ''));
  if (!(v > 0) || (min && v < min) || (max && v > max)) v = min || 100;
  setValue(a, String(v));
  ['input', 'change', 'keyup', 'blur'].forEach(t => a.dispatchEvent(new Event(t, {bubbles: true})));
  state.amount = a.value;
}
state.payment = set(payment);
return state;
"""

# Clicks the form's own submit button (or submits the form) once this script has returned, so
# the navigation does not cut the WebDriver call short. A deposit box without a form has no
# submit; its deposit-like button is clicked.
DEPOSIT_SUBMIT_JS = DEPOSIT_HELPERS_JS + r"""
const f = formAt(arguments[0]);
const els = controlsOf(f);
const b = els.find(e => (e.tagName === 'BUTTON' && (e.getAttribute('type') || 'submit').toLowerCase() === 'submit')
    || (e.tagName === 'INPUT' && ['submit', 'image'].includes(e.type)))
  || els.find(e => (e.tagName === 'BUTTON' || e.type === 'button') && BOX_BUTTON.test(clean(e.innerText || e.value)));
setTimeout(() => { if (b) b.click(); else if (f.requestSubmit) f.requestSubmit(); else if (f.submit) f.submit(); }, 0);
return b ? 'button ' + JSON.stringify((b.innerText || b.value || '').trim().slice(0, 30)) : 'form.submit()';
"""

# Whether the page after a submit is the deposit form again (a visible amount field), and any
# error or alert text it shows. The confirmation page also carries amount/type, but as hidden fields.
DEPOSIT_RESULT_JS = DEPOSIT_HELPERS_JS + r"""
const [amountName, index] = arguments;
let back = false;
if (amountName && index >= 0) {
  back = Array.from(document.forms).some(f => {
    const a = f.elements[amountName];
    return a && a.tagName === 'INPUT' && a.type !== 'hidden' && vis(a);
  });
} else if (amountName) {
  const box = depositBox();
  const els = box ? controlsOf(box) : [];
  const a = els.find(e => keyOf(e, els) === amountName);
  back = !!a && vis(a);
}
const messages = Array.from(document.querySelectorAll('.error, .alert, .alert-danger, .text-danger, .invalid-feedback, [role=alert]'))
  .filter(vis).map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(t => t && t.length < 200);
return {back, messages: Array.from(new Set(messages)).slice(0, 3)};
"""

# Links on a deposit page without a deposit form that lead to more deposit pages of the same
# site, one per method: opextrade.com's /dashboard/deposit lists 5 links such as
# /dashboard/deposit/create?method=btc, and each of those shows its address ("Send BTC to this
# address: bc1q...") before anything is submitted. History and list pages are left out.
DEPOSIT_SUBPAGE_LINKS_JS = r"""
const here = location.href.split('#')[0];
const seen = new Set();
return Array.from(document.querySelectorAll('a[href]')).map(a => a.href.split('#')[0]).filter(h => {
  let u; try { u = new URL(h); } catch (e) { return false; }
  if (u.host !== location.host || h === here || seen.has(h)) return false;
  if (!/deposit/i.test(u.pathname + u.search) || /histor|list|log|transaction/i.test(u.pathname + u.search)) return false;
  seen.add(h);
  return true;
});
"""

# Custom dropdowns: a payment choice built from <div>s instead of a <select>, which DEPOSIT_FORM_JS
# cannot see. lscominer.com's Recharge page (Oct 29) has no <form>: a "Select Deposit Network" box
# (div.select-selected) that opens a list of 10 <div value=...> networks (BTC, ETH, XRP, USDT Tron,
# ...), an amount input and a "Deposit" button that posts the order and loads the page with the
# wallet address. The control is found from the page's shape, not lscominer's class names: a visible
# amount input, and in the box around it a deposit-like button and a visible element reading
# "Select ..." / "Choose ..." (the innermost one). The options are the elements that become visible
# when it is clicked, taken as the largest group sharing one parent; that also covers menus that
# open elsewhere in the page. The elements found are kept in window.__crawler* between calls.
CUSTOM_DROPDOWN_HELPERS_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const press = e => ['mousedown', 'mouseup', 'click'].forEach(t => e.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
function dropdownParts() {
  const AMOUNT = /amount|sum|usd|quantity|money|value|invest/i;
  const BUTTON = /deposit|proceed|pay|continue|submit|fund|confirm|recharge|top ?up|invest/i;
  const TRIGGER = /^(select|choose|please select|pick)\b/i;
  const amounts = Array.from(document.querySelectorAll('input')).filter(e => vis(e)
      && ['number', 'text', 'tel', ''].includes((e.getAttribute('type') || '').toLowerCase())
      && AMOUNT.test([e.name, e.id, e.placeholder].join(' ')));
  for (const amount of amounts) {
    for (let box = amount.parentElement, i = 0; box && i < 6; box = box.parentElement, i++) {
      const button = Array.from(box.querySelectorAll('button, [role=button], input[type=button], input[type=submit]'))
          .find(b => vis(b) && BUTTON.test(clean(b.innerText || b.value)));
      if (!button) continue;
      const trigger = Array.from(box.querySelectorAll('div, span, button, a, p, label')).find(e => vis(e) && !e.closest('select')
          && TRIGGER.test(clean(e.innerText)) && clean(e.innerText).length < 60
          && !Array.from(e.children).some(c => TRIGGER.test(clean(c.innerText))));
      if (trigger) return {amount, button, trigger};
    }
  }
  return null;
}
"""
# Finds the control, notes what is visible, and clicks the "Select ..." element.
CUSTOM_DROPDOWN_OPEN_JS = CUSTOM_DROPDOWN_HELPERS_JS + r"""
const p = dropdownParts();
if (!p) return null;
window.__crawlerParts = p;
window.__crawlerBefore = new Set(Array.from(document.querySelectorAll('body *')).filter(vis));
press(p.trigger);
return {trigger: clean(p.trigger.innerText), amount: p.amount.name || p.amount.id || p.amount.placeholder || '',
        button: clean(p.button.innerText || p.button.value)};
"""
# The labels of the options the click revealed.
CUSTOM_DROPDOWN_OPTIONS_JS = CUSTOM_DROPDOWN_HELPERS_JS + r"""
const before = window.__crawlerBefore || new Set();
const groups = new Map();
Array.from(document.querySelectorAll('body *')).filter(e => vis(e) && !before.has(e)).forEach(e => {
  const t = clean(e.innerText);
  if (!t || t.length > 80) return;
  if (!groups.has(e.parentElement)) groups.set(e.parentElement, []);
  groups.get(e.parentElement).push(e);
});
let best = [];
groups.forEach(v => { if (v.length > best.length) best = v; });
window.__crawlerOptions = best;
return best.map(e => clean(e.innerText).slice(0, 60));
"""
# Clicks option arguments[0], sets the amount to arguments[1], and returns what the control shows.
CUSTOM_DROPDOWN_PICK_JS = CUSTOM_DROPDOWN_HELPERS_JS + r"""
const p = window.__crawlerParts, o = (window.__crawlerOptions || [])[arguments[0]];
if (!p || !o) return {error: 'the option is not on the page'};
window.alert = () => {}; window.confirm = () => true;
press(o);
Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(p.amount, String(arguments[1]));
['input', 'change', 'keyup', 'blur'].forEach(t => p.amount.dispatchEvent(new Event(t, {bubbles: true})));
return {shown: clean(p.trigger.innerText).slice(0, 60), amount: p.amount.value};
"""
# Clicks the deposit button once this script has returned (see DEPOSIT_SUBMIT_JS).
CUSTOM_DROPDOWN_SUBMIT_JS = r"""
const p = window.__crawlerParts;
if (p) setTimeout(() => p.button.click(), 0);
return !!p;
"""
# Visible input and textarea values: a wallet shown in a read-only input set by script is not in
# page_source.
INPUT_VALUES_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
return Array.from(document.querySelectorAll('input, textarea')).filter(e => vis(e) && e.type !== 'hidden' && e.type !== 'password')
  .map(e => e.value || '').join(' ');
"""
CUSTOM_DROPDOWN_AMOUNT = 100

# A plain <select> of coins that shows the wallet as soon as a coin is picked, with no amount field
# and no submit button, so DEPOSIT_FORM_JS finds no deposit form: coinprofx.live's /deposit is a
# form holding only <select name="walletId"> "Select cryptocurrency" (BTC - Bitcoin, ETH - ERC20,
# USDT - ERC20, SOL - SOLANA). The select's name, id, label or placeholder option must read like
# a coin/network choice; language and country pickers are left out. arguments[0] null returns the
# options; an index picks that option the way React and jQuery notice.
COIN_SELECT_JS = DEPOSIT_HELPERS_JS + r"""
const COIN = /asset|coin|crypto|currenc|wallet|network|method|payment|gateway/i;
const NOT = /lang|locale|country|translat|timezone|theme/i;
const PLACEHOLDER = /^(select|choose|please|--|-)/i;
function labelOf(s) {
  const l = (s.id && document.querySelector('label[for="' + CSS.escape(s.id) + '"]')) || s.closest('label');
  return clean([s.name, s.id, s.getAttribute('aria-label'), l ? l.innerText : '', s.options.length ? s.options[0].text : ''].join(' '));
}
const s = Array.from(document.querySelectorAll('select')).find(s => vis(s) && !s.disabled
    && !s.closest('nav, header, footer, [class*=navbar]') && COIN.test(labelOf(s)) && !NOT.test(labelOf(s) + ' ' + s.className)
    && Array.from(s.options).filter(o => !o.disabled && o.value !== '' && !PLACEHOLDER.test(clean(o.text))).length >= 1);
if (!s) return null;
const options = Array.from(s.options).filter(o => !o.disabled && o.value !== '' && !PLACEHOLDER.test(clean(o.text)));
if (arguments[0] === null) return {name: s.name || s.id || '', label: labelOf(s).slice(0, 80), options: options.map(o => clean(o.text).slice(0, 60))};
const o = options[arguments[0]];
if (!o) return {error: 'option ' + arguments[0] + ' is not in the list'};
setValue(s, o.value);
Array.from(s.options).forEach(x => { x.selected = x === o; });
['input', 'change'].forEach(t => s.dispatchEvent(new Event(t, {bubbles: true})));
if (window.jQuery) { try { window.jQuery(s).trigger('change'); } catch (e) {} }
return {picked: clean(o.text).slice(0, 60), value: s.value};
"""

# Deposit pages already run through the deposit step for the current site. aziminer.com's
# /deposits/create was run twice: once as a link from /deposits, once as a dashboard link.
deposit_pages_opened = set()

def process_deposit_page(driver, href, directory_name, original_window, depth=0):
    if href in deposit_pages_opened:
        logging.info(f"Deposit page {href}: already run for this site, skipped")
        return
    deposit_pages_opened.add(href)
    safe_href = windows_safe(href.replace('://', '_').replace('/', '_').replace('?', '_'))

    def capture(counter):
        """Save the current page as deposit_page_<href>_<counter>.html plus a full-page screenshot."""
        with open(os.path.join(directory_name, f"deposit_page_{safe_href}_{counter}.html"), 'w', encoding='utf-8') as file:
            file.write(driver.page_source)
        result = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
        with open(os.path.join(directory_name, f"deposit_screenshot_{safe_href}_{counter}.png"), 'wb') as file:
            file.write(base64.b64decode(result['data']))
        logging.info(f"Captured and saved data for deposit page {href} with counter: {counter}")

    def wait_for_page():
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time.sleep(3)
        pass_bot_wall(driver)

    def wallets_before_submit(counter, what):
        """Wallet-like strings the page shows once a payment option is chosen, before the submit
        (see CHOICE_WAIT); the page is saved as deposit_page_<href>_<counter>_chosen when there are any."""
        time.sleep(CHOICE_WAIT)
        found_now = wallet_like(driver.page_source + " " + (driver.execute_script(INPUT_VALUES_JS) or ""))
        if found_now:
            capture(f"{counter}_chosen")
            site_evidence.add_wallets(found_now, href)
            logging.info(f"Deposit pass {counter} on {href}: after choosing {what!r}, before submitting: wallet-like strings {found_now}")
        return set(found_now)

    def accept_alert(where):
        try:
            alert = driver.switch_to.alert
            logging.info(f"Deposit page {href}: accepted a dialog {where}: {alert.text[:150]!r}")
            alert.accept()
        except NoAlertPresentException:
            pass

    def custom_dropdown_passes():
        """One pass per option of a custom (non-<select>) payment dropdown; see CUSTOM_DROPDOWN_HELPERS_JS.
        False when the page has no such control, so the caller tries its other routes."""
        opened = driver.execute_script(CUSTOM_DROPDOWN_OPEN_JS)
        if not opened:
            return False
        time.sleep(1)  # menus slide open
        labels = (driver.execute_script(CUSTOM_DROPDOWN_OPTIONS_JS) or [])[:MAX_DEPOSIT_PASSES]
        if not labels:
            logging.info(f"Deposit page {href}: clicking {opened['trigger']!r} showed no options")
            return False
        logging.info(f"Deposit page {href}: no deposit form, but a custom dropdown {opened['trigger']!r} with {len(labels)} "
                     f"options {labels}; amount field {opened['amount']!r}, button {opened['button']!r}; {len(labels)} passes planned")
        wallets, passes = set(), 0
        for counter, label in enumerate(labels, start=1):
            try:
                index = counter - 1
                if counter > 1:
                    driver.get(href)
                    wait_for_page()
                    if looks_logged_out(driver):
                        if not relogin_here(driver):
                            logging.info(f"Deposit page {href}: the session is lost, stopping the passes")
                            break
                        driver.get(href)
                        wait_for_page()
                    if not driver.execute_script(CUSTOM_DROPDOWN_OPEN_JS):
                        logging.info(f"Deposit pass {counter} on {href}: the dropdown is not on the reloaded page, stopping")
                        break
                    time.sleep(1)
                    now = driver.execute_script(CUSTOM_DROPDOWN_OPTIONS_JS) or []
                    if label not in now:
                        logging.info(f"Deposit pass {counter} on {href}: option {label!r} is not in the reopened list, skipped")
                        continue
                    index = now.index(label)
                state = driver.execute_script(CUSTOM_DROPDOWN_PICK_JS, index, CUSTOM_DROPDOWN_AMOUNT)
                if state.get('error'):
                    logging.info(f"Deposit pass {counter} on {href}: {state['error']}, stopping")
                    break
                wallets.update(wallets_before_submit(counter, label))
                driver.execute_script(CUSTOM_DROPDOWN_SUBMIT_JS)
                time.sleep(1)
                accept_alert("after submitting")
                wait_for_page()
                capture(counter)
                passes += 1
                found_wallets = wallet_like(driver.page_source + " " + (driver.execute_script(INPUT_VALUES_JS) or ""))
                wallets.update(found_wallets)
                site_evidence.add_wallets(found_wallets, href)
                logging.info(f"Deposit pass {counter} on {href}: option {label!r}; set {state}; "
                             f"result title {driver.title[:60]!r}, url {driver.current_url}, wallet-like strings {found_wallets or 'none'}")
            except UnexpectedAlertPresentException:
                accept_alert(f"during pass {counter}")
        logging.info(f"Deposit page {href}: {passes} passes run, {len(wallets)} distinct wallet-like strings")
        return True

    def coin_select_passes():
        """One pass per option of a coin <select> that has no amount field or submit button; see
        COIN_SELECT_JS. Nothing is submitted: the page is read after each pick. False when the
        page has no such select."""
        found = driver.execute_script(COIN_SELECT_JS, None)
        if not found or not found.get('options'):
            return False
        labels = found['options'][:MAX_DEPOSIT_PASSES]
        logging.info(f"Deposit page {href}: no deposit form, but a coin dropdown {found['name']!r} ({found['label']!r}) "
                     f"with {len(labels)} options {labels}; one pick per option, nothing submitted")
        wallets = set()
        for counter, label in enumerate(labels, start=1):
            try:
                if counter > 1:
                    driver.get(href)
                    wait_for_page()
                    if looks_logged_out(driver):
                        if not relogin_here(driver):
                            logging.info(f"Deposit page {href}: the session is lost, stopping the picks")
                            break
                        driver.get(href)
                        wait_for_page()
                picked = driver.execute_script(COIN_SELECT_JS, counter - 1)
                if not picked or picked.get('error'):
                    logging.info(f"Deposit pick {counter} on {href}: {(picked or {}).get('error', 'the dropdown is gone')}, stopping")
                    break
                time.sleep(2)  # the page fetches or reveals the address
                capture(counter)
                found_wallets = wallet_like(driver.page_source + " " + (driver.execute_script(INPUT_VALUES_JS) or ""))
                wallets.update(found_wallets)
                site_evidence.add_wallets(found_wallets, href)
                logging.info(f"Deposit pick {counter} on {href}: option {label!r} (picked {picked.get('picked')!r}); "
                             f"url {driver.current_url}, wallet-like strings {found_wallets or 'none'}")
            except UnexpectedAlertPresentException:
                accept_alert(f"during pick {counter}")
        logging.info(f"Deposit page {href}: {len(labels)} coin picks, {len(wallets)} distinct wallet-like strings")
        return True

    driver.execute_script("window.open(arguments[0]);", href)
    driver.switch_to.window(driver.window_handles[-1])
    prepare_tab(driver)
    try:
        wait_for_page()
        dismiss_blocking_popups(driver)
        # The page as the site serves it, before anything is filled in. Some sites show their
        # wallet addresses here already, and it shows what the form offered if the passes fail.
        capture(0)
        shown = wallet_like(driver.page_source)
        if shown:
            logging.info(f"Deposit page {href}: wallet-like strings on the page as served: {shown}")
        try:
            site_evidence.add_page(driver.page_source, driver.current_url, 'deposit', count=False)
        except Exception as e:
            logging.info(f"HYIP check: could not read deposit page {href}: {str(e).splitlines()[0] if str(e) else e!r}")
        site_evidence.add_wallets(shown, href)
        found = driver.execute_script(DEPOSIT_FORM_JS)
        form, plan, payment = found.get('form'), found.get('plan'), found.get('payment')
        if form:
            site_evidence.add_deposit(href, plan, payment)
        if not form:
            if custom_dropdown_passes() or coin_select_passes():
                return
            logging.info(f"Deposit page {href}: no deposit form ({found.get('forms', 0)} form(s) on the page, "
                         f"none with an amount field or a choice control and a submit button); 0 passes planned")
            # One level down only, so a method page that links back to the list is not reopened.
            subpages = driver.execute_script(DEPOSIT_SUBPAGE_LINKS_JS) if depth == 0 else []
            if subpages:
                logging.info(f"Deposit page {href}: opening {min(len(subpages), MAX_DEPOSIT_PASSES)} linked deposit "
                             f"page(s) as deposit pages: {subpages[:MAX_DEPOSIT_PASSES]}")
                this_tab = driver.current_window_handle
                for subpage in subpages[:MAX_DEPOSIT_PASSES]:
                    process_deposit_page(driver, subpage, directory_name, this_tab, depth=1)
            return

        plan_choice = plan['options'][0] if plan else None
        # A form hidden in a dialog gets one pass per button that opens it (see OPENERS_JS); a
        # payment dropdown inside the dialog then keeps its first option.
        openers = found.get('openers') or []
        fixed_payment = payment['options'][0] if openers and payment and payment['kind'] != 'button' else None
        options = openers or (payment['options'] if payment else [None])
        if len(options) > MAX_DEPOSIT_PASSES:
            logging.info(f"Deposit page {href}: {len(options)} payment options, only the first {MAX_DEPOSIT_PASSES} are tried")
            options = options[:MAX_DEPOSIT_PASSES]
        chosen = {c['name'] for c in (plan, payment) if c}
        others = [f"{c['kind']} {c['name']!r}" for c in form['controls'] if c['name'] not in chosen]
        logging.info(
            f"Deposit page {href}: form #{form['index']} {form['name']!r}; "
            + (f"plan {plan['kind']} {plan['name']!r} ({len(plan['options'])} options), using {plan_choice['label']!r}"
               + (f" range {plan_choice['range']}" if plan_choice.get('range') else "") if plan else "no plan control")
            + "; " + (f"the form is hidden in a dialog opened by {len(openers)} button(s) {[o['label'] for o in openers]}, one pass each"
                      + (f" with payment {payment['name']!r} = {fixed_payment['label']!r}" if fixed_payment else "") if openers else
                      f"payment {payment['kind']} {payment['name']!r} with {len(payment['options'])} options "
                      f"{[o['label'] for o in payment['options']]}" if payment else "no payment-method control, one pass with the site's default")
            + f"; amount field {form['amount']!r}"
            + (f"; left as they are: {others}" if others else "")
            + f"; {len(options)} passes planned")

        wallets, form_back_in_a_row, passes = set(), 0, 0
        for counter, option in enumerate(options, start=1):
            try:
                if counter > 1:
                    driver.get(href)
                    wait_for_page()
                    # The reloaded deposit page can be the login page (fxtastytrades.com after pass 5);
                    # submitting it as a deposit form clicked its "Access Dashboard" login button.
                    if looks_logged_out(driver):
                        if not relogin_here(driver):
                            logging.info(f"Deposit page {href}: the session is lost, stopping the passes")
                            break
                        driver.get(href)
                        wait_for_page()
                by_button = not openers and bool(payment) and payment['kind'] == 'button'
                if openers:
                    opened = driver.execute_script(OPENER_CLICK_JS, form['index'], option['value'])
                    if opened.get('error'):
                        logging.info(f"Deposit pass {counter} on {href}: {opened['error']}, stopping")
                        break
                    try:
                        WebDriverWait(driver, 6).until(lambda d: d.execute_script(FORM_SHOWN_JS, form['index']))
                    except TimeoutException:
                        logging.info(f"Deposit pass {counter} on {href}: the {option['label']!r} button did not open the "
                                     f"deposit form; skipping it")
                        continue
                if by_button:
                    clicked = driver.execute_script(METHOD_CLICK_JS, form['index'], option['value'])
                    if clicked.get('error'):
                        logging.info(f"Deposit pass {counter} on {href}: {clicked['error']}, stopping")
                        break
                    try:
                        WebDriverWait(driver, 8).until(lambda d: d.execute_script(FIELD_VALUE_JS, form['index'], payment['name']))
                    except TimeoutException:
                        logging.info(f"Deposit pass {counter} on {href}: the {option['label']!r} button did not fill "
                                     f"{payment['name']!r}; skipping this method")
                        continue
                pay = (fixed_payment if openers else None if by_button else option)
                state = driver.execute_script(
                    DEPOSIT_FILL_JS, form['index'],
                    {'kind': plan['kind'], 'name': plan['name'], 'value': plan_choice['value']} if plan else None,
                    {'kind': payment['kind'], 'name': payment['name'], 'value': pay['value']} if pay and payment else None,
                    form['amount'], (plan_choice or {}).get('range') or (option or {}).get('range'))
                if state.get('error'):
                    logging.info(f"Deposit pass {counter} on {href}: {state['error']}, stopping")
                    break
                if by_button:
                    state['payment'] = driver.execute_script(FIELD_VALUE_JS, form['index'], payment['name'])
                before = wallets_before_submit(counter, option['label'] if option else 'site default')
                wallets.update(before)
                how = driver.execute_script(DEPOSIT_SUBMIT_JS, form['index'])
                time.sleep(1)
                accept_alert("after submitting")
                wait_for_page()
                capture(counter)
                passes += 1
                found_wallets = wallet_like(driver.page_source)
                wallets.update(found_wallets)
                site_evidence.add_wallets(found_wallets, href)
                result = driver.execute_script(DEPOSIT_RESULT_JS, form['amount'], form['index'])
                logging.info(
                    f"Deposit pass {counter} on {href}: payment {option['label'] if option else 'site default'!r}"
                    f"{' (' + option['value'] + ')' if option else ''}; submitted with {state} via {how}; "
                    f"result title {driver.title[:60]!r}, url {driver.current_url}, wallet-like strings {found_wallets or 'none'}"
                    + (f"; the deposit form came back{': ' + ' | '.join(result['messages']) if result['messages'] else ' with no message'}"
                       if result['back'] else ""))
                form_back_in_a_row = form_back_in_a_row + 1 if result['back'] and not found_wallets and not before else 0
                if form_back_in_a_row >= 2:
                    logging.info(f"Deposit page {href}: the form came back on two passes in a row, stopping")
                    break
            except UnexpectedAlertPresentException:
                accept_alert(f"during pass {counter}")
        logging.info(f"Deposit page {href}: {passes} passes run, {len(wallets)} distinct wallet-like strings")

    except UnexpectedAlertPresentException:
        accept_alert("while reading the page")
    except Exception as e:
        logging.error(f"Failed to process deposit page {href}: {e}")
    finally:
        driver.close()
        driver.switch_to.window(original_window)

# Sign-up form with timeout
def fill_sign_up_form_with_timeout(driver, url, timeout_seconds=300):
    global should_continue, form_filled, logged_in, email_used, password_used, relogins_done, session_lost, site_evidence
    should_continue = True
    form_filled = False
    logged_in = False
    email_used = None  # else a previous site's address leaked into this site's result
    password_used = ACCOUNT_PASSWORD
    relogins_done, session_lost = 0, False
    deposit_pages_opened.clear()
    site_evidence = SiteEvidence(url)

    def target():
        global form_filled
        form_filled = fill_sign_up_form(driver, url)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        logging.warning(f"Timeout exceeded for {url}")
        should_continue = False
        thread.join()
        # The account's address, not False: digital4xtrade.com ran past the limit after logging in
        # and the result line read "Success: ... (logged in as False)".
        return email_used
    return email_used if form_filled else None

# Find login URL with a single password field
# The sign-up page itself is never taken as the login page: /auth?mode=signup matched the
# 'auth' login keyword and has one password field, so "login" was tried on the sign-up form
# (swifttradinginc.live). Its links are still followed.
SIGNUP_URL_KEYWORDS = ('joinus', 'register', 'signup', 'sign-up', 'createaccount', 'newuser', 'registration')
# The search used to follow every link, off-site ones included, with no limit; on
# cryptocoinprop.com it walked the site for 2 minutes. Links that look like login (by URL or by
# link text) are now tried first, only the site's own pages are followed, and it stops after
# this many pages.
MAX_LOGIN_SEARCH_PAGES = 25

# Returns (login URL, None), or (None, URL of a page with a Logout link) when a page shows the
# account is already logged in, or (None, None). Each page visited is logged with where it ended
# up, so a failed search shows what it saw.
# landed_url, the page the sign-up submit ended on, is checked first: minnercoinex.org sends the
# new account to signin.php, but the search began at signup.php, whose only links are the terms
# and privacy pages, and gave up after 3 pages.
def find_login_url_with_single_password_field(driver, start_url, signup_url=None, landed_url=None):
    base_domain = get_base_domain(start_url)
    visited_urls = set()
    urls_to_check = [start_url]
    if landed_url and landed_url not in (start_url, signup_url) and get_base_domain(landed_url) == base_domain:
        urls_to_check.insert(0, landed_url)
    login_url = None

    while urls_to_check and len(visited_urls) < MAX_LOGIN_SEARCH_PAGES:
        current_url = urls_to_check.pop(0)
        if current_url in visited_urls or is_blacklisted(current_url):
            continue
        visited_urls.add(current_url)
        try:
            driver.get(current_url)
            pass_bot_wall(driver)
            is_signup_page = current_url in (start_url, signup_url) or any(k in current_url.lower() for k in SIGNUP_URL_KEYWORDS)
            login_url = None if is_signup_page else has_single_password_field(driver, current_url)
            state = page_state(driver)
            logging.info(f"Login search: {current_url}"
                         + (f" -> {state['url']}" if state['url'] and state['url'] != current_url else "")
                         + f"; {state['passwords']} visible password field(s)"
                         + ("; Logout link" if state['logout'] else "") + ("; sign-up page, skipped" if is_signup_page else ""))
            # A Logout link means logged in whatever password fields the page has: coinxstake.com's
            # /profile holds a change-password field next to its Logout link, was taken for the login
            # page, and the credentials went into the change-password form (Oct 30).
            if state['logout']:
                return None, state['url']
            if login_url:
                logging.info(f"URL with a single password field in a form: {login_url}")
                return login_url, None
            # A login page that sends the browser on to an account page means the session is already
            # logged in, even with no Logout link to see: ultrabitfx.com's /login went to
            # /user/dashboard after the e-mail confirmation, and its dashboard has no logout element.
            # The site's front page does not count: some sites send visitors there from /login.
            landed_path = urlparse(state['url']).path.strip('/').lower()
            if (LOGIN_URL_RE.search(current_url) and state['url'] and state['url'] != current_url and not state['passwords']
                    and landed_path not in ('', 'index', 'index.html', 'index.php', 'home') and same_site(state['url'], base_domain)):
                logging.info(f"Login search: {current_url} sends a logged-in browser on to {state['url']}")
                return None, state['url']
            login_like = []
            # Hrefs and link texts in one script call, so a page that changes mid-loop cannot leave
            # stale link elements (see the sign-up search).
            links = driver.execute_script("return Array.from(document.querySelectorAll('a'))"
                                          ".map(a => [a.getAttribute('href') ? a.href : '', (a.innerText || '').trim()]);") or []
            for href, link_text in links:
                if not href or href.startswith(('javascript:', 'mailto:', 'tel:')):
                    continue
                absolute_link = urljoin(current_url, href)
                if (not same_site(absolute_link, base_domain) or absolute_link in visited_urls or LANGUAGE_LINK_RE.search(absolute_link)
                        or SESSION_END_RE.search(absolute_link)
                        or absolute_link in urls_to_check or is_blacklisted(absolute_link)):
                    continue
                if LOGIN_URL_RE.search(absolute_link) or LOGIN_TEXT_RE.match(link_text):
                    login_like.append(absolute_link)
                else:
                    urls_to_check.append(absolute_link)
            urls_to_check[:0] = login_like
        except Exception as e:
            logging.error(f"An error occurred while crawling {current_url}: {str(e)}")
    if urls_to_check:
        logging.info(f"No page with a single password field found in the first {MAX_LOGIN_SEARCH_PAGES} pages.")
    else:
        logging.info("No page with a single password field found.")
    return None, None

# Save and retrieve last processed URL
def save_last_processed_url(file_path, last_url):
    with open(file_path, 'w') as file:
        file.write(last_url)

def get_last_processed_url(file_path):
    try:
        with open(file_path, 'r') as file:
            return file.read().strip()
    except FileNotFoundError:
        return None

# Check Mailinator inbox and click links. Mailinator is used as an email service to click on email confirmation links after signup
#
# Polls the one inbox the sign-up used (the local part of email_address) for up
# to MAILINATOR_WAIT_SECONDS and opens the links in any message that arrived
# after signup_time. Shapes verified against the v2 API on Oct 6 2026:
#   GET {MAILINATOR_API}/inboxes/{inbox}                      -> {"msgs": [{"id", "time" (epoch ms), "subject", ...}]}
#   GET {MAILINATOR_API}/inboxes/{inbox}/messages/{id}/links  -> {"links": [...]}
#   GET {MAILINATOR_API}/inboxes/{inbox}/messages/{id}        -> {"parts": [{"body": ...}], ...}
# The old version listed "inboxes" (the API returns "msgs"), read "time" as
# seconds and "data.parts" for the body, so it could never click anything.
MAILINATOR_CONFIRM_HINTS = ('verif', 'confirm', 'activat', 'token', 'valid', 'register', 'signup', 'sign-up')
MAILINATOR_MAX_CLICKS = 3
MAILINATOR_EXTRA_WAIT = 20  # seconds to keep polling after clicking links that were not confirmation links
# Links that are not pages to open: images and the like (xcelholdingsltd.org's header.png),
# and, off the site's own domain, anything without a confirmation word in it
# (swantradecapital.com's http://www.w3.org/2000/svg). Off-site confirmation links stay:
# swifttradinginc.live confirms through supabase.co/auth/v1/verify?token=...
ASSET_LINK_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js)$", re.I)
OFFSITE_CONFIRM_HINTS = ('verif', 'confirm', 'activat', 'token')
# A 4-8 digit code shortly after the word code/OTP/PIN, as in swantradecapital.com's
# "please verify your email address by entering the verification code below: Your
# Verification Code 667415".
EMAIL_CODE_RE = re.compile(r"\b(?:code|otp|pin)\b[^0-9]{0,80}?\b(\d{4,8})\b", re.I)

def same_site(url, site_domain):
    d = get_base_domain(url)
    return d == site_domain or d.endswith('.' + site_domain) or site_domain.endswith('.' + d)

def _mailinator_links(inbox, msg_id, params, site_domain=None):
    base = f"{MAILINATOR_API}/inboxes/{inbox}/messages/{msg_id}"
    resp = curl_requests.get(f"{base}/links", params=params, timeout=15)
    resp.raise_for_status()
    links = resp.json().get('links', [])
    if not links:  # fall back to the hrefs in the message body
        resp = curl_requests.get(base, params=params, timeout=15)
        resp.raise_for_status()
        for part in resp.json().get('parts', []):
            links += [html.unescape(l) for l in re.findall(r'https?://[^\s"\'<>]+', part.get('body', ''))]
    # Confirmation-looking links first, unsubscribe links never, each link once.
    links = [l for l in dict.fromkeys(links) if 'unsubscribe' not in l.lower() and not ASSET_LINK_RE.search(urlparse(l).path)]
    if site_domain:
        links = [l for l in links if same_site(l, site_domain) or any(h in l.lower() for h in OFFSITE_CONFIRM_HINTS)]
    return sorted(links, key=lambda l: not any(h in l.lower() for h in MAILINATOR_CONFIRM_HINTS))

def _mailinator_code(inbox, msg_id, params):
    """The verification code in a message's text, or None."""
    resp = curl_requests.get(f"{MAILINATOR_API}/inboxes/{inbox}/messages/{msg_id}", params=params, timeout=15)
    resp.raise_for_status()
    for part in resp.json().get('parts', []):
        text = html.unescape(re.sub(r"<(style|script)\b.*?</\1>|<[^>]+>", " ", part.get('body', ''), flags=re.S | re.I))
        match = EMAIL_CODE_RE.search(re.sub(r"\s+", " ", text))
        if match:
            return match.group(1)
    return None

# Returns (whether a link was clicked, the verification code found or None). With want_code
# (the page after sign-up asks for a code) it keeps polling until a code turns up, not just
# until the first link is clicked: swantradecapital.com sends a welcome e-mail with a link
# and a separate e-mail with the code.
def check_mailinator_inbox_and_click_links(api_key, driver, email_address, signup_time,
                                           wait_seconds=MAILINATOR_WAIT_SECONDS, site_url=None, want_code=False):
    if not api_key or api_key == "YOUR_API_KEY":
        logging.info("Mailinator API key not configured; skipping email-confirmation step.")
        return False, None
    if not email_address or not email_address.endswith(f"@{EMAIL_DOMAIN}"):
        logging.info(f"Sign-up email {email_address!r} is not on {EMAIL_DOMAIN}; skipping email-confirmation step.")
        return False, None
    inbox = email_address.split('@')[0]
    params = {"token": api_key}
    site_domain = get_base_domain(site_url) if site_url else None
    since_ms = (signup_time - 30) * 1000  # small allowance for clock skew
    seen, clicked, code = set(), 0, None
    confirm_clicked, first_click_at, extra_wait_logged = False, None, False
    deadline = time.time() + wait_seconds

    # Without a code to wait for, stop once a confirmation-looking link was opened, or
    # MAILINATOR_EXTRA_WAIT seconds after the first click. duduinvest.com sent "Welcome" (home and
    # login links) and "Confirm your email" (supabase.co/auth/v1/verify) in the same second; the
    # first poll only had the welcome mail, the loop stopped after clicking it, and the login then
    # failed with "Email not confirmed".
    def done():
        if want_code:
            return bool(code)
        return confirm_clicked or (clicked and time.time() - first_click_at >= MAILINATOR_EXTRA_WAIT)

    while time.time() < deadline and should_continue and not done():
        try:
            resp = curl_requests.get(f"{MAILINATOR_API}/inboxes/{inbox}",
                                params={**params, "limit": 10, "sort": "descending"}, timeout=15)
            resp.raise_for_status()
            for msg in resp.json().get('msgs', []):
                if msg['id'] in seen or msg.get('time', 0) < since_ms:
                    continue
                seen.add(msg['id'])
                logging.info(f"Mailinator: '{msg.get('subject', '')}' arrived for {email_address}")
                if want_code and not code:
                    code = _mailinator_code(inbox, msg['id'], params)
                    if code:
                        logging.info(f"Mailinator: verification code {code} in '{msg.get('subject', '')}'")
                for link in _mailinator_links(inbox, msg['id'], params, site_domain)[:MAILINATOR_MAX_CLICKS]:
                    driver.get(link)
                    clicked += 1
                    first_click_at = first_click_at or time.time()
                    confirm_clicked = confirm_clicked or any(h in link.lower() for h in OFFSITE_CONFIRM_HINTS)
                    logging.info(f"Clicked link in email: {link}")
                if clicked and not confirm_clicked and not want_code and not extra_wait_logged:
                    extra_wait_logged = True
                    logging.info(f"No confirmation link among the clicked e-mail links; checking the inbox up to {MAILINATOR_EXTRA_WAIT} s more")
        except (curl_requests.exceptions.RequestException, ValueError, KeyError) as e:
            logging.error(f"Error checking Mailinator inbox {inbox}: {e}")
        except WebDriverException as e:
            logging.error(f"Error opening a link from the Mailinator inbox {inbox}: {e}")
        if not done():
            time.sleep(5)
    return clicked > 0, code

# --- Verification codes ------------------------------------------------------------------------
# swantradecapital.com sends a 6-digit code by e-mail and redirects every page to /verify-code
# until it is entered. A code field is: one box per digit (4 or more inputs with maxlength 1),
# or an input named/labelled like a code (code, otp, token, pin, verif, or autocomplete
# "one-time-code"), or the only text input on a page that mentions a code.
CODE_INPUTS_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const inputs = Array.from(document.querySelectorAll('input')).filter(e =>
    vis(e) && !e.disabled && !e.readOnly && ['text', 'number', 'tel'].includes(e.type));
const boxes = inputs.filter(e => e.maxLength === 1);
if (boxes.length >= 4) return boxes;
// Referral-type fields are not verification codes: beacondigital.ltd's optional "referralCode" on
// the sign-up form made the crawler wait 60 s for an e-mailed code that never came.
const NOT_A_CODE = /refer|ref_?code|promo|invite|invitation|sponsor|coupon|voucher|bonus|upline|zip|postal/i;
const hinted = inputs.filter(e => !NOT_A_CODE.test([e.name, e.id, e.placeholder].join(' ')) && (e.autocomplete === 'one-time-code' ||
    /code|otp|token|pin|verif/i.test([e.name, e.id, e.placeholder, e.getAttribute('aria-label')].join(' '))));
if (hinted.length) return hinted.slice(0, 1);
const plain = inputs.filter(e => !NOT_A_CODE.test([e.name, e.id, e.placeholder].join(' ')));
if (inputs.length === 1 && plain.length === 1 && /\bcode\b/i.test(document.body ? document.body.innerText : '')) return plain;
return [];
"""

def enter_email_code(driver, code, code_page_url):
    """Type the e-mailed code into the code field and submit it. Returns (page state after the
    submit, URL of the code page), or (None, None) when no code field is found."""
    fields = driver.execute_script(CODE_INPUTS_JS)
    if not fields and code_page_url and driver.current_url != code_page_url:
        driver.get(code_page_url)  # a clicked e-mail link may have left the code page
        time.sleep(2)
        fields = driver.execute_script(CODE_INPUTS_JS)
    if not fields:
        logging.info(f"E-mail code {code} found, but no code field on {driver.current_url}")
        return None, None
    code_url = driver.current_url
    before = page_state(driver)
    if len(fields) > 1:
        for field, digit in zip(fields, code):
            field.send_keys(digit)
    else:
        fields[0].clear()
        fields[0].send_keys(code)
    time.sleep(1)
    form = driver.execute_script("return arguments[0].form;", fields[0])
    if form is None or not submit_form(driver, [], form):
        fields[-1].send_keys(Keys.RETURN)
    after = wait_for_outcome(driver, before)
    logging.info(f"Entered the e-mail code {code} on {code_url}: now at {after['url']}; "
                 f"{after['passwords']} visible password field(s)" + ("; Logout link" if after['logout'] else "")
                 + (f"; messages: {' | '.join(new_errors(before, after))[:200]}" if new_errors(before, after) else ""))
    return after, code_url

# Main function
def main():
    global driver, current_site_url, use_xvfb, classify_only, single_site
    # --url crawls one site without reading urls.txt or touching the crawled_urls.txt
    # checkpoint, for testing a single site; --show-browser shows the Chrome window on a
    # Linux desktop instead of drawing it on Xvfb (on Windows the window is always shown).
    parser = argparse.ArgumentParser(description="Crimson authenticated crawler")
    parser.add_argument('--url', help="crawl only this site instead of urls.txt")
    parser.add_argument('--show-browser', action='store_true', help="on Linux, use the real display instead of Xvfb")
    parser.add_argument('--classify-only', action='store_true',
                        help="read the start page and the sign-up search's pages for the HYIP verdict; never sign up")
    args = parser.parse_args()
    if args.show_browser:
        use_xvfb = False
    classify_only, single_site = args.classify_only, bool(args.url)
    if args.url:  # also print the log to the terminal
        console = logging.StreamHandler(sys.__stdout__)
        console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(console)

    start_virtual_display()
    driver = initialize_driver()
    handle_alert(driver)  # Handle initial alerts right after the driver is initialized
    try:
        if args.url:
            current_site_url = args.url
            log_site_result(args.url, fill_sign_up_form_with_timeout(driver, args.url))
            return
        list_of_urls_file = URLS_FILE #List of URLs (produced by feeder.py from pipeline results)
        last_processed_file_path = CRAWLED_URLS_FILE #Contains the last crawled URL to keep track
        # Login happens per site inside fill_sign_up_form(). A second login search
        # used to run here from start_url, which is always '', so it only logged
        # an "invalid argument" error.
        fill_sign_up_urls_in_list(list_of_urls_file, last_processed_file_path)
    except Exception as e:
        logging.error(f"An error occurred in the main loop: {e}")
        driver = ensure_driver(driver)  # Ensures the driver is responsive and handles re-initialization if needed
    finally:
        logging.info("Quitting Driver")
        driver.quit()
        stop_virtual_display()

if __name__ == "__main__":
    main()