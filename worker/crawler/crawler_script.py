
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
ACCOUNT_PASSWORD = 'Pass@1234'
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

def setup_chrome_options():
    chrome_options = webdriver.ChromeOptions()
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
    service = Service(executable_path=CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    handle_alert(driver)  # Handle alerts immediately after driver initialization
    return driver

driver = None  # created in main(); fill_form() and the URL loop use this global
# The urls.txt line being crawled. login_and_take_screenshot() writes it to
# source_url.txt in the site's folder, because the folder is named after the login
# URL, which can be on another host than the site feeder.py gave the crawler.
# build_wallets.py reads it to tie each wallet to its domain.
current_site_url = None

# Generate random strings and emails for filling forms
def generate_random_string(length=8, names_file=NAMES_FILE): #names.txt contains a dictionary of common names
    with open(names_file, 'r') as file:
        names = [line.strip() for line in file.readlines()]
    random_name = random.choice(names)
    result_string = f"{random_name}"
    return result_string[:length]

def generate_random_email():
    random_number = random.randint(60, 99)
    username = generate_random_string()
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

# Predict form values
def predict_values(form):
    predictions = {}
    predicted_password = ACCOUNT_PASSWORD
    predicted_username = generate_random_string()
    predicted_email = f"{predicted_username}{random.randint(60, 99)}@{EMAIL_DOMAIN}"
    input_tags = form.find_elements(By.TAG_NAME, 'input')

    for input_tag in input_tags:
        input_name = input_tag.get_attribute('name')
        input_type = input_tag.get_attribute('type')
        if input_name:
            if "username" in input_name.lower():
                predictions[input_name] = predicted_username
            elif input_type.lower() == "password":
                predictions[input_name] = predicted_password
            elif "email" in input_name.lower():
                predictions[input_name] = predicted_email
            else:
                predictions[input_name] = generate_random_string()
    return predictions

# Fill form
def fill_form(predictions):
    for input_name, prediction in predictions.items():
        input_field = driver.find_element(By.NAME, input_name)
        try:
            if input_field.is_displayed() and input_field.is_enabled():
                input_field.send_keys(prediction)
            else:
                logging.info(f"Element {input_name} is not interactable")
        except ElementNotInteractableException:
            logging.info(f"Element {input_name} is not interactable")

# Handle checkboxes
def handle_checkbox(driver):
    try:
        checkboxes = WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'input[type="checkbox"]')))
        for checkbox in checkboxes:
            try:
                if checkbox.is_displayed() and checkbox.is_enabled() and not checkbox.is_selected():
                    checkbox.click()
                    logging.info("Checkbox clicked directly.")
                else:
                    label = driver.find_element(By.XPATH, f"//label[@for='{checkbox.get_attribute('id')}']")
                    if label.is_displayed():
                        label.click()
                        logging.info("Checkbox label clicked.")
            except Exception:
                driver.execute_script("arguments[0].click();", checkbox)
                logging.info("Checkbox clicked using JavaScript.")
    except TimeoutException as e:
        logging.error(f"Timed out waiting for checkboxes: {e}")
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
def handle_dropdowns(form, driver):
    select_elements = form.find_elements(By.TAG_NAME, 'select')
    for select_element in select_elements:
        try:
            select = Select(select_element)
            options = select.options
            if options:
                random_option = random.choice(options)
                select.select_by_value(random_option.get_attribute("value"))
        except WebDriverException:
            try:
                if options:
                    random_value = random.choice(options).get_attribute("value")
                    driver.execute_script("arguments[0].value = arguments[1];", select_element, random_value)
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
const logout = Array.from(document.querySelectorAll('a,button')).filter(vis).some(e =>
    /log-?out|sign-?out|logoff|a=logout/i.test(e.getAttribute('href') || '') ||
    /^\s*(log ?out|sign ?out|log off)\s*$/i.test(e.innerText || ''));
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
    if after["passwords"] and (errors or after["invalid"]):
        return "rejected", "; ".join(errors + after["invalid"])[:300]
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

