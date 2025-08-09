import os
import random
import time
import re
from typing import Optional, List, Tuple, Set

from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, ElementClickInterceptedException, WebDriverException
)
from fake_useragent import UserAgent

# --- opsional validasi proxy gratis ---
import requests

URL = "https://www.gas.zip/faucet/fogo"

load_dotenv()

HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
CLAIM_ATTEMPTS = int(os.getenv("CLAIM_ATTEMPTS", "1"))
PAUSE_MIN_SEC = int(os.getenv("PAUSE_MIN_SEC", "5"))
PAUSE_MAX_SEC = int(os.getenv("PAUSE_MAX_SEC", "20"))
RETRY_PER_ATTEMPT = int(os.getenv("RETRY_PER_ATTEMPT", "3"))
PROXY_ROTATION = os.getenv("PROXY_ROTATION", "roundrobin").lower()  # roundrobin|random

USE_FREE_PROXIES = os.getenv("USE_FREE_PROXIES", "false").lower() == "true"
FREE_PROXY_VALIDATE = os.getenv("FREE_PROXY_VALIDATE", "false").lower() == "true"
FREE_PROXY_TIMEOUT = int(os.getenv("FREE_PROXY_TIMEOUT", "5"))

# ----------------- Utils: file loaders -----------------
def load_addresses(path: str = "address.txt") -> List[str]:
    if not os.path.exists(path):
        raise SystemExit(f"❌ File {path} tidak ditemukan.")
    with open(path, "r", encoding="utf-8") as f:
        addrs = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not addrs:
        raise SystemExit("❌ address.txt kosong.")
    return addrs

def load_proxies_file(path: str = "proxies.txt") -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        items = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    return items

# ----------------- Free proxy fetchers -----------------
FREE_PROXY_SOURCES = [
    # plaintext lists
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    # simple API
    "https://www.proxy-list.download/api/v1/get?type=http",
    "https://www.proxy-list.download/api/v1/get?type=https",
]

PROXY_REGEX = re.compile(r"^(?:(http|https|socks5)://)?([\d]{1,3}(?:\.[\d]{1,3}){3}):(\d{2,5})$")

def normalize_proxy(p: str) -> Optional[str]:
    p = p.strip()
    if not p:
        return None
    m = PROXY_REGEX.match(p)
    if m:
        scheme = m.group(1) or "http"
        hostport = f"{m.group(2)}:{m.group(3)}"
        return f"{scheme}://{hostport}"
    # try to accept bare host:port
    if re.match(r"^[\d]{1,3}(?:\.[\d]{1,3}){3}:\d{2,5}$", p):
        return f"http://{p}"
    # pass-through for entries that already have auth or socks5 creds
    if "@" in p and "://" in p:
        return p
    return None

def fetch_free_proxies() -> List[str]:
    out: Set[str] = set()
    for src in FREE_PROXY_SOURCES:
        try:
            r = requests.get(src, timeout=10)
            if r.ok:
                for line in r.text.splitlines():
                    norm = normalize_proxy(line)
                    if norm:
                        out.add(norm)
        except Exception:
            continue
    return list(out)

def validate_proxy_quick(p: str, timeout: int = 5) -> bool:
    try:
        r = requests.get("http://httpbin.org/ip", proxies={"http": p, "https": p}, timeout=timeout)
        return r.ok
    except Exception:
        return False

def build_proxy_pool() -> List[str]:
    pool = []
    # 1) from file
    file_proxies = [normalize_proxy(p) for p in load_proxies_file()]
    file_proxies = [p for p in file_proxies if p]
    pool.extend(file_proxies)

    # 2) from free sources
    if USE_FREE_PROXIES:
        print("🌐 Mengambil proxy gratis...")
        free_list = fetch_free_proxies()
        if FREE_PROXY_VALIDATE:
            print("🧪 Validasi cepat proxy gratis (ini bisa agak lama, bisa dimatikan dengan FREE_PROXY_VALIDATE=false)...")
            valid = []
            for p in free_list:
                if validate_proxy_quick(p, FREE_PROXY_TIMEOUT):
                    valid.append(p)
            print(f"✅ Proxy gratis valid: {len(valid)} / {len(free_list)}")
            pool.extend(valid)
        else:
            pool.extend(free_list)

    # dedupe sambil preserve order
    seen = set()
    unique = []
    for p in pool:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique

# ----------------- Selenium bits -----------------
def pick_proxy(pool: List[str], attempt_idx: int) -> Optional[str]:
    if not pool:
        return None
    if PROXY_ROTATION == "random":
        return random.choice(pool)
    # round robin default
    return pool[attempt_idx % len(pool)]

def new_driver(proxy: Optional[str]) -> uc.Chrome:
    ua = UserAgent()
    user_agent = ua.random

    options = uc.ChromeOptions()
    options.add_argument(f"--user-agent={user_agent}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--lang=en-US,en")
    if HEADLESS:
        options.add_argument("--headless=new")
    if proxy:
        # Chrom(e|ium) expects scheme://host:port
        options.add_argument(f"--proxy-server={proxy}")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    return uc.Chrome(options=options)

def page_has_captcha(driver) -> bool:
    html = driver.page_source.lower()
    indicators = ["hcaptcha", "captcha", "cf-turnstile", "turnstile"]
    return any(k in html for k in indicators)

