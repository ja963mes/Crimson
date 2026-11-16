"""HYIP (A) or another kind of scam (B), decided from what the crawler sees on a site.

The evidence and the rule come from the Oct 31 feature research ("HYIP Feature Venn",
https://claude.ai/artifact/TRZujgdCnkbHH9tBAv75iu; data and scripts in crawler-test/hyip-research/).
It measured ~127 features over 925 HYIP and 216 other-scam sites. The one thing that separates
them is the self-service package: an HYIP lets you open an account, pick a plan and deposit, all on
the site. The other scams hand the visitor off (a name + phone lead form, a contact-only page, an
article that links elsewhere, a download) or sell something else (a prop-firm challenge, hash
rate, a monthly subscription). Return promises, coin names, FAQ/team/terms sections and forex words
appear on both sides and decide nothing.

The page features are extracted with the research's own patterns (hyip-research/extract_features.py,
copied here unchanged), so the rates it measured (hyip_features.json, built by
hyip-research/build_rates.py) apply to them. Added since:
- plan_card_txt: "<word> <word> plan" with a % or a duration and a $ range nearby. plan_tiers only
  knows "basic/silver/gold... plan", so viodokexmarket's "Basic Miner Plan 2% ROI, 24 hours,
  $50 - $999.99" was missed (Nov 1).
- account_button: a login or sign-up control that is a <button> with no link behind it
  (gptcryptotrading's "Sign In", Nov 1).
- prop_challenge, subscription_pricing, hashrate_offer: what the other families sell instead of
  plans (algonodetrade, fundingpips and epicfuturetraders sell trading challenges; gptcryptotrading
  and cryptovault-5p8 monthly subscriptions; miningbest "Lifetime Hashrate", Nov 1).
Features from the crawl itself are named crawl_*.

Nothing here touches the browser: the crawler passes the page source in, so the rules also run
offline on saved pages and on the research data (hyip-research/eval_classifier.py).
"""
import json
import math
import os
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PAGES = 12  # pages read per site before login; the sign-up search can walk 25 or more

# Rates per feature from the research: {feature: {"region", "H": [n, N], "O": [n, N], "fam": {family: [n, N]}}}.
with open(os.path.join(HERE, 'hyip_features.json'), encoding='utf-8') as _f:
    RATES = json.load(_f)
FAMILY_NAMES = {'HYIP': 'HYIP', 'broker/trading': 'broker / trading', 'call-center': 'call-center lead funnel',
                'MLM': 'course / MLM funnel', 'news/review funnel': 'news / review funnel',
                'mining/generator': 'mining / faucet / task', 'malware': 'software / subscription',
                'other investment': 'other investment'}
OTHER_FAMILIES = [f for f in FAMILY_NAMES if f != 'HYIP']

# ---- Page features: the research's patterns (hyip-research/extract_features.py), unchanged -----

PARKED = re.compile(r'domain (is )?for sale|buy this domain|this domain (may be|is) (for sale|parked)|parkingcrew|sedo|dan\.com|afternic|'
                    r'account (has been )?suspended|website (is )?(expired|suspended)|hostinger.*(parked|coming soon)|'
                    r'default web page|it works!|welcome to nginx|index of /|domain has expired|this site can.t be reached|'
                    r'just a moment\.\.\.|attention required! \| cloudflare|checking your browser|enable javascript and cookies', re.I)


def W(p):
    return re.compile(p, re.I)