def submit_form(driver, selectors):
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
    global should_continue, email_used
    driver = ensure_driver(driver)
    base_domain = get_base_domain(start_url)
    visited_urls = set()
    urls_to_check = [start_url]
    form_filled = False

    while urls_to_check and should_continue:
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
                    form = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'form')))
                    if form and should_continue:
                        predictions = predict_values(form)
                        fill_form(predictions)
                        time.sleep(1)

                        if not should_continue:
                            return False

                        handle_dropdowns(form, driver)
                        time.sleep(1)

                        if not should_continue:
                            return False

                        handle_checkbox(driver)

                        # After the fields are filled, so predict_values() cannot overwrite it.
                        if captcha_answer is not None:
                            answer_captcha(driver, captcha_answer, captcha_target)

                        selectors = [
                            ('input[type="submit"]', By.CSS_SELECTOR),
                            ('button[type="submit"]', By.CSS_SELECTOR),
                            ('button[type="button"]', By.CSS_SELECTOR),
                            ('input[name="submit"]', By.NAME)
                        ]

                        form_url = driver.current_url
                        before_submit = page_state(driver)
                        if submit_form(driver, selectors):
                            outcome, evidence = assess_signup(before_submit, wait_for_outcome(driver, before_submit), form_url)
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
                            logging.info(f"Form submission successful. Signed up using {email_used}")
                            email_checked = check_mailinator_inbox_and_click_links(
                                MAILINATOR_API_KEY, driver, email_used, signup_time=time.time())
                            if email_checked:
                                logging.info("Successfully interacted with new email.")
                            else:
                                logging.info(f"No confirmation email clicked within {MAILINATOR_WAIT_SECONDS} seconds.")

                            login_url = find_login_url_with_single_password_field(driver, current_url)
                            if login_url:
                                login_and_take_screenshot(driver, login_url, ACCOUNT_PASSWORD, email_used)
                            else:
                                logging.info("Could not find a login URL to proceed with login.")
                                return False
                            break

                        else:
                            logging.info("Form submission unsuccessful.")
                    else:
                        logging.info("No form found on the page.")
                except Exception as e:
                    logging.error(f"An error occurred: {str(e)}")

            if not form_filled:
                for link in driver.find_elements(By.CSS_SELECTOR, 'a[href]'):
                    absolute_link = urljoin(current_url, link.get_attribute('href'))
                    if get_base_domain(absolute_link) == base_domain and not is_blacklisted(absolute_link):
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
                    if email_used:
                        logging.info(f"Success: {url} ")
                    else:
                        logging.info(f"Timeout or failure: {url}")
                    save_last_processed_url(last_processed_file_path, url)
                except Exception as e:
                    logging.error(f"An error occurred while processing {url}: {e}")
                    break
            if url == last_processed_url:
                start_processing = True