def find_address_input(driver):
    candidates = [
        (By.CSS_SELECTOR, "input[placeholder*='address' i]"),
        (By.CSS_SELECTOR, "input[placeholder*='wallet' i]"),
        (By.CSS_SELECTOR, "input[type='text']"),
        (By.XPATH, "//input[contains(translate(@placeholder,'ADDRESS','address'),'address')]"),
        (By.XPATH, "//input[contains(translate(@placeholder,'WALLET','wallet'),'wallet')]"),
    ]
    for by, sel in candidates:
        try:
            el = WebDriverWait(driver, 6).until(EC.presence_of_element_located((by, sel)))
            if el.is_enabled():
                return el
        except TimeoutException:
            pass
    # fallback: cari input setelah label berteks 'Address'/'Wallet'
    try:
        label = driver.find_element(
            By.XPATH,
            "//*[contains(translate(text(),'ADDRESS','address'),'address') or contains(translate(text(),'WALLET','wallet'),'wallet')]"
        )
        input_el = label.find_element(By.XPATH, ".//following::input[1]")
        return input_el
    except Exception:
        return None

def find_claim_button(driver):
    candidates = [
        (By.XPATH, "//button[contains(translate(.,'CLAIM','claim'),'claim')]"),
        (By.CSS_SELECTOR, "button[type='submit']"),
        (By.XPATH, "//div[descendant::button][contains(translate(.,'CLAIM','claim'),'claim')]//button"),
    ]
    for by, sel in candidates:
        try:
            btn = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((by, sel)))
            return btn
        except TimeoutException:
            pass
    return None

def do_single_claim(driver, wallet_address: str) -> bool:
    driver.get(URL)

    # tunggu halaman siap
    try:
        WebDriverWait(driver, 25).until(
            EC.any_of(
                EC.presence_of_element_located((By.TAG_NAME, "input")),
                EC.presence_of_element_located((By.TAG_NAME, "button"))
            )
        )
    except TimeoutException:
        print("⏳ Halaman lambat / elemen tidak muncul.")
        return False

    if page_has_captcha(driver):
        print("🧩 CAPTCHA terdeteksi. Stop (tidak bypass).")
        return False

    # isi address
    inp = find_address_input(driver)
    if not inp:
        print("❌ Input address tidak ditemukan.")
        return False
    inp.clear()
    time.sleep(0.2)
    inp.send_keys(wallet_address)
    time.sleep(0.4)
    inp.send_keys(Keys.TAB)

    # klik tombol claim
    btn = find_claim_button(driver)
    if not btn:
        print("❌ Tombol claim tidak ditemukan.")
        return False
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.2 + random.random() * 0.4)
        btn.click()
    except (ElementClickInterceptedException, WebDriverException):
        try:
            driver.execute_script("arguments[0].click();", btn)
        except Exception as e:
            print(f"❌ Gagal klik tombol: {e}")
            return False

    # deteksi indikasi sukses
    success_texts = ["success", "claimed", "sent", "done", "congrats", "transaction"]
    try:
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Success"),
                EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Claimed"),
                EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Sent"),
                EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Done"),
                EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "Congrats"),
            )
        )
        print("✅ Terindikasi sukses claim.")
        return True
    except TimeoutException:
        # fallback cek HTML
        page = driver.page_source.lower()
        if any(t in page for t in success_texts):
            print("✅ Kemungkinan sukses (indikator di HTML).")
            return True
        print("⚠️ Tidak yakin sukses. Cek manual di explorer/wallet.")
        return False

def jitter_sleep(min_s: int, max_s: int):
    delay = random.randint(min_s, max_s)
    print(f"⏸️  Jeda {delay} detik...")
    time.sleep(delay)

def main():
    addresses = load_addresses()
    proxy_pool = build_proxy_pool()

    print("=== Auto-Claim FOGO Faucet ===")
    print(f"Target      : {URL}")
    print(f"Headless    : {HEADLESS}")
    print(f"Wallet total: {len(addresses)}")
    if proxy_pool:
        print(f"Proxy pool  : {len(proxy_pool)} (mode {PROXY_ROTATION})")
    else:
        print("Proxy pool  : kosong (jalan tanpa proxy)")

    for idx, wallet in enumerate(addresses, start=1):
        print(f"\n==============================")
        print(f"Wallet {idx}/{len(addresses)}: {wallet}")
        print("==============================")

        for i in range(CLAIM_ATTEMPTS):
            chosen_proxy = pick_proxy(proxy_pool, i)
            print(f"\n[Attempt {i+1}/{CLAIM_ATTEMPTS}] Proxy: {chosen_proxy or '-'}")

            success = False
            for r in range(1, RETRY_PER_ATTEMPT + 1):
                print(f"  └─ Try {r}/{RETRY_PER_ATTEMPT}...")
                driver = None
                try:
                    driver = new_driver(chosen_proxy)
                    success = do_single_claim(driver, wallet)
                except Exception as e:
                    print(f"  ❗ Error: {e}")
                finally:
                    try:
                        if driver:
                            driver.quit()
                    except Exception:
                        pass

                if success:
                    break
                else:
                    time.sleep(2 + random.random() * 2)

            if not success:
                print("✗ Gagal pada attempt ini.")
            else:
                print("✓ Selesai attempt ini.")

            if i < CLAIM_ATTEMPTS - 1:
                jitter_sleep(PAUSE_MIN_SEC, PAUSE_MAX_SEC)

    print("\n=== Selesai ===")

if __name__ == "__main__":
    main()
