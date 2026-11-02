"""Run on VM1 in ~/hyip-feature-research after fetch_pages.py.
Per domain: features from (a) detection-time OCR text + js libs + IOCs (records.json)
and (b) live HTML (html/<domain>/*.html), only when the live page is still the scam site.
Writes features.json: {domain: {group, family, live_ok, f: {feature: source-string}}}
"""
import json, os, re, glob
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

os.chdir(os.path.expanduser('~/hyip-feature-research'))
labels = {r['domain']: r for r in json.load(open('labels.json'))}
recs = json.load(open('records.json'))
fetch = {r['domain']: r for r in json.load(open('fetch_index.json'))}

FAMILY = [
    ('broker/trading', r'BROKER|PROP SCAM|TRADING APP|TRADING BOT|BOT SCAM|AUTO INVESTMENT|INVESTMENT APP|EXCHANGE RATE|WEALTH MANAGEMENT|BANK'),
    ('call-center', r'CALL'),
    ('MLM', r'MLM|LEADER'),
    ('news/review funnel', r'NEWS SCAM|REVIERW SCAM|REVIEW SCAM|ARTICLE|INFORMATION SCAM|WEBINAR|WORKSHOP'),
    ('mining/generator', r'MINER|GENERATOR|EARN CRYPTO|BUY CRYPTO'),
    ('malware', r'MALWARE'),
    ('other investment', r'INVESTMENT|INVEST INTO|REAL ESTATE|CAR SCAM'),
]

def family(g):
    if g == 'HYIP':
        return 'HYIP'
    if not g.startswith('OTHER:'):
        return g.split(':')[0]
    sub = g[6:]
    for name, pat in FAMILY:
        if re.search(pat, sub):
            return name
    return 'not-scam/out-of-scope'  # review site, link directories, news, software, game, legal, ...

PARKED = re.compile(r'domain (is )?for sale|buy this domain|this domain (may be|is) (for sale|parked)|parkingcrew|sedo|dan\.com|afternic|'
                    r'account (has been )?suspended|website (is )?(expired|suspended)|hostinger.*(parked|coming soon)|'
                    r'default web page|it works!|welcome to nginx|index of /|domain has expired|this site can.t be reached|'
                    r'just a moment\.\.\.|attention required! \| cloudflare|checking your browser|enable javascript and cookies', re.I)

def W(p):  # word-ish regex helper
    return re.compile(p, re.I)