# Check for single password field
def has_single_password_field(driver, url):
    if url.startswith('javascript:'):
        return None
    login_keywords = [
        'login', 'signin', 'log-in', 'sign-in', 'auth', 'authenticate',
        'account', 'user', 'useraccount', 'user-account', 'myaccount', 'userlogin', 'enter' #Keywords to detect login pages in DOM
    ]
    if any(keyword in url.lower() for keyword in login_keywords):
        logging.info(f"The login URLs are {url}")
        return url
    try:
        driver.get(url)
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
        password_inputs = soup.find_all('input', {'type': 'password'})
        if len(password_inputs) == 1:
            return url
    except Exception as e:
        logging.error(f"An error occurred while fetching {url}: {str(e)}")
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
            if input_type == "text":
                modified_email = re.sub(r'\d+', '', email_used).replace(f"@{EMAIL_DOMAIN}", "")
                username_input.send_keys(modified_email)
            else:
                username_input.send_keys(email_used)
        except TimeoutException:
            logging.error("Timeout occurred while trying to find the username/email input field.")

        password_input = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="password"]')))
        password_input.send_keys(predicted_password)

        before_submit = page_state(driver)
        try:
            submit_button = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'],input[type='submit']")))
            submit_button.click()
        except TimeoutException:
            submit_button = driver.find_element(By.CSS_SELECTOR, "form button, form input[type='button']")
            driver.execute_script("arguments[0].click();", submit_button)

        # Judged from the page, not from a URL change: a failed login that redirects to
        # ?a=login&say=invalid_login changes the URL too.
        outcome, evidence = assess_login(before_submit, wait_for_outcome(driver, before_submit))
        logging.info(f"Login result: {outcome} - {evidence}")
        if outcome == "failed":
            return

        directory_name = login_url.replace('://', '_').replace('/', '_')
        os.makedirs(directory_name, exist_ok=True)
        if current_site_url:
            with open(os.path.join(directory_name, 'source_url.txt'), 'w') as f:
                f.write(current_site_url + '\n')
        processed_links = set()

        links = driver.find_elements(By.TAG_NAME, 'a')
        process_links(driver, links, processed_links, base_domain, directory_name, original_window, max_links=25)
    except NoSuchElementException as e:
        logging.error(f"Element not found during login process: {e}")
    except TimeoutException as e:
        logging.error("Timeout occurred, login might have failed or the URL did not change.")
    except Exception as e:
        logging.error(f"Unexpected error during login and page capturing: {e}")
    finally:
        if original_window and original_window in driver.window_handles:
            driver.switch_to.window(original_window)

#Validate links to avoid clicking on logout/signout buttons
def validate_link(href, processed_links, base_domain):
    return href is not None and href not in processed_links and "logout" not in href.lower() and "signout" not in href.lower() and get_base_domain(href) == base_domain

def process_link(driver, href, directory_name, original_window):
    driver.execute_script("window.open(arguments[0]);", href)
    driver.switch_to.window(driver.window_handles[-1])
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    time.sleep(3)

    html_content = driver.page_source
    html_file_path = os.path.join(directory_name, f"page_{href.replace('://', '_').replace('/', '_').replace('?', '_')}.html")
    with open(html_file_path, 'w', encoding='utf-8') as file:
        file.write(html_content)

    result = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
    screenshot_data = base64.b64decode(result['data'])
    screenshot_path = os.path.join(directory_name, f"screenshot_{href.replace('://', '_').replace('/', '_').replace('?', '_')}.png")
    with open(screenshot_path, 'wb') as file:
        file.write(screenshot_data)

    logging.info(f"Captured and saved data for {href}")
    driver.close()
    driver.switch_to.window(original_window)

# Wallet-looking strings on a page, for the deposit-stage log lines only. A hint for reading
# the log: iocs_searcher.sh still does the real extraction. Script and style blocks are
# dropped first so tokens inside them are not reported.
WALLET_LIKE_RE = re.compile(r"\b(bc1[02-9ac-hj-np-z]{25,60}|[13][1-9A-HJ-NP-Za-km-z]{25,34}|0x[0-9a-fA-F]{40}|T[1-9A-HJ-NP-Za-km-z]{33})\b")

def wallet_like(html_content):
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", html_content, flags=re.S | re.I)
    return sorted(set(WALLET_LIKE_RE.findall(visible)))