TEXT = {
    'txt_login': W(r'\b(log ?in|sign ?in)\b'),
    'txt_register': W(r'\b(sign ?up|register|create (an |your )?account|open (an |your )?account|join (us|now))\b'),
    'txt_callback': W(r'request (a )?call|call ?back|we will call you|(manager|specialist|agent|representative|advisor|expert|consultant)s? will (contact|call|reach)|get a (free )?call|leave (your|a) (phone|number|request)|schedule a call|book a (call|consultation)|free consultation'),
    'txt_personal_manager': W(r'personal (account )?manager|dedicated (account )?manager|personal (advisor|adviser|broker|analyst)|account manager'),
    'txt_kyc': W(r'\bkyc\b|verify your identity|identity verification|upload (your )?(id|passport|document)'),
    'pct_per_period': W(r'\d+(\.\d+)?\s?%\s*(/\s*)?(daily|a day|per day|every day|hourly|per hour|weekly|per week|monthly|per month|after|for \d+)|(daily|hourly|weekly) (profit|return|roi|interest|income|earning)s?'),
    'plan_tiers': W(r'\b(basic|starter|standard|silver|gold|platinum|diamond|vip|premium|bronze|master|elite|beginner|advanced)\s+(plan|package)\b'),
    'min_max_deposit': W(r'(min(imum)?|max(imum)?)\.?\s+(deposit|investment|invest|amount)'),
    'principal_return': W(r'principal|capital (back|return)|deposit (returned|included)'),
    'calculator': W(r'(profit|investment|roi|earnings?) calculator|calculate (your )?(profit|earnings)'),
    'referral': W(r'referr?al|affiliate|refer (a )?friend|invite (a )?friend'),
    'referral_levels': W(r'(level|tier) ?[123]\b.{0,40}%|\b[2-9][- ]levels?\b|multi.?level'),
    'mlm_structure': W(r'\bbinary\b|\bmatrix\b|\bupline\b|\bdownline\b|matching bonus|leadership bonus|rank (bonus|reward|achievement)|\bsponsor\b|team bonus|network marketing'),
    'stats_counters': W(r'days? online|running days|online since|total (members|users|investors|accounts|deposit(ed|s)?|invested|withdraw(n|als)?|paid( out)?)|registered (users|members)'),
    'last_tx_table': W(r'(last|latest|recent) (deposits|withdrawals|payouts|transactions|investments)'),
    'activity_popup_txt': W(r'just (invested|deposited|withdrew|withdrawn|earned|received|joined)|has (just )?(invested|withdrawn)'),
    'instant_withdraw': W(r'instant (withdrawal|payout|payment)s?|withdraw(al)? (anytime|instantly|24/7)|fast (withdrawal|payout)s?'),
    'guaranteed': W(r'guarantee(d)? (profit|return|income|payout)|risk[- ]free|100% (safe|secure|guaranteed)|no risk'),
    'ecurrency': W(r'perfect ?money|payeer|advcash|advanced cash|epaycore|fchange|volet'),
    'crypto_coins': W(r'bitcoin|\bbtc\b|ethereum|\beth\b|usdt|tether|trc-?20|bep-?20|litecoin|dogecoin|\btron\b|\btrx\b'),
    'fiat_cards_wire': W(r'\bvisa\b|mastercard|bank (transfer|wire)|wire transfer|credit card|debit card|skrill|neteller'),
    'company_reg': W(r'company (number|no\.?|registration)|registration (number|no)|registered in (the )?(uk|united kingdom|england|usa|united states)|companies house|certificate of (incorporation|registration)|incorporated in|\bltd\b.{0,30}registered'),
    'regulation_claim': W(r'regulated by|licensed by|\bfca\b|cysec|\basic\b|\bnfa\b|fincen|\bmsb\b|sec[- ]registered|financial conduct authority|license (number|no)'),
    'security_badges': W(r'ddos protect|ssl (secured|certificate|protected)|secured by|comodo|sectigo|geotrust|256[- ]bit'),
    'trading_platform': W(r'metatrader|\bmt[45]\b|webtrader|web trader|trading platform|trading terminal|trade now'),
    'leverage_spreads': W(r'leverage|spreads?\b|\bpips?\b|lot size|margin (call|trading)|stop loss|take profit'),
    'asset_classes': W(r'\bforex\b|\bcfds?\b|\bstocks?\b|indices|commodities|\boil\b|\bgold\b.{0,20}(trading|price)|\betfs?\b|options trading'),
    'account_types': W(r'(standard|ecn|pro|micro|islamic|demo|vip|classic|premium|mini) accounts?|account types'),
    'demo_account': W(r'demo account|free demo|practice account'),
    'risk_warning': W(r'risk warning|risk disclosure|cfds are complex|lose money|high (level of )?risk|past performance'),
    'copy_ai_bot': W(r'copy[- ]trading|trading signals?|trading bot|\bai\b.{0,15}(trading|bot|powered|algorithm)|algorithmic|auto[- ]?trad'),
    'article_markers': W(r'published (on|by)|updated on|posted (on|by)|share this (article|post)|leave a (comment|reply)|min(ute)? read|editor.s (note|pick)'),
    'invest_cta': W(r'invest now|make (a )?deposit|deposit now|start investing|choose (a |your )?plan|get started now'),
    'deposit_withdraw_ui': W(r'\b(deposit|withdraw)\b.{0,40}\b(withdraw|deposit)\b'),
    # Nov 6: the two words anywhere on a page, each on its own. deposit_withdraw_ui wants them within
    # 40 characters; 14 of James's HYIPs on the 10-31 CSV use both words, only further apart (no
    # research rates yet, so they are recorded as evidence and not scored).
    'deposit_word': W(r'\bdeposit(s|ed|ing)?\b|fund (your|the) account|add funds|top ?up'),
    'withdraw_word': W(r'\bwithdraw(al|als|n|ing)?\b|cash ?out|\bpayouts?\b'),
    'media_celebrity': W(r'elon musk|bill gates|jeff bezos|\bbbc\b|\bcnn\b|dragons.? den|shark tank|as seen on|featured (in|on)|forbes|bloomberg|reuters|yahoo finance|the guardian'),
    'review_language': W(r'\breview(s|ed)?\b.{0,40}(scam|legit)|is it (a )?(scam|legit)|pros and cons|our verdict|\brating\b|trustpilot'),
    'urgency': W(r'limited (time|spots|places|offer)|only \d+ (spots|places|slots)|offer (ends|expires)|hurry|don.t miss|last chance|spots? (left|remaining)'),
    'cloud_mining': W(r'cloud mining|hash ?rate|\b[tgpe]h/s\b|mining (contract|plan|pool|farm|power|rig)|\basics?\b|\bminers?\b'),
    'generator_doubler': W(r'generator|private key|double your|\bx2\b|send .{0,30}receive|giveaway|airdrop|claim (your|free)'),
    'token_ico': W(r'whitepaper|white paper|tokenomics|presale|pre-sale|\bico\b|buy (the )?token|token sale|staking'),
    'download_txt': W(r'download (for|on|the|our)? ?(windows|mac|app|apk|now|wallet|extension|software|client)|install (the )?(app|extension)|available on (the )?(app store|google play)'),
    'faq': W(r'\bfaqs?\b|frequently asked'),
    'testimonials': W(r'testimonials?|what (our|people|clients|investors|customers) (say|said)|reviews from|client reviews'),
    'team_section': W(r'our team|meet the team|(ceo|founder|co-founder|managing director)\b'),
    'how_it_works': W(r'how it works|how to (start|invest|get started)|(3|three|4|four) (easy |simple )?steps|step 1'),
    'privacy_terms': W(r'privacy policy|terms (and|&) conditions|terms of (use|service)'),
    'aml_policy': W(r'\baml\b|anti[- ]money laundering'),
    'contact_txt': W(r'contact (us|support)|get in touch|support@|customer support'),
    'address_txt': W(r'\b\d{1,5}\s+\w+(\s\w+)?\s+(street|st\.|avenue|ave|road|rd\.|lane|boulevard|blvd|suite|floor)\b|london|new york|united kingdom'),
    'roadmap': W(r'\broadmap\b'),
    'partners_logos': W(r'our partners|partners|trusted by|payment (methods|systems|partners)'),
}

JS = {
    'chat_widget': r'tawk\.to|jivosite|jivo|livechatinc|livechat|smartsupp|crisp\.chat|tidio|zopim|zendesk|chatra|intercom|chaport|userlike|purechat|olark|getbutton|livehelp|smartsupp|chatwoot|botpress|kommo',
    'translate_widget': r'gtranslate|translate\.google|google_translate|translate_a/element',
    'tradingview_widget': r'tradingview',
    'crypto_ticker_widget': r'coinmarketcap|cryptocompare|coingecko|cryptohopper|livecoinwatch|coinlib|widget\.coin',
    'fake_activity_js': r'faker(\.min)?\.js|faker',
    'toastr_notify_js': r'toastr|notify|izitoast|sweetalert|noty\.',
    'analytics': r'googletagmanager|google-analytics|gtag|yandex|metrika|facebook\.net|fbevents',
    'recaptcha': r'recaptcha|hcaptcha|turnstile',
    'wordpress': r'wp-content|wp-includes',
    'react_next_vite': r'_next/|/static/js/main\.|vite|react|chunk\.js|app\.[0-9a-f]{6,}\.js',
    'aos_wow_anim': r'aos\.js|wow(\.min)?\.js|animate|gsap|particles',
}
INLINE_JS = ('chat_widget', 'translate_widget', 'tradingview_widget', 'crypto_ticker_widget', 'recaptcha')

