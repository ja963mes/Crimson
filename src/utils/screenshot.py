import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

class SeleniumScreenshot:
    def __init__(self):
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--incognito")
        # Match is_domain_available()'s verify=False: newly-registered scam hosts
        # routinely have self-signed or expired certs, and Chrome's interstitial
        # would otherwise be what gets screenshotted instead of the page.
        chrome_options.add_argument("--ignore-certificate-errors")
        # Headless Chrome advertises "HeadlessChrome/..." in its User-Agent, which
        # the same Cloudflare rules that block python-requests also reject. Match
        # the header recv.py sends so both fetch paths get the same page.
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        chrome_options.add_argument('--remote-debugging-pipe')
        chrome_options.add_argument("--window-size=1024,768")
        chrome_options.binary_location = '/usr/bin/chromium-browser'
        self.options = chrome_options

    def take_screenshot(self, url, curr_date, path, SYSNO):
        # Both attempts used to load the identical http:// URL, so a site that
        # only serves HTTPS failed twice for the same reason. Try each scheme
        # once instead -- HTTPS first, matching is_domain_available().
        for scheme in ('https://', 'http://'):
            if self.screenshot_retrier(url, curr_date, path, SYSNO, scheme):
                return True
        time.sleep(0.5)
        return False

    def screenshot_retrier(self, url, curr_date,  path, SYSNO, scheme="https://"):
        service = Service(executable_path=r'/usr/bin/chromedriver')
        browser = None
        try:
            service.start()
            browser = webdriver.Chrome(options=self.options, service=service)
            browser.set_page_load_timeout(20)
            browser.get(scheme + url)
            WebDriverWait(browser, 20).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
            browser.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            browser.execute_script("window.scrollTo(0, 0);")
            S = lambda X: browser.execute_script('return document.body.parentNode.scroll' + X)
            browser.set_window_size(S('Width'), S('Height'))
            browser.find_element(By.TAG_NAME, 'body').screenshot(path + '/full_page.png')
            return True
        except Exception as e:
            return False
        finally:
            if browser:
                browser.quit()
            service.stop()