def process_links(driver, links, processed_links, base_domain, directory_name, original_window, max_links=25):
    if len(processed_links) >= max_links:
        logging.info(f"Reached maximum link processing threshold of {max_links}. Stopping further processing.")
        return

    deposit_page_counter = 0

    # Logged so a site whose deposit page was never opened can be told apart from one whose
    # deposit page was opened but produced nothing (process_deposit_page logs that case).
    try:
        deposit_hrefs = sorted({h for h in (l.get_attribute('href') for l in links) if h and re.search(r'\bdeposits?\b', h.lower())})
        logging.info(f"Post-login page has {len(links)} links; deposit links: {deposit_hrefs or 'none'}")
    except Exception as e:
        logging.info(f"Could not list the deposit links on the post-login page: {e}")

    for link in links:
        href = link.get_attribute('href')
        if validate_link(href, processed_links, base_domain):
            try:
                is_deposit = re.search(r'\bdeposits?\b', href.lower()) # condition true if either deposit or deposits in url
                if is_deposit and deposit_page_counter < 5: # and within limit
                    logging.info(f"Opening deposit page {href} (deposit link {deposit_page_counter + 1} of at most 5)")
                    process_deposit_page(driver, href, directory_name, original_window)
                    deposit_page_counter += 1
                else:
                    if is_deposit:
                        logging.info(f"Deposit link {href} handled as an ordinary page: the limit of 5 deposit pages was reached")
                    process_link(driver, href, directory_name, original_window)
                processed_links.add(href)

                second_layer_links = driver.find_elements(By.TAG_NAME, 'a')
                for second_link in second_layer_links:
                    second_href = second_link.get_attribute('href')
                    if validate_link(second_href, processed_links, base_domain) and len(processed_links) < max_links:
                        is_deposit = re.search(r'\bdeposits?\b', second_href.lower())
                        if is_deposit and deposit_page_counter < 5:
                            logging.info(f"Opening deposit page {second_href} (deposit link {deposit_page_counter + 1} of at most 5)")
                            process_deposit_page(driver, second_href, directory_name, original_window)
                            deposit_page_counter += 1
                        else:
                            if is_deposit:
                                logging.info(f"Deposit link {second_href} handled as an ordinary page: the limit of 5 deposit pages was reached")
                            process_link(driver, second_href, directory_name, original_window)
                        processed_links.add(second_href)
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

DEPOSIT_FORM_JS = r"""
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const PLACEHOLDER = /^(select|choose|please|--|-)/i;
const PLANLIKE = /%|\bdaily\b|\bhours?\b|\bdays?\b|\bweeks?\b|\bprofit|\bmin(imum)?\b|\bmax(imum)?\b|\bplan\b|\bpackage\b|\blevel\b/i;
const PLANNAME = /h_id|plan|package|scheme|tariff|level/i;
const PAYNAME = /type|pay|method|gateway|currency|coin|wallet|processor|system|ps$|ec$/i;
const AMOUNTNAME = /amount|sum/i;
function range(s) {
  const m = (s || '').match(/\$?\s?([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*\$?\s?([\d,]+(?:\.\d+)?)/i);
  if (m) return [parseFloat(m[1].replace(/,/g, '')), parseFloat(m[2].replace(/,/g, ''))];
  const n = (s || '').match(/min(?:imum)?[^0-9$]{0,15}\$?\s?([\d,]+(?:\.\d+)?)/i);
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
const described = Array.from(document.forms).map((f, index) => {
  const els = Array.from(f.elements);
  const amount = els.find(e => e.tagName === 'INPUT'
      && ['text', 'number', 'tel', ''].includes((e.getAttribute('type') || '').toLowerCase())
      && AMOUNTNAME.test([e.name, e.id, e.placeholder].join(' ')));
  const controls = [];
  els.filter(e => e.tagName === 'SELECT' && !e.disabled && e.name).forEach(s => {
    const options = Array.from(s.options)
      .filter(o => !o.disabled && o.value !== '' && !PLACEHOLDER.test(clean(o.text)))
      .map(o => ({value: o.value, label: clean(o.text).slice(0, 80), range: range(o.text)}));
    controls.push({kind: 'select', name: s.name, options});
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
  return {index, name: f.getAttribute('name') || f.id || '', amount: amount ? amount.name : null,
          controls: controls.filter(c => c.options.length), submit};
});
const byControls = (a, b) => b.controls.length - a.controls.length;
const form = described.filter(d => d.amount).sort(byControls)[0]
          || described.filter(d => d.controls.length && d.submit).sort(byControls)[0];
if (!form) return {forms: described.length};
const plan = form.controls.find(c => c.plan) || null;
const rest = form.controls.filter(c => c !== plan);
const payment = rest.find(c => PAYNAME.test(c.name)) || rest.sort((a, b) => b.options.length - a.options.length)[0] || null;
return {forms: described.length, form, plan, payment};
"""