LOGIN_RE = re.compile(r'\b(log ?in|sign ?in)\b|[?&/]a=login|/login', re.I)
REG_RE = re.compile(r'\b(sign ?up|register|registration|create (an |your )?account|open (an |your )?account|join now|get started)\b|[?&]a=signup|/register|/signup', re.I)
CONTACT_RE = re.compile(r'\bcontact\b|[?&]a=support', re.I)
SOCIAL = {'telegram_link': r't\.me/|telegram\.(me|org)', 'whatsapp_link': r'wa\.me|whatsapp\.com|api\.whatsapp',
          'facebook_link': r'facebook\.com', 'instagram_link': r'instagram\.com', 'x_twitter_link': r'twitter\.com|//x\.com',
          'youtube_link': r'youtube\.com|youtu\.be', 'linkedin_link': r'linkedin\.com', 'discord_link': r'discord\.(gg|com)',
          'appstore_link': r'apps\.apple\.com|play\.google\.com', 'binary_download': r'\.(exe|msi|dmg|apk|zip|rar|pkg)(\?|$)'}


def form_feats(soup, page_kind):
    f = {}
    for form in soup.find_all('form'):
        inputs = form.find_all(['input', 'select', 'textarea'])

        def has(pat, types=None):
            for i in inputs:
                attrs = ' '.join(str(i.get(a, '')) for a in ('name', 'id', 'placeholder', 'type', 'aria-label', 'autocomplete', 'class')).lower()
                lab = ''
                if i.get('id'):
                    l = soup.find('label', attrs={'for': i.get('id')})
                    lab = l.get_text(' ', strip=True).lower() if l else ''
                if types and (i.get('type') or '').lower() in types:
                    return True
                if re.search(pat, attrs + ' ' + lab):
                    return True
            return False
        pw = has(r'password|passwd|pwd', {'password'})
        phone = has(r'phone|tel\b|mobile|whatsapp|telephone|cell', {'tel'})
        name = has(r'\bname\b|first.?name|last.?name|fullname|full_name|fname|lname')
        email = has(r'e-?mail', {'email'})
        msg = any(i.name == 'textarea' for i in inputs)
        country = has(r'country|nationality')
        ref = has(r'referr?al|upline|sponsor|invite|ref_?code|promo')
        wallet = has(r'(btc|bitcoin|usdt|eth|trx|tron|ltc|perfect|payeer|wallet|pm_account|account_?no|e-?currency|pay_account)')
        captcha = has(r'captcha|turing|verification code|security code') or form.find(class_=re.compile('recaptcha|h-captcha|cf-turnstile')) is not None
        secret = has(r'secret|question|answer|pin\b')
        terms = has(r'agree|terms|tos\b|accept')
        amount = has(r'amount|sum|deposit')
        if pw and (name or email) and (len(inputs) >= 4 or page_kind == 'register'):
            if not f.get('signup_form') or page_kind == 'register':
                f['signup_form'] = 1
                for k, v in (('signup_phone', phone), ('signup_country', country), ('signup_referral', ref),
                             ('signup_wallet_fields', wallet), ('signup_captcha', captcha), ('signup_secret_q', secret),
                             ('signup_terms', terms), ('signup_name', name)):
                    if v:
                        f[k] = 1
        elif pw:
            f['login_form'] = 1
        elif phone and (name or email) and not msg:
            f['lead_form_phone'] = 1  # name/e-mail + phone, no password, no message box: hand-off to a caller
            if page_kind == 'home':
                f['lead_form_on_home'] = 1
            if has(r'first.?name|fname') and has(r'last.?name|lname|surname'):
                f['lead_form_first_last'] = 1
        elif msg and (name or email):
            f['contact_form'] = 1
            if phone:
                f['contact_form_with_phone'] = 1
        elif email and len(inputs) <= 3:
            f['newsletter_form'] = 1
        if amount and not pw:
            f['amount_form'] = 1
    if soup.find('iframe', src=re.compile(r'(form|register|signup|lead|landing)', re.I)):
        f['iframe_form'] = 1
    if soup.find('input', attrs={'type': 'password'}):
        f['password_field_any'] = 1
    return f


def tech(raw):
    f = {}
    low = raw.lower()
    if re.search(r'[?&]a=(signup|login|faq|rules|cust|support|news|deposit)', low):
        f['goldcoders_hscript'] = 1  # GoldCoders H-Script / HYIP Manager Pro URL scheme
    if re.search(r'assets/templates/|viserlab|hyiplab', low):
        f['viserlab_hyiplab'] = 1
    if re.search(r'name="csrf-token"|laravel_session|livewire', low):
        f['laravel'] = 1
    if 'hyip' in low:
        f['word_hyip_in_source'] = 1
    if re.search(r'tilda|wix\.com|webflow|framer|squarespace|weebly|godaddy.*website builder|zyrosite|hostinger.*builder|site123|strikingly|carrd', low):
        f['site_builder'] = 1
    if re.search(r'hreflang|language-switcher|lang-switch|select language|language-select|lang-dropdown|class="[^"]*(lang|language)[-_](menu|switch|select|dropdown|list)', low):
        f['language_selector'] = 1
    if re.search(r'cookie(consent| consent|-banner|banner|s policy)', low):
        f['cookie_banner'] = 1
    if re.search(r'lorem ipsum', low):
        f['lorem_ipsum'] = 1
    if re.search(r'countdown|data-countdown|flipclock', low):
        f['countdown_timer'] = 1
    if re.search(r'intltelinput|intl-tel-input', low):
        f['intl_tel_input'] = 1  # country-code phone picker used by lead funnels
    if re.search(r'<video|youtube\.com/embed|player\.vimeo|wistia|vidalytics', low):
        f['video_embed'] = 1
    return f