# text features: applied to OCR text and live visible text
TEXT = {
    # account / onboarding
    'txt_login': W(r'\b(log ?in|sign ?in)\b'),
    'txt_register': W(r'\b(sign ?up|register|create (an |your )?account|open (an |your )?account|join (us|now))\b'),
    'txt_callback': W(r'request (a )?call|call ?back|we will call you|(manager|specialist|agent|representative|advisor|expert|consultant)s? will (contact|call|reach)|get a (free )?call|leave (your|a) (phone|number|request)|schedule a call|book a (call|consultation)|free consultation'),
    'txt_personal_manager': W(r'personal (account )?manager|dedicated (account )?manager|personal (advisor|adviser|broker|analyst)|account manager'),
    'txt_kyc': W(r'\bkyc\b|verify your identity|identity verification|upload (your )?(id|passport|document)'),
    # investment offer
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
    # trading / broker
    'trading_platform': W(r'metatrader|\bmt[45]\b|webtrader|web trader|trading platform|trading terminal|trade now'),
    'leverage_spreads': W(r'leverage|spreads?\b|\bpips?\b|lot size|margin (call|trading)|stop loss|take profit'),
    'asset_classes': W(r'\bforex\b|\bcfds?\b|\bstocks?\b|indices|commodities|\boil\b|\bgold\b.{0,20}(trading|price)|\betfs?\b|options trading'),
    'account_types': W(r'(standard|ecn|pro|micro|islamic|demo|vip|classic|premium|mini) accounts?|account types'),
    'demo_account': W(r'demo account|free demo|practice account'),
    'risk_warning': W(r'risk warning|risk disclosure|cfds are complex|lose money|high (level of )?risk|past performance'),
    'copy_ai_bot': W(r'copy[- ]trading|trading signals?|trading bot|\bai\b.{0,15}(trading|bot|powered|algorithm)|algorithmic|auto[- ]?trad'),
    # funnel / news / review
    'article_markers': W(r'published (on|by)|updated on|posted (on|by)|share this (article|post)|leave a (comment|reply)|min(ute)? read|editor.s (note|pick)'),
    'invest_cta': W(r'invest now|make (a )?deposit|deposit now|start investing|choose (a |your )?plan|get started now'),
    'deposit_withdraw_ui': W(r'\b(deposit|withdraw)\b.{0,40}\b(withdraw|deposit)\b'),
    'media_celebrity': W(r'elon musk|bill gates|jeff bezos|\bbbc\b|\bcnn\b|dragons.? den|shark tank|as seen on|featured (in|on)|forbes|bloomberg|reuters|yahoo finance|the guardian'),
    'review_language': W(r'\breview(s|ed)?\b.{0,40}(scam|legit)|is it (a )?(scam|legit)|pros and cons|our verdict|\brating\b|trustpilot'),
    'urgency': W(r'limited (time|spots|places|offer)|only \d+ (spots|places|slots)|offer (ends|expires)|hurry|don.t miss|last chance|spots? (left|remaining)'),
    # mining / generator / token
    'cloud_mining': W(r'cloud mining|hash ?rate|\b[tgpe]h/s\b|mining (contract|plan|pool|farm|power|rig)|\basics?\b|\bminers?\b'),
    'generator_doubler': W(r'generator|private key|double your|\bx2\b|send .{0,30}receive|giveaway|airdrop|claim (your|free)'),
    'token_ico': W(r'whitepaper|white paper|tokenomics|presale|pre-sale|\bico\b|buy (the )?token|token sale|staking'),
    # malware / software / app
    'download_txt': W(r'download (for|on|the|our)? ?(windows|mac|app|apk|now|wallet|extension|software|client)|install (the )?(app|extension)|available on (the )?(app store|google play)'),
    # legal / content sections
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

def text_of(html):
    try:
        s = BeautifulSoup(html, 'html.parser')
    except Exception:
        return None, ''
    for t in s(['script', 'style', 'noscript']):
        t.decompose()
    return s, re.sub(r'\s+', ' ', s.get_text(' ', strip=True))

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

out = {}
for d, lab in labels.items():
    g = lab['group']
    fam = family(g)
    if fam in ('SKIP',):
        continue
    if g.split(':')[0] not in ('HYIP', 'OTHER', 'NO-PLAIN', 'NO-AMBIG'):
        continue
    feats = {}
    def add(k, src):
        if src not in feats.get(k, ''):
            feats[k] = feats.get(k, '') + src
    rec = recs.get(d, {})
    ocr = (rec.get('title') or '') + ' ' + (rec.get('text') or '')
    for k, rx in TEXT.items():
        if rx.search(ocr):
            add(k, 'O')
    js = ' '.join(rec.get('js_libraries') or []).lower()
    for k, pat in JS.items():
        if re.search(pat, js):
            add(k, 'J')
    ioc = rec.get('ioc') or {}
    for k in ioc:
        add('ioc_' + k, 'O')
    # live html
    live_ok = False
    fi = fetch.get(d, {})
    home = f'html/{d}/home.html'
    if fi.get('home_status') and fi['home_status'] < 400 and os.path.exists(home):
        raw_home = open(home, errors='replace').read()
        soup_h, txt_h = text_of(raw_home)
        title_h = (soup_h.title.get_text(strip=True) if soup_h and soup_h.title else '')
        final_host = urlparse(fi.get('home_final') or '').netloc.lower()
        moved = final_host and d not in final_host and final_host not in d
        if soup_h is not None and not PARKED.search(title_h + ' ' + txt_h[:3000]) and not moved:
            live_ok = True
            if len(txt_h) < 400:
                add('spa_or_thin_shell', 'L')
            for p in glob.glob(f'html/{d}/*.html'):
                kind = os.path.basename(p)[:-5]
                raw = open(p, errors='replace').read()
                soup, txt = text_of(raw)
                if soup is None:
                    continue
                for k, rx in TEXT.items():
                    if rx.search(txt):
                        add(k, 'L')
                srcs = ' '.join(s.get('src', '') for s in BeautifulSoup(raw, 'html.parser').find_all('script')).lower()
                inline = raw.lower()
                for k, pat in JS.items():
                    if re.search(pat, srcs) or (k in ('chat_widget', 'translate_widget', 'tradingview_widget', 'crypto_ticker_widget', 'recaptcha') and re.search(pat, inline)):
                        add(k, 'L')
                for k in form_feats(soup, kind):
                    add(k, 'L')
                for k in tech(raw):
                    add(k, 'L')
                hrefs = [a.get('href', '') for a in soup.find_all('a', href=True)]
                for a in soup.find_all('a', href=True):
                    t = a.get_text(' ', strip=True) + ' ' + a['href']
                    if LOGIN_RE.search(t):
                        add('login_link', 'L')
                    if REG_RE.search(t):
                        add('register_link', 'L')
                        h = urlparse(urljoin(fi['home_final'], a['href'])).netloc.lower()
                        if h and h != final_host and not h.endswith('.' + d) and not re.search(r'google|apple|facebook|t\.me|whatsapp|twitter|instagram', h):
                            add('register_offsite', 'L')
                    if CONTACT_RE.search(t):
                        add('contact_link', 'L')
                    if a['href'].startswith('mailto:'):
                        add('email_shown', 'L')
                    if a['href'].startswith('tel:'):
                        add('phone_shown', 'L')
                for k, pat in SOCIAL.items():
                    if any(re.search(pat, h, re.I) for h in hrefs):
                        add(k, 'L')
                if kind == 'home':
                    ext = {urlparse(urljoin(fi['home_final'], h)).netloc.lower() for h in hrefs}
                    ext = {h for h in ext if h and h != final_host and d not in h}
                    if len(ext) >= 15:
                        add('many_external_links', 'L')
                    if soup.find('article') or soup.find(attrs={'itemprop': 'author'}) or soup.find('time'):
                        add('article_markup', 'L')
            for pg in fi.get('pages', []):
                if pg['kind'] == 'register' and pg.get('offsite'):
                    add('register_offsite', 'L')
    if re.search(r'\+?\d[\d\s().-]{8,}\d', (rec.get('text') or '')) and 'phoneNumber' in ioc:
        add('phone_shown', 'O')
    t = rec.get('title') or lab.get('title') or ''
    if re.search(r'official (web)?site|in (canada|australia|uk|united kingdom|south africa|india|nigeria|kenya|ireland|brazil|new zealand|singapore|germany|italy|spain|mexico)', t, re.I):
        add('title_official_country', 'T')
    if re.search(r'20(25|26)', t):
        add('title_year', 'T')
    if re.search('[ᐉ®™]', t):
        add('title_glyph', 'T')
    if re.search(r'\b(ai|bot|algorithm|automated)\b', t, re.I):
        add('title_ai_bot', 'T')
    if re.search(r'\b(ltd|limited|llc|inc|corp|plc|group|holdings?)\b', t + ' ' + re.sub(r'[-.]', ' ', d), re.I):
        add('name_corporate_suffix', 'T')
    if re.search(r'capital|invest|trade|asset|financ|fund|wealth|profit|crypto|coin|mining|bit', d):
        add('domain_finance_word', 'T')
    if '.pages.dev' in d:
        add('host_pages_dev', 'T')
    tld = d.rsplit('.', 1)[-1]
    add('tld_com' if tld == 'com' else 'tld_not_com', 'T')
    add('tld_' + tld, 'T')
    try:
        cd = int(rec.get('creation_date') or 0)
        if cd:
            age_days = (1792000000 - cd) / 86400  # 1792000000 = Oct 14 2026, mid-collection
            add('domain_age_lt_90d' if age_days < 90 else ('domain_age_90d_1y' if age_days < 365 else 'domain_age_gt_1y'), 'T')
    except Exception:
        pass
    if 'cloudflare' in ((rec.get('ip_info') or {}).get('isp') or '').lower():
        add('behind_cloudflare', 'T')
    out[d] = {'group': g, 'family': fam, 'live_ok': live_ok, 'title': rec.get('title') or lab.get('title'),
              'created': rec.get('creation_date'), 'country': (rec.get('ip_info') or {}).get('countryCode'),
              'isp': (rec.get('ip_info') or {}).get('isp'), 'f': feats}

json.dump(out, open('features.json', 'w'))
import collections
c = collections.Counter((v['family'], v['live_ok']) for v in out.values())
for k, n in sorted(c.items()):
    print(k, n)