# Sets the plan, the amount and one payment option on the reloaded page, and returns what the
# form holds afterwards. The plan goes first because some sites refill the amount when the plan
# changes; that amount is kept when it lies inside the plan's range.
DEPOSIT_FILL_JS = r"""
const [index, plan, payment, amountName, range] = arguments;
const f = document.forms[index];
if (!f) return {error: 'the deposit form is not on the reloaded page'};
// The crawler's alert/confirm override (handle_alert) only covered the first page; a
// "Are you sure?" confirm() on submit would otherwise block the pass.
window.alert = () => {}; window.confirm = () => true;
const els = Array.from(f.elements);
function set(c) {
  if (!c) return null;
  if (c.kind === 'select') {
    const s = els.find(e => e.tagName === 'SELECT' && e.name === c.name);
    if (!s) return 'missing';
    s.value = c.value;
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
const a = amountName ? els.find(e => e.name === amountName) : null;
if (a) {
  const [min, max] = range || [null, null];
  let v = parseFloat((a.value || '').replace(/[^\d.]/g, ''));
  if (!(v > 0) || (min && v < min) || (max && v > max)) v = min || 100;
  a.value = String(v);
  ['input', 'change', 'keyup', 'blur'].forEach(t => a.dispatchEvent(new Event(t, {bubbles: true})));
  state.amount = a.value;
}
state.payment = set(payment);
return state;
"""

# Clicks the form's own submit button (or submits the form) once this script has returned, so
# the navigation does not cut the WebDriver call short.
DEPOSIT_SUBMIT_JS = r"""
const f = document.forms[arguments[0]];
const b = Array.from(f.elements).find(e => (e.tagName === 'BUTTON' && (e.getAttribute('type') || 'submit').toLowerCase() === 'submit')
    || (e.tagName === 'INPUT' && ['submit', 'image'].includes(e.type)));
setTimeout(() => { if (b) b.click(); else if (f.requestSubmit) f.requestSubmit(); else f.submit(); }, 0);
return b ? 'button ' + JSON.stringify((b.innerText || b.value || '').trim().slice(0, 30)) : 'form.submit()';
"""

# Whether the page after a submit is the deposit form again (a visible amount field), and any
# error or alert text it shows. The confirmation page also carries amount/type, but as hidden fields.
DEPOSIT_RESULT_JS = r"""
const amountName = arguments[0];
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const back = !!amountName && Array.from(document.forms).some(f => {
  const a = f.elements[amountName];
  return a && a.tagName === 'INPUT' && a.type !== 'hidden' && vis(a);
});
const messages = Array.from(document.querySelectorAll('.error, .alert, .alert-danger, .text-danger, .invalid-feedback, [role=alert]'))
  .filter(vis).map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(t => t && t.length < 200);
return {back, messages: Array.from(new Set(messages)).slice(0, 3)};
"""

def process_deposit_page(driver, href, directory_name, original_window):
    safe_href = href.replace('://', '_').replace('/', '_').replace('?', '_')

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
        # The page as the site serves it, before anything is filled in. Some sites show their
        # wallet addresses here already, and it shows what the form offered if the passes fail.
        capture(0)
        found = driver.execute_script(DEPOSIT_FORM_JS)
        form, plan, payment = found.get('form'), found.get('plan'), found.get('payment')
        if not form:
            logging.info(f"Deposit page {href}: no deposit form ({found.get('forms', 0)} form(s) on the page, "
                         f"none with an amount field or a choice control and a submit button); 0 passes planned")
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
                state = driver.execute_script(
                    DEPOSIT_FILL_JS, form['index'],
                    {'kind': plan['kind'], 'name': plan['name'], 'value': plan_choice['value']} if plan else None,
                    {'kind': payment['kind'], 'name': payment['name'], 'value': option['value']} if option else None,
                    form['amount'], (plan_choice or {}).get('range'))
                if state.get('error'):
                    logging.info(f"Deposit pass {counter} on {href}: {state['error']}, stopping")
                    break
                how = driver.execute_script(DEPOSIT_SUBMIT_JS, form['index'])
                time.sleep(1)
                accept_alert("after submitting")
                wait_for_page()
                capture(counter)
                passes += 1
                found_wallets = wallet_like(driver.page_source)
                wallets.update(found_wallets)
                result = driver.execute_script(DEPOSIT_RESULT_JS, form['amount'])
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
    global should_continue, form_filled
    should_continue = True
    form_filled = False

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
        return False
    return email_used if form_filled else None