def title_feats(title, domain):
    f = {}
    if re.search(r'official (web)?site|in (canada|australia|uk|united kingdom|south africa|india|nigeria|kenya|ireland|brazil|new zealand|singapore|germany|italy|spain|mexico)', title, re.I):
        f['title_official_country'] = 1
    if re.search(r'20(25|26)', title):
        f['title_year'] = 1
    if re.search('[ᐉ®™]', title):
        f['title_glyph'] = 1
    if re.search(r'\b(ai|bot|algorithm|automated)\b', title, re.I):
        f['title_ai_bot'] = 1
    # Nov 7 exclusions (precise versions; title_official_country/title_year/title_glyph above are loose:
    # they fire on any year or a trademark sign). "Official site/website" is the call-center template.
    if re.search(r'official (web)?site', title, re.I):
        f['excl_official_site'] = 1
    if re.search(r'\breview(s|ed)?\b|\bbest\b.{0,40}\b20\d\d\b|\btop \d+\b|\bvs\.?\b|\bcompar(e|ison)\b|\bnews\b|\bblog\b', title, re.I):
        f['excl_review_title'] = 1
    if re.search(r'\b(ltd|limited|llc|inc|corp|plc|group|holdings?)\b', title + ' ' + re.sub(r'[-.]', ' ', domain), re.I):
        f['name_corporate_suffix'] = 1
    if re.search(r'capital|invest|trade|asset|financ|fund|wealth|profit|crypto|coin|mining|bit', domain):
        f['domain_finance_word'] = 1
    if '.pages.dev' in domain:
        f['host_pages_dev'] = 1
    f['tld_com' if domain.rsplit('.', 1)[-1] == 'com' else 'tld_not_com'] = 1
    return f

# ---- Features added for the crawler (no research rates; used by the rules) ----------------------

# "<word> <word> plan" with a % or a duration and a $ range within reach: one plan card.
PLAN_NAME_RE = re.compile(r'\b(?:[a-z0-9]+\s+){1,2}(?:plan|package)\b', re.I)
PCT_RE = re.compile(r'\d+(\.\d+)?\s?%')
DURATION_RE = re.compile(r'\b\d+\s*(hours?|hrs?|days?|weeks?|months?)\b|\b(daily|hourly|weekly|monthly)\b', re.I)
MONEY = r'(?:[$€£]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:usd[tc]?|\$))'
RANGE_RE = re.compile(MONEY + r'\s*(?:-|–|to)\s*' + MONEY + r'|' + MONEY + r'\s*(?:-|–|to)\s*\d|'
                      r'(?:min(?:imum)?|max(?:imum)?)\.?\s*(?:deposit|investment|invest|amount)?\s*:?\s*' + MONEY, re.I)
PLAN_WINDOW = 250
# A monthly price: software, signals and bots are sold this way, HYIP plans are not.
MONTHLY_PRICE_RE = re.compile(r'[$€£]\s?\d[\d,]*(\.\d+)?\s*(/\s*(mo|month)\b|per month|a month)', re.I)
SUBSCRIPTION_RE = re.compile(r'free trial|\d+[- ]day (free )?trial|cancel (any ?time|at any time)|billed (monthly|annually|yearly)|'
                             r'(monthly|annual|yearly) (subscription|billing)|subscription (plans?|fee)', re.I)
# Prop firms sell a trading challenge for a fee. One "prop firm" / "funded account" is enough;
# other challenge words need two different ones ("trading challenge" alone is ordinary copy).
PROP_SURE_RE = re.compile(r'prop(rietary)?[- ]?(trading )?firms?|funded (trader|account)s?', re.I)
PROP_RE = re.compile(r'\b\d+k (challenge|account)|challenge (fee|account|phase)|evaluation (phase|account|challenge)|profit split|'
                     r'(trading|funding) challenges?|\binstant funding\b', re.I)
# Cloud mining sells hash rate. "Cloud mining" alone is not enough: mining-themed HYIPs use the words
# too (14% of HYIPs in the research), and sell plans with a % instead (viodokexmarket).
HASHRATE_RE = re.compile(r'hash ?rate|hash ?power|\b\d+(\.\d+)?\s?[kmgtpe]h/s\b|mining contracts?', re.I)
ACCOUNT_BUTTON_RE = re.compile(r'^\s*(member |client |investor )?(log ?in|sign ?in|sign ?up|register|create (an |your )?account'
                               r'|open (an |your )?account|join now)\s*$', re.I)


def offer_feats(text):
    f = {}
    for m in PLAN_NAME_RE.finditer(text):
        window = text[max(0, m.start() - PLAN_WINDOW):m.end() + PLAN_WINDOW]
        if (PCT_RE.search(window) or DURATION_RE.search(window)) and RANGE_RE.search(window):
            f['plan_card_txt'] = 1
            break
    monthly = {re.sub(r'\s', '', m.group(0)).lower() for m in MONTHLY_PRICE_RE.finditer(text)}
    if len(monthly) >= 2 or (monthly and SUBSCRIPTION_RE.search(text)):
        f['subscription_pricing'] = 1
    if PROP_SURE_RE.search(text) or len({m.group(0).lower() for m in PROP_RE.finditer(text)}) >= 2:
        f['prop_challenge'] = 1
    if HASHRATE_RE.search(text):
        f['hashrate_offer'] = 1
    return f


# ---- Nov 7: the positive-evidence rule's text signals and the precise exclusions --------------------
# Measured offline on James's labels (Nov 5-7): with these, "exclusion -> B; sign-up form + deposit and
# withdraw wording + a plan offer -> A; else B" matched 102 of 120 judged sites of confirmed-2026-11-02
# (crawler before: 89) and cut wrong YES calls from 25 to 8.
PCT_PERIOD_ANY_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s?%\s*(?:/\s*)?(daily|a day|per day|each day|every day|hourly|per hour|weekly|per week|monthly|per month|after (\d+) ?(hours?|days?|weeks?)|for (\d+) ?(hours?|days?|weeks?)|roi\b|profit|return)'
    r'|(daily|hourly|weekly|monthly|roi|profit|return|interest)\s*(?:profit|return|roi|interest|income|rate)?\s*[:\-]?\s*(?:up to )?(\d+(?:\.\d+)?)\s?%', re.I)
