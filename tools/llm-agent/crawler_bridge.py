"""The crawler's own e-mail and wallet code, imported for the LLM agent test.

Nothing here is new logic: it loads authentication_crawling/crawler_script.py and re-exports what the
agent needs, so the agent reads Mailinator and finds wallets exactly as the crawler does
(one read per message via _mailinator_message, subject-first; wallet_like() on the page text).

Two things make the import work in the agent's environment:
- browser-use 0.13.14 pins typing-extensions 4.15.0 and SeleniumBase needs >= 4.16, so SeleniumBase is
  not installed here. crawler_script.py only uses it to start its own Chrome (Driver), which the agent
  never does, so an empty stand-in module answers its `from seleniumbase import Driver`.
- crawler_script.py calls logging.basicConfig(filename='logs/logfile.log') at import. The caller sets
  up logging first, so that call does nothing; it still creates ./logs (git-ignored here).
- It also redirects stdout/stderr at import; both are restored below.
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
CRAWLER_DIR = os.path.normpath(os.path.join(HERE, '..', 'authentication_crawling'))

if 'seleniumbase' not in sys.modules:
    _stub = types.ModuleType('seleniumbase')

    def _no_driver(*args, **kwargs):
        raise RuntimeError('SeleniumBase is not installed in the agent environment')

    _stub.Driver = _no_driver
    sys.modules['seleniumbase'] = _stub

if CRAWLER_DIR not in sys.path:
    sys.path.insert(0, CRAWLER_DIR)

# crawler_script.py also points sys.stdout and sys.stderr at logs/logfile.log on import (its lines
# 82-83); the agent's live step log has to reach the terminal, so both are put back afterwards.
_stdout, _stderr = sys.stdout, sys.stderr
try:
    import crawler_script as cs  # noqa: E402
finally:
    sys.stdout, sys.stderr = _stdout, _stderr

# Identity
generate_random_email = cs.generate_random_email
generate_random_string = cs.generate_random_string
ACCOUNT_PASSWORD = cs.ACCOUNT_PASSWORD
ACCOUNT_PASSWORD_ALT = cs.ACCOUNT_PASSWORD_ALT
EMAIL_DOMAIN = cs.EMAIL_DOMAIN

# Mailinator
MAILINATOR_API = cs.MAILINATOR_API
MAILINATOR_API_KEY = os.environ.get('MAILINATOR_API_KEY') or cs.MAILINATOR_API_KEY
curl_requests = cs.curl_requests
mailinator_message = cs._mailinator_message      # one read per message, cached for the process
mailinator_links = cs._mailinator_links
mailinator_code = cs._mailinator_code
message_cache = cs._mailinator_messages          # msg_id -> message; a cached message costs no read
code_in_subject = cs.code_in_subject
VERIFY_SUBJECT_RE = cs.VERIFY_SUBJECT_RE
LOGIN_ID_RE = cs.LOGIN_ID_RE

# Pages and wallets
wallet_like = cs.wallet_like
get_base_domain = cs.get_base_domain

# Payout wallets for sign-up forms that ask for the user's own address: random, valid checksum, per coin
payout_address = cs.payout_address

# The crawler's e-mailed-code box detection (Selenium function body: returns the code input elements)
CODE_INPUTS_JS = cs.CODE_INPUTS_JS