# Find login URL with a single password field
def find_login_url_with_single_password_field(driver, start_url):
    visited_urls = set()
    urls_to_check = [start_url]
    login_url = None

    while urls_to_check:
        current_url = urls_to_check.pop(0)
        if current_url in visited_urls or is_blacklisted(current_url):
            continue
        visited_urls.add(current_url)
        try:
            driver.get(current_url)
            login_url = has_single_password_field(driver, current_url)
            if login_url:
                logging.info(f"URL with a single password field in a form: {login_url}")
                return login_url
            links = driver.find_elements(By.TAG_NAME, 'a')
            for link in links:
                absolute_link = urljoin(current_url, link.get_attribute('href'))
                if absolute_link not in visited_urls and absolute_link not in urls_to_check and not is_blacklisted(absolute_link):
                    urls_to_check.append(absolute_link)
        except Exception as e:
            logging.error(f"An error occurred while crawling {current_url}: {str(e)}")
    logging.info("No page with a single password field found.")
    return None

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

def _mailinator_links(inbox, msg_id, params):
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
    links = [l for l in dict.fromkeys(links) if 'unsubscribe' not in l.lower()]
    return sorted(links, key=lambda l: not any(h in l.lower() for h in MAILINATOR_CONFIRM_HINTS))

def check_mailinator_inbox_and_click_links(api_key, driver, email_address, signup_time,
                                           wait_seconds=MAILINATOR_WAIT_SECONDS):
    if not api_key or api_key == "YOUR_API_KEY":
        logging.info("Mailinator API key not configured; skipping email-confirmation step.")
        return False
    if not email_address or not email_address.endswith(f"@{EMAIL_DOMAIN}"):
        logging.info(f"Sign-up email {email_address!r} is not on {EMAIL_DOMAIN}; skipping email-confirmation step.")
        return False
    inbox = email_address.split('@')[0]
    params = {"token": api_key}
    since_ms = (signup_time - 30) * 1000  # small allowance for clock skew
    seen, clicked = set(), 0
    deadline = time.time() + wait_seconds
    while time.time() < deadline and should_continue and not clicked:
        try:
            resp = curl_requests.get(f"{MAILINATOR_API}/inboxes/{inbox}",
                                params={**params, "limit": 10, "sort": "descending"}, timeout=15)
            resp.raise_for_status()
            for msg in resp.json().get('msgs', []):
                if msg['id'] in seen or msg.get('time', 0) < since_ms:
                    continue
                seen.add(msg['id'])
                logging.info(f"Mailinator: '{msg.get('subject', '')}' arrived for {email_address}")
                for link in _mailinator_links(inbox, msg['id'], params)[:MAILINATOR_MAX_CLICKS]:
                    driver.get(link)
                    clicked += 1
                    logging.info(f"Clicked link in email: {link}")
        except (curl_requests.exceptions.RequestException, ValueError, KeyError) as e:
            logging.error(f"Error checking Mailinator inbox {inbox}: {e}")
        except WebDriverException as e:
            logging.error(f"Error opening a link from the Mailinator inbox {inbox}: {e}")
        if not clicked:
            time.sleep(5)
    return clicked > 0

# Main function
def main():
    global driver
    driver = initialize_driver()
    handle_alert(driver)  # Handle initial alerts right after the driver is initialized
    try:
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