MONEY_RE = re.compile(r'(\$|usd|usdt|€|£|₹)\s?\d[\d,.]*|\d[\d,.]*\s?(\$|usd|usdt|€|£)', re.I)
PLAN_WORD_RE = re.compile(r'\b(plan|package|tier|account)\b', re.I)
STEP_ACC_RE = re.compile(r'(create|open|register|sign ?up|registration|set up)\s+(an |your |a free |a )?(account|profile)|\bsign ?up\b|\bregister\b|\bregistration\b|\bjoin (us|now)\b', re.I)
STEP_FUND_RE = re.compile(r'\bdeposit\b|fund (your )?(account|wallet)|add funds|top ?up|make (a |your )?(first )?deposit|choose (a |an |your )?(investment )?plan|select (a |an |your )?(investment )?plan', re.I)
STEP_EARN_RE = re.compile(r'\bearn(ing|ings)?\b|\bprofits?\b|\bwithdraw\b|get paid|\binvest(ing)?\b|start (trading|investing|earning)|\btrade\b|explore (the )?markets|returns?\b|grow your', re.I)
EXCL_TEXT = {
    'excl_prop': re.compile(r'prop(rietary)?[- ]?(trading )?firms?\b|funded trader program|(evaluation|trading) challenge\b|pass (the|our|your) (evaluation|challenge)|profit split', re.I),
    'excl_simulation': re.compile(r'exclusively (using|with) virtual|simulated (trading )?environment|no real (money|funds|capital) (is |are )?(ever )?(used|involved|at risk)|all trad(es|ing) (is |are )?simulated|simulated trading programs?|paper[- ]trading (platform|only|account)', re.I),
    'excl_faucet': re.compile(r'\bfaucet\b|claim (free )?(btc|bitcoin|litecoin|ltc|crypto|satoshis?)\b|(bitcoin|btc|crypto|usdt) (generator|flasher)|private key (generator|finder)|double your (bitcoin|btc|crypto)', re.I),
    'excl_crypto_loan': re.compile(r'crypto[- ]backed loans?|borrow (against|using) (your )?(crypto|bitcoin|btc)|loan[- ]to[- ]value|\bltv\b', re.I),
}

def _high_return(text):
    for m in PCT_PERIOD_ANY_RE.finditer(text):
        g = m.groups()
        pct, period, n = float(g[0] or g[7] or 0), (g[1] or g[6] or '').lower(), g[2] or g[4]
        if 'hour' in period or (('daily' in period or ('day' in period and 'after' not in period and 'for' not in period)) and pct >= 1):
            return True
        if ('week' in period and pct >= 5) or ('month' in period and pct >= 15) or (period == 'roi' and pct >= 100):
            return True
        if n and ('after' in period or 'for' in period):
            unit = (g[3] or g[5] or '').lower()
            days = int(n) / 24 if 'hour' in unit else int(n) * (7 if 'week' in unit else 1)
            if days <= 30 and pct >= 10:
                return True
    return False

def rule_feats(text):
    f = {}
    if PCT_PERIOD_ANY_RE.search(text):
        f['pct_return_any'] = 1
    for m in PLAN_WORD_RE.finditer(text):
        window = text[m.start(): m.end() + 250]
        if MONEY_RE.search(window) and DURATION_RE.search(window):
            f['plan_duration_money'] = 1
            break
    for a in STEP_ACC_RE.finditer(text):  # account -> (verify) -> fund -> earn/trade/invest within 900 characters
        window = text[a.end(): a.end() + 900]
        fund = STEP_FUND_RE.search(window)
        if fund and STEP_EARN_RE.search(window, fund.end()):
            f['flow_steps'] = 1
            break
    if _high_return(text):
        f['high_return'] = 1
    for k, rx in EXCL_TEXT.items():
        if rx.search(text):
            f[k] = 1
    return f


def button_feats(soup):
    """Login/sign-up controls that are buttons rather than links (dead ones included)."""
    for b in soup.find_all(['button', 'input']) + soup.find_all(attrs={'role': 'button'}):
        if b.name == 'input' and (b.get('type') or '').lower() not in ('button', 'submit'):
            continue
        label = b.get('value', '') if b.name == 'input' else b.get_text(' ', strip=True)
        if ACCOUNT_BUTTON_RE.match(label or ''):
            return {'account_button': 1}
    return {}

# ---- Evidence for one site ----------------------------------------------------------------------

LABELS = {
    'password_field_any': 'a password field', 'signup_form': 'a sign-up form with a password', 'login_form': 'a login form',
    'login_link': 'a login link', 'register_link': 'a register link', 'account_button': 'a login/sign-up button with no link',
    'crawl_signup_form': 'the crawler found a sign-up form with a password', 'crawl_logged_in': 'the crawler logged in',
    'crawl_deposit_plans': 'a deposit page with plan choices', 'crawl_deposit_form': 'a deposit form', 'crawl_wallets': 'deposit wallets shown',
    'crawl_lead_form': 'a name/e-mail/phone form and no password on the sign-up page', 'crawl_offsite_signup': 'sign-up on another site',
    'crawl_bo_deposit': 'back office: deposit page', 'crawl_bo_withdraw': 'back office: withdraw page',
    'crawl_bo_referral': 'back office: referral page', 'crawl_bo_plans': 'back office: plans/invest page',
    'crawl_bo_challenge': 'back office: challenges page',
    'deposit_word': 'deposit wording', 'withdraw_word': 'withdraw wording',
    'pct_return_any': '% returns', 'plan_duration_money': 'a plan with $ and a duration', 'flow_steps': 'account -> deposit -> earn steps',
    'high_return': 'very high returns', 'excl_official_site': '"official site" title', 'excl_review_title': 'review/news title',
    'excl_prop': 'prop-firm wording', 'excl_simulation': 'simulation wording', 'excl_faucet': 'faucet/generator wording',
    'excl_crypto_loan': 'crypto-loan wording',
    'plan_tiers': 'named plan tiers', 'plan_card_txt': 'plan cards with % or duration and a $ range', 'min_max_deposit': 'min/max deposit',
    'pct_per_period': '"% per day/hour" returns', 'principal_return': 'capital back / principal returned', 'last_tx_table': 'last deposits/withdrawals table',
    'goldcoders_hscript': 'HYIP script URLs (?a=signup)', 'viserlab_hyiplab': 'HyipLab template', 'signup_wallet_fields': 'sign-up asks for payout wallets',
    'lead_form_phone': 'a name + phone lead form', 'lead_form_first_last': 'separate first/last name fields', 'intl_tel_input': 'a country-code phone picker',
    'lead_form_on_home': 'the lead form is on the home page',
    'title_ai_bot': '"AI/bot" in the title', 'title_official_country': '"official site in <country>" title', 'txt_callback': '"request a call" wording',
    'contact_form': 'a contact form', 'contact_link': 'a contact link', 'article_markup': 'article/news layout', 'article_markers': 'article wording',
    'review_language': 'review language', 'register_offsite': 'register link to another site', 'prop_challenge': 'prop-firm challenge wording',
    'subscription_pricing': 'monthly subscription pricing', 'hashrate_offer': 'hash-rate / mining-contract offer',
    'translate_widget': 'Google Translate bar', 'chat_widget': 'live-chat widget', 'tradingview_widget': 'TradingView widget',
    'invest_cta': '"invest now" buttons', 'signup_country': 'sign-up asks for country', 'signup_phone': 'sign-up asks for phone',
    'signup_name': 'sign-up asks for name', 'signup_terms': 'sign-up terms box', 'signup_referral': 'sign-up referral field',
    'leverage_spreads': 'leverage/spreads wording', 'trading_platform': 'trading-platform wording', 'asset_classes': 'forex/CFD/stock wording',
    'language_selector': 'language selector', 'fiat_cards_wire': 'card/wire payments', 'analytics': 'ad analytics',
    'media_celebrity': 'celebrity/media names', 'iframe_form': 'a form in an iframe',
}

