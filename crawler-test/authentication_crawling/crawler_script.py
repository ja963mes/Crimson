
import argparse
import base64
import html
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
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    ElementNotInteractableException, NoSuchElementException,
    TimeoutException, WebDriverException, NoAlertPresentException,
    UnexpectedAlertPresentException, StaleElementReferenceException
)


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
EMAIL_DOMAIN = os.environ.get('MAILINATOR_DOMAIN', 'your-email-domain.com')
MAILINATOR_API_KEY = os.environ.get('MAILINATOR_API_KEY', 'YOUR_API_KEY')
# "private" addresses the account's private domain (EMAIL_DOMAIN) in the v2 API.
MAILINATOR_API = 'https://api.mailinator.com/api/v2/domains/private'
MAILINATOR_WAIT_SECONDS = 60   # how long to poll the sign-up inbox for a confirmation email
# The password every sign-up form gets, and therefore the one login must use.
# Login used to send the literal string "PASSWORD", so no login could succeed.
# 'Pass@1234' was refused as too weak by sign-up checks against breached-password
# lists (swifttradinginc.live, Oct 24); this one is long and not a common password.
ACCOUNT_PASSWORD = 'Vq7#Lm2pXr9!Tz'
# Chromium/chromedriver locations. Defaults match the Crimson worker nodes
# (snap Chromium shim + system chromedriver), the same paths recv.py uses.
# Override via env for other environments.
CHROMEDRIVER_PATH = os.environ.get('CHROMEDRIVER_PATH', '/usr/bin/chromedriver')
CHROME_BINARY = os.environ.get('CHROME_BIN', '/usr/bin/chromium-browser')
# --------------------------------------------------------------------------

