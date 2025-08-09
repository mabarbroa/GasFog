import os
import random
import time
import re
import json
from typing import Optional, List, Set, Dict
from datetime import datetime, timedelta, timezone

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

# --- optional: fetch + validate free proxies ---
import requests

URL = "https://www.gas.zip/faucet/fogo"
STATE_FILE = "claims_state.json"
CLAIM_INTERVAL = timedelta(hours=24)  # 24 jam per wallet

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

STAY_RUNNING = os.getenv("STAY_RUNNING", "true").lower() == "true"
COUNTDOWN_STEP_SEC = int(os.getenv("COUNTDOWN_STEP_SEC", "60"))
PER_WALLET_DELAY_SEC = int(os.getenv("PER_WALLET_DELAY_SEC", "2"))

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
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
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
    if re.match(r"^[\d]{1,3}(?:\.[\d]{1,3}){3}:\d{2,5}$", p):
        return f"http://{p}"
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
    file_proxies = [normalize_proxy(p) for p in load_proxies_file()]
    file_proxies = [p for p in file_proxies if p]
    pool.extend(file_proxies)

    if USE_FREE_PROXIES:
        print("🌐 Mengambil proxy gratis...")
        free_list = fetch_free_proxies()
        if FREE_PROXY_VALIDATE:
            print("🧪 Validasi cepat proxy gratis...")
            valid = []
            for p in free_list:
                if validate_proxy_quick(p, FREE_PROXY_TIMEOUT):
                    valid.append(p)
            print(f"✅ Proxy gratis valid: {len(valid)} / {len(free_list)}")
            pool.extend(valid)
        else:
            pool.extend(free_list)

    seen = set()
    unique = []
    for p in pool:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique

# ----------------- State (last claim per wallet) -----------------
def load_state() -> Dict[str, float]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict[str, float]):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def seconds_until_due(wallet: str, state: Dict[str, float]) -> int:
    """
    return detik sampai wallet due lagi; <=0 artinya sudah bisa claim
    """
    last_ts = state.get(wallet)
    if not last_ts:
        return 0
    last_dt = datetime.fromtimestamp(last_ts, tz=timezone.utc)
    next_dt = last_dt + CLAIM_INTERVAL
    now = datetime.now(timezone.utc)
    delta = (next_dt - now).total_seconds()
    return int(delta)

def set_claimed_now(wallet: str, state: Dict[str, float]):
    now_ts = datetime.now(timezone.utc).timestamp()
    state[wallet] = now_ts
    save_state(state)

def format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def sleep_with_countdown(total_seconds: int, step: int = 60, prefix: str = "⏳ Next in"):
    remain = total_seconds
    while remain > 0:
        show = format_duration(remain)
        print(f"{prefix} {show}", end="\r", flush=True)
        t = min(step, remain)
        time.sleep(t)
        remain -= t
    print(" " * 40, end="\r")  # bersihkan baris

# ----------------- Selenium bits -----------------
def pick_proxy(pool: List[str], attempt_idx: int) -> Optional[str]:
    if not pool:
        return None
    if PROXY_ROTATION == "random":
        return random.choice(pool)
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

    inp = find_address_input(driver)
    if not inp:
        print("❌ Input address tidak ditemukan.")
        return False
    inp.clear()
    time.sleep(0.2)
    inp.send_keys(wallet_address)
    time.sleep(0.4)
    inp.send_keys(Keys.TAB)

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

# ----------------- Main loop (auto 24 jam) -----------------
def main_once(addresses: List[str], proxy_pool: List[str], state: Dict[str, float]) -> bool:
    """
    Jalankan satu 'scan' semua wallet. Return True kalau ada aksi (claim) terjadi,
    False kalau tidak ada wallet yang due.
    """
    any_action = False

    for idx, wallet in enumerate(addresses, start=1):
        remain = seconds_until_due(wallet, state)
        if remain > 0:
            print(f"[{idx}/{len(addresses)}] {wallet} - belum due. Next: {format_duration(remain)}")
            time.sleep(PER_WALLET_DELAY_SEC)
            continue

        print(f"\n==============================")
        print(f"Wallet {idx}/{len(addresses)}: {wallet} (DUE)")
        print("==============================")

        for i in range(CLAIM_ATTEMPTS):
            chosen_proxy = pick_proxy(proxy_pool, i)
            print(f"[Attempt {i+1}/{CLAIM_ATTEMPTS}] Proxy: {chosen_proxy or '-'}")

            success = False
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
                set_claimed_now(wallet, state)
                any_action = True
                print(f"🕒 Next claim untuk wallet ini ~ 24 jam dari sekarang.")
                break
            else:
                if i < CLAIM_ATTEMPTS - 1:
                    time.sleep(2 + random.random() * 2)

        if i < CLAIM_ATTEMPTS - 1:
            jitter_sleep(PAUSE_MIN_SEC, PAUSE_MAX_SEC)

        time.sleep(PER_WALLET_DELAY_SEC)

    return any_action

def main():
    addresses = load_addresses()
    proxy_pool = build_proxy_pool()
    state = load_state()

    print("=== Auto-Claim FOGO Faucet ===")
    print(f"Target      : {URL}")
    print(f"Headless    : {HEADLESS}")
    print(f"Wallet total: {len(addresses)}")
    print(f"Mode        : {'Loop (STAY_RUNNING=true)' if STAY_RUNNING else 'Sekali scan'}")
    if proxy_pool:
        print(f"Proxy pool  : {len(proxy_pool)} (mode {PROXY_ROTATION})")
    else:
        print("Proxy pool  : kosong (jalan tanpa proxy)")

    if not STAY_RUNNING:
        _ = main_once(addresses, proxy_pool, state)
        print("\n=== Selesai (sekali scan) ===")
        return

    # Loop terus: claim yang due, lalu tidur sampai yang tercepat due
    while True:
        did_something = main_once(addresses, proxy_pool, state)

        # cari waktu minimum sampai due berikutnya
        min_remain = None
        for w in addresses:
            r = seconds_until_due(w, state)
            if r <= 0:
                min_remain = 0
                break
            if (min_remain is None) or (r < min_remain):
                min_remain = r

        if min_remain is None:
            # tidak ada data state; aman tidur sebentar
            min_remain = 300  # 5 menit default
        if min_remain <= 0:
            # ada yang sudah due lagi (mungkin gagal set state), iterasi lagi tanpa tidur lama
            time.sleep(3)
            continue

        # countdown sampai wallet tercepat due
        print(f"\n🕘 Semua wallet belum due. Menunggu {format_duration(min_remain)} sampai wallet tercepat due...")
        sleep_with_countdown(min_remain, COUNTDOWN_STEP_SEC, prefix="⏳ Next cycle in")
        # kemudian loop lagi

if __name__ == "__main__":
    main()