# Groups of features the rules look at.
ACCOUNT = ('crawl_logged_in', 'crawl_signup_form', 'signup_form', 'login_form', 'password_field_any', 'login_link', 'account_button')
CONTROLS = ACCOUNT + ('register_link',)
PASSWORD = ('crawl_logged_in', 'crawl_signup_form', 'signup_form', 'login_form', 'password_field_any')
# A real plan offer: features no other family uses this way. Any one of them keeps a site out of the
# hard-B rules, and a YES needs one (see classify).
# "principal" and "recent deposits/payouts" used to be here. On the Nov 1 blind run (confirmed-2026-10-30.csv
# rows 101-173) they were the only "offer" on prop firms with a "recent payouts" box (northpeakfunded,
# fxcapital3-network), on prop firms and blogs that say "principal" (fundingperpetuals, myfundedstep,
# howtomakemoneyonline5000) and on hash-rate sellers (ltcprime, quarkcoins): their NO rule never ran.
OFFER_STRONG = ('crawl_deposit_plans', 'plan_card_txt', 'signup_wallet_fields', 'goldcoders_hscript', 'viserlab_hyiplab')
# Plan names, min/max deposits, "% daily", "principal" and recent-payout tables also turn up on brokers,
# bots, subscriptions ("Premium plan") and prop firms ("5% daily drawdown"); they are listed as
# reasons but decide nothing on their own.
OFFER = OFFER_STRONG + ('plan_tiers', 'min_max_deposit', 'pct_per_period', 'principal_return', 'last_tx_table')
LEAD = ('crawl_lead_form', 'lead_form_phone')
ARTICLE = ('article_markers', 'review_language', 'article_markup')
OFFSITE = ('crawl_offsite_signup', 'register_offsite')
BACKOFFICE = {'crawl_bo_deposit': re.compile(r'deposit|add[- _]?funds?|top[- _]?up|recharge', re.I),
              'crawl_bo_withdraw': re.compile(r'withdraw|payout', re.I),
              'crawl_bo_referral': re.compile(r'referr?al|affiliate|downline|\bteam\b', re.I),
              # Nov 6: + "Fixed Deposit" and "Staking" (earn-way.com's and lidfxmarket.com's plan pages)
              'crawl_bo_plans': re.compile(r'\bplans?\b|invest(ment)?s?\b|packages?\b|fixed[- _]?deposits?\b|\bstak(e|ing)\b', re.I),
              'crawl_bo_challenge': re.compile(r'challenges?\b|evaluation', re.I)}
# Not scored: the crawler never sees them (detection-time IOC extraction, WHOIS age, hosting ISP,
# the plain-HTTP fetch of a JavaScript page).
NOT_SCORED = re.compile(r'^(ioc_|domain_age_|behind_cloudflare$|spa_or_thin_shell$)')


def _log_ratio(n, N, m, M):
    """log of the Laplace-smoothed rate n/N over m/M."""
    return math.log((n + 1) / (N + 2)) - math.log((m + 1) / (M + 2))


WEIGHTS = {k: _log_ratio(*r['H'], *r['O']) for k, r in RATES.items() if not NOT_SCORED.match(k)}
FAMILY_WEIGHTS = {fam: {k: _log_ratio(*r['fam'][fam], *r['O']) for k, r in RATES.items() if not NOT_SCORED.match(k)}
                  for fam in OTHER_FAMILIES}


def host_of(url):
    try:
        host = (urlparse(url if '://' in url else 'https://' + url).hostname or '').lower()
    except ValueError:  # e.g. a bracketed host that is not an IP address
        return ''
    return host[4:] if host.startswith('www.') else host


def _join(base, href):
    """urljoin that gives '' instead of raising. A page's links can hold text urllib refuses:
    a WordPress page on the 10-31 CSV links to "//[xoo_el_inline_form active=login]", and the
    ValueError ("does not appear to be an IPv4 or IPv6 address") lost the whole page (Nov 7)."""
    try:
        return urljoin(base, href)
    except ValueError:
        return ''


def _path(href):
    try:
        return urlparse(href or '').path
    except ValueError:
        return ''


