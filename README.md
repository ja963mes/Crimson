# Crimson — Scripts

## `src/`

### `cron.py`
Supervises the certstream-server process. Polls its status every 15 seconds and restarts it if it has died, logging to `../server-logs/server.log`.

### `listen.py`
Connects to the external CertStream WebSocket (`CERTSTREAM_URL`, `ws://130.245.32.192:8080/` by default) and publishes each raw certificate-update message to the `urls` RabbitMQ queue. Runs on the broker host (130.245.32.119) alongside `send.py`.

### `send.py`
The URL filter. Consumes `urls`, pulls every domain out of each certificate, and keeps only those whose name matches the crypto keyword list via `keyword_utils`. Survivors are de-duplicated through a 50,000-entry LRU cache and published to `incubation_queue`, which holds each domain for 12 hours before dead-lettering it into `cryptoscams`. Logs which domains passed, were deduped, and were sent.

### `recv.py`
The worker. Consumes `cryptoscams` and, for each domain: fetches the site (HTTPS first, browser User-Agent, certificate errors ignored), extracts and sanitizes the page text, then applies the content filter, which requires investment words, coin words, and context words to all be present. An HYIP phrase (such as "investment plans" or "2% daily") can stand in for the investment word. Pages with exclusion words that mark another kind of site (token presales and airdrops, parked domains, QFS pages, or two different gambling words) are dropped unless they also contain an HYIP phrase, and each is logged to `content_excluded.txt`. Pages with under 150 characters of DOM text fall back to a Selenium screenshot plus Tesseract OCR, and the content filter then runs on the OCR text and DOM text combined, as in the published artifact. Survivors go to `evaluate_hybrid()`, which asks the local Ollama model first and escalates to OpenAI only when the local verdict is `unsure` or below the confidence bar. Both prompts target HYIP (high-yield investment program) sites only; other crypto scams such as drainers, airdrops and meme coins count as "no". Domains confirmed above the bar are enriched with WHOIS, IP and IOC data, then rsynced to `collected_results/` on the broker host (`QUEUE_IP`, 130.245.32.119). Takes `SYSNO` and `SCREENNO` as command-line arguments.

### `validate.py`
Classifies OCR-extracted text as HYIP or not by piping it to a local LLM through a subprocess, using the same HYIP-only prompt as `recv.py`. Validates that the model's JSON reply contains `answer` and `reason` keys, and retries up to five times with a correction prompt appended when the format is wrong.

### `test_pipeline.py`
Pulls three domains out of `incubation_queue` and republishes them directly into `cryptoscams`, bypassing the 12-hour incubation wait so the workers can be exercised immediately.

## `src/utils/`

### `keyword_utils.py`
The URL filter's matching logic. Splits a domain into component words with wordninja using the merged crypto word model, then tests those tokens against the crypto keyword set — exact match for short keywords, prefix match for keywords of six characters or more. Also holds the domain whitelist that exempts known-good hosts.

### `screenshot.py`
Headless Chrome full-page screenshot helper used by `recv.py`. Ignores certificate errors, sends a real Chrome User-Agent, and tries HTTPS before falling back to HTTP.

## `src/authentication_crawling/`

### `crawler_script.py`
Browser automation that registers accounts on confirmed scam sites. Generates random names, emails, and fake wallet addresses for each supported coin; predicts and fills sign-up form fields; handles checkboxes, dropdowns, alerts, and CAPTCHA detection; logs in; screenshots the authenticated pages; and follows up to 25 internal links per site.

### `feeder.py`
Bridges detection to crawling. Reads confirmed-scam records from the results logs, normalizes each `url` field into a full URL, de-duplicates against domains already handed to the crawler, and appends the new ones to `urls.txt`. Appends only — never rewrites or reorders — so the crawler's single-URL checkpoint stays valid.

### `iocs_searcher.sh`
Runs `iocsearcher` across every HTML file in a given directory, extracting 26 indicator types including cryptocurrency addresses, onion addresses, emails, phone numbers, and social media handles. Keeps a log of processed files so nothing is scanned twice.