# Configure logging
logging.basicConfig(filename=log_file_path, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
sys.stdout = open(log_file_path, 'a', buffering=1)
sys.stderr = open(log_file_path, 'a', buffering=1)
logging.info("Script started.")

def setup_chrome_options(headless=True):
    chrome_options = webdriver.ChromeOptions()
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-translate")
    chrome_options.add_experimental_option("prefs", {
        "translate_whitelists": {},
        "translate": {"enabled": False},
        "intl.accept_languages": "en-US,en"
    })
    # Off the VMs (e.g. a local Windows test) these paths don't exist; Selenium then
    # uses the installed Chrome and fetches a matching chromedriver itself.
    if os.path.exists(CHROME_BINARY):
        chrome_options.binary_location = CHROME_BINARY
    return chrome_options


chrome_options = setup_chrome_options()

# Handle pop ups and alerts
def handle_alert(driver):
    try:
        # Inject JavaScript to override alert, confirm, and prompt functions
        driver.execute_script("""
            window.alert = function(message) {
                console.log("Alert called with message: " + message);
            };
            window.confirm = function(message) {
                console.log("Confirm called with message: " + message);
                return true;  // Return true to simulate clicking "OK"
            };
            window.prompt = function(message, defaultText) {
                console.log("Prompt called with message: " + message + ", default text: " + defaultText);
                return defaultText;  // Return the default text to simulate entering text and clicking "OK"
            };
        """)
        logging.info("Alert functions overridden to log messages to the console.")
    except Exception as e:
        logging.error(f"Failed to override alert functions: {e}")

# Initialize driver function
#
# Each browser gets its own chromedriver Service. A single module-level Service
# used to be shared by every webdriver.Chrome() call, and a browser was also
# opened at import time and never quit: every run leaked a chromedriver plus
# Chrome, and later starts reused a Service that was already running.
def initialize_driver():
    service = Service(executable_path=CHROMEDRIVER_PATH if os.path.exists(CHROMEDRIVER_PATH) else None)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    handle_alert(driver)  # Handle alerts immediately after driver initialization
    return driver

driver = None  # created in main(); fill_form() and the URL loop use this global
# The urls.txt line being crawled. login_and_take_screenshot() writes it to
# source_url.txt in the site's folder, because the folder is named after the login
# URL, which can be on another host than the site feeder.py gave the crawler.
# build_wallets.py reads it to tie each wallet to its domain.
current_site_url = None
# Set by login_and_take_screenshot() when the login is not judged failed, so the
# per-site "Success" line means logged in, not merely that a form was filled.
logged_in = False

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
        else:
            predictions[key] = generate_random_string()
    return predictions

# Passwords. ACCOUNT_PASSWORD has symbols and is 14 characters long. Sites that allow only
# letters and digits, 8-12 characters, get ACCOUNT_PASSWORD_ALT: tradevistavelocityinvestmentmark.com
# checks /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z\d]{8,12}$/ in its own script and keeps "Create
# Account" disabled until it passes. The alternative is used when a password field's maxlength or
# pattern refuses ACCOUNT_PASSWORD, or (see fill_sign_up_form) when the submit button stays disabled.
# password_used is the one the account got, for the login.
ACCOUNT_PASSWORD_ALT = 'Vq7Lm2pXr9Tz'
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
return {url: location.href, errors: [...new Set(errors)].slice(0, 8), invalid: invalid.slice(0, 5), logout: logout,
        passwords: Array.from(document.querySelectorAll('input[type=password]')).filter(vis).length,
        text: (document.body ? document.body.innerText : '').slice(0, 20000)};
"""
SIGNUP_OK_RE = re.compile(r"registration (was |is )?(complete|completed|successful)|successfully (registered|signed up|created)|registered successfully|"
                          r"account (has been |was )?(created|registered)|thank you for (registering|signing up|joining|your registration)|"
                          r"check your (e-?mail|inbox)|(confirmation|verification|activation) (link|e-?mail|code) (has been |was )?sent|verify your e-?mail", re.I)
FORM_ERROR_RE = re.compile(r"already (exists|registered|in use|taken)|required|invalid|incorrect|do(es)? not match|must (be|contain|have)|"
                           r"please (enter|fill|provide|select|check|agree|type|correct|choose)|captcha|not valid|too (short|long)|wrong|error|failed", re.I)
LOGIN_FAILED_URL_RE = re.compile(r"say=invalid|invalid_login|login_?error|[?&]error=", re.I)
# The page after a sign-up asks for the e-mail address to be confirmed (text or URL).
VERIFY_EMAIL_RE = re.compile(r"verify[-_ ]?(your )?e-?mail|confirm (your )?e-?mail|check your (e-?mail|inbox)|e-?mail[-_ ]?verification|"
                             r"(confirmation|verification|activation) (link|e-?mail|code) (has been |was )?sent", re.I)

def page_state(driver):
    """Logout link, visible password fields, visible error messages, invalid form fields and body text of the current page."""
    try:
        return driver.execute_script(PAGE_STATE_JS)
    except Exception as e:
        logging.info(f"Could not read the page state: {e}")
        return {"url": "", "errors": [], "invalid": [], "logout": False, "passwords": 0, "text": ""}

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
def fill_sign_up_form(driver, start_url):
    global should_continue, email_used, password_used
    driver = ensure_driver(driver)
    base_domain = get_base_domain(start_url)
    visited_urls = set()
    urls_to_check = [start_url]
    form_filled = False

    # Stops once an account is made. After an error past that point (swantradecapital.com, Oct 26)
    # the loop carried on and loaded every queued page again, about 55 s of page loads for nothing.
    while urls_to_check and should_continue and not form_filled:
        current_url = urls_to_check.pop(0)
        if current_url in visited_urls or is_blacklisted(current_url):
            continue
        visited_urls.add(current_url)
        try:
            if has_two_or_more_password_fields(driver, current_url) and not form_filled and should_continue:
                driver = ensure_driver(driver)
                driver.get(current_url)
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
                    if form is None and captcha_answer is not None and should_continue and pass_captcha_gate(driver, captcha_answer, captcha_target):
                        captcha_blocks, captcha_summary, captcha_answer, captcha_target = check_captcha(driver)
                        logging.info(f"Captcha check after the gate on {driver.current_url}: {captcha_summary}")
                        if captcha_blocks:
                            return False
                        form = pick_signup_form(driver)
                    if form and should_continue:
                        logging.info(f"Sign-up form chosen: action={form.get_attribute('action')!r}")
                        predictions = predict_values(form)
                        password_used = next((v for v in predictions.values() if v in (ACCOUNT_PASSWORD, ACCOUNT_PASSWORD_ALT)), ACCOUNT_PASSWORD)
                        if password_used == ACCOUNT_PASSWORD_ALT:
                            logging.info(f"The password fields' maxlength/pattern refuse the usual password; using {ACCOUNT_PASSWORD_ALT!r}")
                        fill_form(form, predictions)
                        time.sleep(1)

                        if not should_continue:
                            return False

                        handle_dropdowns(form, driver)
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
                                # cryptocoinprop.com (/verify-email/) and swifttradinginc.live refuse
                                # login until the address is confirmed, so the search would be wasted.
                                logging.info("Account created, waiting on e-mail verification (no confirmation link was clicked); skipping the login search")
                                break

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
                        logging.info("No sign-up form found on the page (no visible form with a password field that is not a login form).")
                except Exception as e:
                    logging.error(f"An error occurred: {str(e)}")

            if not form_filled:
                for link in driver.find_elements(By.CSS_SELECTOR, 'a[href]'):
                    absolute_link = urljoin(current_url, link.get_attribute('href'))
                    # same_site() also takes subdomains: securenovatrade.com's sign-up and login are on
                    # account.securenovatrade.com, which used to count as another site.
                    if same_site(absolute_link, base_domain) and not is_blacklisted(absolute_link):
                        if absolute_link not in visited_urls and absolute_link not in urls_to_check and should_continue:
                            urls_to_check.append(absolute_link)
        except TimeoutException:
            logging.info(f"Sign-up unsuccessful for {current_url}. Website not reachable")
    if not form_filled:
        logging.info("No page with two or more password fields found.")
    return form_filled

# Check for multiple password fields
def has_two_or_more_password_fields(driver, url):
    if url.startswith('javascript:'):
        return None
    keywords = ['joinus', 'register', 'signup','createaccount', 'newuser', 'registration'] #Keywords to detect signup pages in DOM
    if any(keyword in url.lower() for keyword in keywords):
        logging.info(f"Registration URL is {url}")
        return url
    try:
        driver.get(url)
        password_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]')
        if len(password_inputs) >= 2:
            return url
    except Exception as e:
        logging.error(f"An error occurred while fetching {url}: {str(e)}")

# "Success" used to be logged whenever a sign-up form was submitted, including sites where
# the login then failed (swifttradinginc.live). It now needs a login that was not judged failed.
def log_site_result(url, email):
    if logged_in:
        logging.info(f"Success: {url} (logged in as {email})")
    elif email:
        logging.info(f"Signed up but not logged in: {url} ({email})")
    else:
        logging.info(f"Timeout or failure: {url}")

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
        logging.info(f"Login result: {outcome} - {evidence}")
        if outcome == "failed":
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

# Save and crawl the site from the current, logged-in page. The folder is named after
# folder_url: the login page, or the page that was already logged in when no login was needed.
def crawl_logged_in(driver, folder_url, base_domain, original_window):
    global logged_in
    logged_in = True
    dismiss_blocking_popups(driver)  # before the links are read: an overlay can hide or replace them

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
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    time.sleep(3)
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
# dropped first so tokens inside them are not reported.
# addr1... is a Cardano (Shelley) address, as on swantradecapital.com's Cardano payment page.
# XRP (r...) and Dogecoin (D...) as on digital4xtrade.com, whose deposit passes showed
# rJn2zAPdFA193sixJwuFixRkYDUtx3apQh and DDHZqHFipkxPXxTS8MryJNfrUjeQNh9jCD that the log missed.
# Solana's bare base58 is left out: it cannot be told apart from other long tokens.
WALLET_LIKE_RE = re.compile(r"\b(bc1[02-9ac-hj-np-z]{25,60}|[13][1-9A-HJ-NP-Za-km-z]{25,34}|0x[0-9a-fA-F]{40}|T[1-9A-HJ-NP-Za-km-z]{33}"
                            r"|addr1[02-9ac-hj-np-z]{50,110}|r[1-9A-HJ-NP-Za-km-z]{24,34}|D[5-9A-HJ-NP-U][1-9A-HJ-NP-Za-km-z]{32})\b")

# Hidden inputs are dropped too, and all-hex strings are skipped: bitvestwise.net's hidden
# form_token 388d8db1cc6a514b9dafc5a8bb51c484 (an MD5) fits the legacy Bitcoin pattern.
def wallet_like(html_content):
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", html_content, flags=re.S | re.I)
    visible = re.sub(r"<input\b[^>]*\btype=[\"']?hidden\b[^>]*>", " ", visible, flags=re.I)
    # Embedded data: URIs (base64 images) hold random letter runs; opextrade.com's pages gave
    # "rH1t9WP7Y1JjsfP9nnuHTUS9MxAAA" and similar r... strings out of them.
    visible = re.sub(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]+", " ", visible)
    # An r.../D... match must mix digits, upper and lower case, as real addresses do, so long
    # lower-case words starting with r are not taken for XRP addresses.
    mixed = lambda w: re.search(r"\d", w) and re.search(r"[A-Z]", w) and re.search(r"[a-z]", w)
    return sorted({w for w in WALLET_LIKE_RE.findall(visible)
                   if (w.startswith('0x') or not re.fullmatch(r"[0-9a-fA-F]+", w))
                   and (w[0] not in 'rD' or mixed(w))})

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
DEPOSIT_BUTTONS_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const label = b => (b.innerText || b.value || '').trim().replace(/\s+/g, ' ');
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

def click_deposit_button(driver, directory_name, base_domain, original_window, processed_links):
    buttons = driver.execute_script(DEPOSIT_BUTTONS_JS)
    if not buttons:
        logging.info("No deposit link or deposit button on the post-login page")
        return
    button = buttons[0]
    label = (button.text or button.get_attribute('value') or '').strip()
    start_url = driver.current_url
    handles_before = set(driver.window_handles)
    driver.execute_script("arguments[0].click();", button)
    time.sleep(3)
    new_tabs = [h for h in driver.window_handles if h not in handles_before]
    if new_tabs:
        driver.switch_to.window(new_tabs[0])

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
  const cands = Array.from(document.querySelectorAll('button, a, [role=button], input[type=button]')).filter(b =>
      vis(b) && !(form && form.contains(b) && (b.type || '').toLowerCase() === 'submit') && LABEL.test(clean(b.innerText || b.value))
      && !b.closest('nav, header, aside, footer, [class*=sidebar], [class*=menu], [class*=navbar]'));
  const groups = {};
  cands.forEach(b => {
    const m = (b.getAttribute('onclick') || '').match(/^\s*([\w$.]+)\s*\(/);
    const key = m ? 'fn:' + m[1] : 'text:' + clean(b.innerText || b.value).toLowerCase();
    (groups[key] = groups[key] || []).push(b);
  });
  const best = Object.values(groups).sort((a, b) => b.length - a.length)[0] || [];
  return best.map(b => {
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

DEPOSIT_FORM_JS = DEPOSIT_HELPERS_JS + METHOD_BUTTONS_JS + r"""
const PLACEHOLDER = /^(select|choose|please|--|-)/i;
const PLANLIKE = /%|\bdaily\b|\bhours?\b|\bdays?\b|\bweeks?\b|\bprofit|\bmin(imum)?\b|\bmax(imum)?\b|\bplan\b|\bpackage\b|\blevel\b/i;
const PLANNAME = /h_id|plan|package|scheme|tariff|level/i;
const PAYNAME = /type|pay|method|gateway|currency|coin|wallet|processor|system|ps$|ec$/i;
const AMOUNTNAME = /amount|sum/i;
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
  const submit = els.some(e => (e.tagName === 'BUTTON' && (e.getAttribute('type') || 'submit').toLowerCase() === 'submit')
      || (e.tagName === 'INPUT' && ['submit', 'image'].includes(e.type)));
  return {index, name: index < 0 ? 'deposit box without a form' : (f.getAttribute('name') || f.id || ''),
          amount: amount ? keyOf(amount, els) : null, controls: controls.filter(c => c.options.length), submit};
}
const described = Array.from(document.forms).map((f, index) => describe(f, index));
const byControls = (a, b) => b.controls.length - a.controls.length;
let form = described.filter(d => d.amount).sort(byControls)[0]
        || described.filter(d => d.controls.length && d.submit).sort(byControls)[0];
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
return {forms: described.length, form, plan, payment};
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

    def accept_alert(where):
        try:
            alert = driver.switch_to.alert
            logging.info(f"Deposit page {href}: accepted a dialog {where}: {alert.text[:150]!r}")
            alert.accept()
        except NoAlertPresentException:
            pass

    driver.execute_script("window.open(arguments[0]);", href)
    driver.switch_to.window(driver.window_handles[-1])
    try:
        wait_for_page()
        dismiss_blocking_popups(driver)
        # The page as the site serves it, before anything is filled in. Some sites show their
        # wallet addresses here already, and it shows what the form offered if the passes fail.
        capture(0)
        shown = wallet_like(driver.page_source)
        if shown:
            logging.info(f"Deposit page {href}: wallet-like strings on the page as served: {shown}")
        found = driver.execute_script(DEPOSIT_FORM_JS)
        form, plan, payment = found.get('form'), found.get('plan'), found.get('payment')
        if not form:
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
        options = payment['options'] if payment else [None]
        if len(options) > MAX_DEPOSIT_PASSES:
            logging.info(f"Deposit page {href}: {len(options)} payment options, only the first {MAX_DEPOSIT_PASSES} are tried")
            options = options[:MAX_DEPOSIT_PASSES]
        chosen = {c['name'] for c in (plan, payment) if c}
        others = [f"{c['kind']} {c['name']!r}" for c in form['controls'] if c['name'] not in chosen]
        logging.info(
            f"Deposit page {href}: form #{form['index']} {form['name']!r}; "
            + (f"plan {plan['kind']} {plan['name']!r} ({len(plan['options'])} options), using {plan_choice['label']!r}"
               + (f" range {plan_choice['range']}" if plan_choice.get('range') else "") if plan else "no plan control")
            + "; " + (f"payment {payment['kind']} {payment['name']!r} with {len(payment['options'])} options "
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
                by_button = bool(payment) and payment['kind'] == 'button'
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
                state = driver.execute_script(
                    DEPOSIT_FILL_JS, form['index'],
                    {'kind': plan['kind'], 'name': plan['name'], 'value': plan_choice['value']} if plan else None,
                    {'kind': payment['kind'], 'name': payment['name'], 'value': option['value']} if option and not by_button else None,
                    form['amount'], (plan_choice or {}).get('range') or (option or {}).get('range'))
                if state.get('error'):
                    logging.info(f"Deposit pass {counter} on {href}: {state['error']}, stopping")
                    break
                if by_button:
                    state['payment'] = driver.execute_script(FIELD_VALUE_JS, form['index'], payment['name'])
                how = driver.execute_script(DEPOSIT_SUBMIT_JS, form['index'])
                time.sleep(1)
                accept_alert("after submitting")
                wait_for_page()
                capture(counter)
                passes += 1
                found_wallets = wallet_like(driver.page_source)
                wallets.update(found_wallets)
                result = driver.execute_script(DEPOSIT_RESULT_JS, form['amount'], form['index'])
                logging.info(
                    f"Deposit pass {counter} on {href}: payment {option['label'] if option else 'site default'!r}"
                    f"{' (' + option['value'] + ')' if option else ''}; submitted with {state} via {how}; "
                    f"result title {driver.title[:60]!r}, url {driver.current_url}, wallet-like strings {found_wallets or 'none'}"
                    + (f"; the deposit form came back{': ' + ' | '.join(result['messages']) if result['messages'] else ' with no message'}"
                       if result['back'] else ""))
                form_back_in_a_row = form_back_in_a_row + 1 if result['back'] and not found_wallets else 0
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
    global should_continue, form_filled, logged_in, email_used, password_used, relogins_done, session_lost
    should_continue = True
    form_filled = False
    logged_in = False
    email_used = None  # else a previous site's address leaked into this site's result
    password_used = ACCOUNT_PASSWORD
    relogins_done, session_lost = 0, False
    deposit_pages_opened.clear()

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
            is_signup_page = current_url in (start_url, signup_url) or any(k in current_url.lower() for k in SIGNUP_URL_KEYWORDS)
            login_url = None if is_signup_page else has_single_password_field(driver, current_url)
            state = page_state(driver)
            logging.info(f"Login search: {current_url}"
                         + (f" -> {state['url']}" if state['url'] and state['url'] != current_url else "")
                         + f"; {state['passwords']} visible password field(s)"
                         + ("; Logout link" if state['logout'] else "") + ("; sign-up page, skipped" if is_signup_page else ""))
            if login_url:
                logging.info(f"URL with a single password field in a form: {login_url}")
                return login_url, None
            if state['logout'] and not state['passwords']:
                return None, state['url']
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
            for link in driver.find_elements(By.TAG_NAME, 'a'):
                href = link.get_attribute('href')
                if not href or href.startswith(('javascript:', 'mailto:', 'tel:')):
                    continue
                absolute_link = urljoin(current_url, href)
                if (not same_site(absolute_link, base_domain) or absolute_link in visited_urls or LANGUAGE_LINK_RE.search(absolute_link)
                        or SESSION_END_RE.search(absolute_link)
                        or absolute_link in urls_to_check or is_blacklisted(absolute_link)):
                    continue
                if LOGIN_URL_RE.search(absolute_link) or LOGIN_TEXT_RE.match(link.text or ''):
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
    global driver, chrome_options, current_site_url
    # --url crawls one site without reading urls.txt or touching the crawled_urls.txt
    # checkpoint, for testing a single site; --show-browser runs Chrome with a window.
    parser = argparse.ArgumentParser(description="Crimson authenticated crawler")
    parser.add_argument('--url', help="crawl only this site instead of urls.txt")
    parser.add_argument('--show-browser', action='store_true', help="run Chrome with a visible window")
    args = parser.parse_args()
    if args.show_browser:
        chrome_options = setup_chrome_options(headless=False)
    if args.url:  # also print the log to the terminal
        console = logging.StreamHandler(sys.__stdout__)
        console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(console)

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

if __name__ == "__main__":
    main()