class SiteEvidence:
    """What the crawler saw on one site: feature -> where it was first seen, plus the crawl's own findings."""

    def __init__(self, url):
        self.url = url
        self.domain = host_of(url)
        self.hosts = {self.domain}  # plus the host the start page redirects to
        self.features = {}
        self.onsite = set()       # features seen on the site's own pages (not after an off-site sign-up link)
        self.pages = []           # URLs read, in order
        self.read = 0             # pages that were the site (not a parked or bot-check page)
        self.title = ''
        self.unreachable = None   # the error, when the start page never loaded
        self.parked = None        # first placeholder/parked/bot-check page seen instead of the site
        self.stopped_early = None  # reasons, when the crawl stopped before signing up
        self.signup_fields = []   # hints of the chosen sign-up form's inputs
        self.deposit = []         # {'url', 'plan', 'payment'} per deposit page
        self.wallets = []
        self.wallet_methods = {}  # address -> the payment option chosen when it appeared
        self.backoffice = {}      # back-office feature -> link

    def add(self, feature, source, onsite=True):
        self.features.setdefault(feature, source)
        if onsite:
            self.onsite.add(feature)

    def has(self, *features):
        return any(k in self.features for k in features)

    def is_onsite(self, url):
        host = host_of(url)
        return any(host == h or host.endswith('.' + h) or h.endswith('.' + host) for h in self.hosts)

    def wants_page(self, url):
        return len(self.pages) < MAX_PAGES and url not in self.pages

    def add_page(self, raw_html, url, kind='page', count=True):
        """Features of one loaded page (the rendered page source). kind 'home' is the start page,
        'register' a page the crawler took for the sign-up page. count=False reads it even past
        MAX_PAGES (deposit pages and the page after login)."""
        # The sign-up page is read again as 'register' even if the search already read it: the search
        # reads a page as soon as it loads, before a late sign-up form is drawn (Nov 6).
        seen = url in self.pages
        if (seen and kind != 'register') or (not seen and count and not self.wants_page(url)):
            return
        if not seen:
            self.pages.append(url)
        try:
            soup = BeautifulSoup(raw_html, 'html.parser')
        except Exception:
            return
        title = soup.title.get_text(strip=True) if soup.title else ''
        srcs = ' '.join(s.get('src', '') for s in soup.find_all('script')).lower()
        for t in soup(['script', 'style', 'noscript']):
            t.decompose()
        text = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True))
        if PARKED.search(title + ' ' + text[:3000]):
            self.parked = self.parked or url
            return
        if kind == 'home':
            self.hosts.add(host_of(url))  # the start page may have redirected to another domain
        if not seen:
            self.read += 1
        if not self.title:
            self.title = title
            for k in title_feats(title, self.domain):
                self.add(k, 'title')
        onsite = self.is_onsite(url)
        src = kind if kind in ('home', 'register') else (urlparse(url).path or '/')
        if '?' in url and kind not in ('home', 'register'):
            src += '?' + urlparse(url).query
        src = (src if onsite else host_of(url) + src)[:60]
        found = set()
        for k, rx in TEXT.items():
            if rx.search(text):
                found.add(k)
        inline = raw_html.lower()
        for k, pat in JS.items():
            if re.search(pat, srcs) or (k in INLINE_JS and re.search(pat, inline)):
                found.add(k)
        for feats in (form_feats(soup, kind), tech(raw_html), button_feats(soup), offer_feats(text), rule_feats(text)):
            found.update(feats)
        page_host = host_of(url)
        hrefs = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            hrefs.append(href)
            t = a.get_text(' ', strip=True) + ' ' + href
            if LOGIN_RE.search(t):
                found.add('login_link')
            if REG_RE.search(t):
                found.add('register_link')
                h = host_of(_join(url, href)) if href.startswith(('http', '//')) else page_host
                if (h and h != page_host and not h.endswith('.' + self.domain) and not page_host.endswith('.' + h)
                        and not re.search(r'google|apple|facebook|t\.me|whatsapp|twitter|instagram', h)):
                    found.add('register_offsite')
            if CONTACT_RE.search(t):
                found.add('contact_link')
            if href.startswith('mailto:'):
                found.add('email_shown')
            if href.startswith('tel:'):
                found.add('phone_shown')
        for k, pat in SOCIAL.items():
            if any(re.search(pat, h, re.I) for h in hrefs):
                found.add(k)
        if kind == 'home':
            ext = {host_of(_join(url, h)) for h in hrefs if h.startswith(('http', '//'))}
            if len({h for h in ext if h and h != page_host and self.domain not in h}) >= 15:
                found.add('many_external_links')
            if soup.find('article') or soup.find(attrs={'itemprop': 'author'}) or soup.find('time'):
                found.add('article_markup')
        for k in sorted(found):
            self.add(k, src, onsite)

    def add_signup_form(self, fields, url):
        """The chosen sign-up form's inputs, as FIELD_HINTS_JS reads them: [name, id, type, hints, required]."""
        onsite = self.is_onsite(url)
        self.add('crawl_signup_form', url, onsite)
        self.signup_fields = [[f[0] or f[1], f[2], f[3][:80]] for f in fields][:30]
        hints = ' | '.join(f[3] for f in fields)
        for k, rx in (('signup_wallet_fields', r'\b(btc|bitcoin|usdt|eth|trx|tron|ltc|perfect ?money|payeer|wallet)\b'),
                      ('signup_phone', r'phone|mobile|whatsapp|\btel\b'), ('signup_country', r'country|nationality'),
                      ('signup_referral', r'referr?al|upline|sponsor|invite|ref_?code'), ('signup_secret_q', r'secret|question|answer|\bpin\b')):
            if re.search(rx, hints):
                self.add(k, url, onsite)

    def add_deposit(self, url, plan, payment):
        """A deposit page's form as DEPOSIT_FORM_JS describes it (plan and payment controls, or None)."""
        self.add('crawl_deposit_form', url, self.is_onsite(url))
        if plan:
            self.add('crawl_deposit_plans', url, self.is_onsite(url))
        self.deposit.append({'url': url, 'plan': [o['label'] for o in plan['options']] if plan else [],
                             'payment': [o['label'] for o in payment['options']] if payment else []})

    def add_wallets(self, wallets, url, method=None):
        """Wallet-like strings found on url.

        method is the payment option the crawler had selected when they appeared -- "Bitcoin",
        "USDT / TRC20", "Tether (ERC20)" and so on. It is the page's own statement of what to
        send, and it is the only reliable way to tell USDT from ETH on an 0x... address or TRX
        from USDT-TRC20 on a Tron one: the address format cannot. build_wallets.py prefers it
        over the text it scrapes from around the address, which on a logged-in dashboard is
        usually the sidebar nav rather than a coin name.

        Recorded per address in wallet_methods. self.wallets stays a plain list of addresses so
        everything that already reads it is unaffected.
        """
        label = (method or '').strip()
        # 'site default' means no payment control was chosen, so it names no coin.
        if label.lower() in ('site default', 'none'):
            label = ''
        for w in wallets:
            if w not in self.wallets:
                self.wallets.append(w)
            # First non-empty label wins: it belongs to the option that was selected when the
            # address first showed up. Later passes on the same page choose OTHER options, and
            # the address usually changes with them -- but when a site reuses one address for
            # every coin, the first label is the one that was actually on screen with it.
            if label and not self.wallet_methods.get(w):
                self.wallet_methods[w] = label[:60]
        if wallets:
            self.add('crawl_wallets', url, self.is_onsite(url))

    def add_backoffice(self, links, url):
        """Links of the page after login (url): [(href, text)]. Records which back-office pages exist."""
        for href, text in links:
            for k, rx in BACKOFFICE.items():
                if k not in self.backoffice and (rx.search(text or '') or rx.search(_path(href))):
                    self.backoffice[k] = href
                    self.add(k, href, self.is_onsite(url))

    def to_json(self, verdict):
        return {'url': self.url, 'domain': self.domain, 'verdict': verdict, 'title': self.title,
                'pages_read': self.pages, 'unreachable': self.unreachable, 'parked': self.parked,
                'stopped_early': self.stopped_early, 'features': self.features,
                'offsite_features': sorted(set(self.features) - self.onsite), 'signup_fields': self.signup_fields,
                'deposit_pages': self.deposit, 'wallets': self.wallets,
                # address -> the payment option selected when it appeared; build_wallets.py reads
                # this to name the coin/network instead of guessing from the surrounding text.
                'wallet_methods': self.wallet_methods, 'backoffice': self.backoffice}

# ---- The decision -------------------------------------------------------------------------------


def _why(ev, *features, limit=4):
    out = []
    for k in features:
        if k in ev.features and len(out) < limit:
            out.append(f"{LABELS.get(k, k.replace('_', ' '))} ({ev.features[k]})")
    return out


def _best_family(ev):
    scores = {fam: sum(w.get(k, 0) for k in ev.features) for fam, w in FAMILY_WEIGHTS.items()}
    return max(scores, key=scores.get)


def hard_b(ev, whole_site=True):
    """(family, reasons) when the site shows another scam's sign, else None (Nov 7 exclusions).

    Measured on James's labels across all CSVs, each of these hits at most ~1% of his HYIPs (prop and
    simulation hit none on his current labels). The older rules that also rejected HYIPs were dropped:
    hash-rate offers (7% of his HYIPs; alnoortrade, jetwayassetminers, cryptovista), off-site sign-up
    (investorassetmanagement, whiteoakcapital), monthly subscription pricing, "no controls", and the
    loose prop_challenge / title_official_country features (expertoptionplus's "properly funded account",
    any title with a year or a trademark sign). whole_site is kept for the callers; every exclusion is
    a positive sign, so it counts as soon as it is seen."""
    if ev.has(*LEAD) and not ev.has(*PASSWORD):
        return 'call-center', (['a name/e-mail + phone form and no password field on any page read']
                               + _why(ev, *LEAD, 'lead_form_first_last', 'intl_tel_input', 'txt_callback'))
    for feature, family, why in (('excl_official_site', 'call-center', '"official site/website" in the title (call-center template)'),
                                 ('excl_prop', 'other investment', 'sells prop-firm challenges / funded accounts'),
                                 ('excl_simulation', 'other investment', 'simulated trading with virtual money'),
                                 ('excl_faucet', 'mining/generator', 'faucet / crypto generator'),
                                 ('excl_crypto_loan', 'other investment', 'crypto-backed loans'),
                                 ('excl_review_title', 'news/review funnel', 'review / news site title')):
        if ev.has(feature):
            return family, [why] + _why(ev, feature)
    return None


def clear_b(ev):
    """Reasons to stop before signing up (an exclusion on the pages read so far), else None."""
    found = hard_b(ev, whole_site=False)
    return found[1] if found else None


def score(ev):
    """Log-likelihood ratio HYIP : other over the features present, and the per-feature terms (reported only)."""
    terms = [(k, WEIGHTS[k]) for k in ev.features if k in WEIGHTS]
    return sum(w for _, w in terms), terms


SIGNUP_FORM = PASSWORD                      # a real sign-up/login form with a password, or the crawler logged in
DEPOSIT_WORDS = ('deposit_word', 'deposit_withdraw_ui', 'amount_form')
WITHDRAW_WORDS = ('withdraw_word', 'deposit_withdraw_ui', 'instant_withdraw')
MONEY_IN = ('crawl_deposit_form', 'crawl_bo_deposit', 'crawl_wallets')
PLAN_OFFER = ('pct_per_period', 'plan_card_txt', 'pct_return_any', 'plan_duration_money', 'flow_steps', 'high_return')


def properties(ev):
    """The three HYIP properties: (sign-up form, money goes in, plan offer)."""
    return (ev.has(*SIGNUP_FORM),
            (ev.has(*DEPOSIT_WORDS) and ev.has(*WITHDRAW_WORDS)) or ev.has(*MONEY_IN),
            ev.has(*PLAN_OFFER))


def classify(ev):
    """{'verdict': 'A' | 'B' | 'none', 'family', 'family_name', 'step', 'score', 'reasons'}.

    Nov 7: another scam's sign -> B; all three HYIP properties (a real sign-up form, deposit and
    withdraw wording or a deposit page, a plan offer) -> A; anything else -> B."""
    total, terms = score(ev)

    def result(verdict, family, step, reasons):
        return {'verdict': verdict, 'family': family, 'family_name': FAMILY_NAMES.get(family, family),
                'step': step, 'score': round(total, 2), 'reasons': reasons}

    if not ev.read:
        why = (f"start page did not load: {ev.unreachable}" if ev.unreachable
               else f"only a parked or bot-check page was seen ({ev.parked})" if ev.parked else "no page was read")
        return result('none', '', 'unreachable', [why])
    found = hard_b(ev)
    if found:
        return result('B', found[0], 'exclusion', found[1])
    form, money, offer = properties(ev)
    if form and money and offer:
        return result('A', 'HYIP', 'properties', ['sign-up form, deposit and withdraw wording, and a plan offer']
                      + _why(ev, *SIGNUP_FORM, limit=1) + _why(ev, *MONEY_IN, *DEPOSIT_WORDS, limit=1)
                      + _why(ev, *PLAN_OFFER, limit=2))
    missing = [name for name, ok in (('a sign-up form', form), ('deposit and withdraw wording', money), ('a plan offer', offer)) if not ok]
    return result('B', _best_family(ev), 'properties', [f"HYIP properties not all seen: missing {', '.join(missing)}"]
                  + _why(ev, *SIGNUP_FORM, *MONEY_IN, *DEPOSIT_WORDS, *PLAN_OFFER, limit=3))


def verdict_line(v):
    """The log line run_regression.py reads."""
    reasons = '; '.join(v['reasons'])
    if v['verdict'] == 'A':
        return f"HYIP verdict: A (HYIP) | family HYIP | reasons: {reasons}"
    if v['verdict'] == 'B':
        return f"HYIP verdict: B (other scam: {v['family_name']}) | reasons: {reasons}"
    return f"HYIP verdict: none (unreachable) | reasons: {reasons}"
