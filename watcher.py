import os
import json
import hashlib
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://uk.webuy.com/search?stext=psp&stores=Edinburgh~Edinburgh+Cameron+Toll~Leith+Edinburgh"
STATE_FILE = Path("state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK")


from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

def build_page_url(base_url: str, page_num: int) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def get_page_text():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            locale="en-GB",
            viewport={"width": 1280, "height": 900}
        )

        page = context.new_page()
        all_items = []

        # Load first page
        page.goto(build_page_url(URL, 1), timeout=60000)

        try:
            page.get_by_text("Accept All").click(timeout=5000)
        except:
            pass

        page.wait_for_timeout(4000)

        # Detect page count from pagination links
        page_count = 1
        try:
            links = page.locator("a").all()
            nums = []

            for link in links:
                try:
                    text = link.inner_text().strip()
                    if text.isdigit():
                        nums.append(int(text))
                except:
                    pass

            if nums:
                page_count = max(nums)
        except:
            pass

        print(f"Detected {page_count} page(s)")

        # Scrape all pages
        for page_num in range(1, page_count + 1):
            url = build_page_url(URL, page_num)
            print(f"Checking page {page_num}: {url}")

            page.goto(url, timeout=60000)
            page.wait_for_timeout(3000)

            products = page.locator("div[data-testid='product-card']").all()

            print(f"Found {len(products)} products on page {page_num}")

            for product in products:
                try:
                    text = product.inner_text().strip()
                    if text:
                        all_items.append(text)
                except:
                    pass

        page.screenshot(path="debug.png", full_page=True)
        browser.close()

        return "\n".join(all_items)

def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_old():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(text: str):
    payload = {
        "hash": digest(text),
        "text": text[:50000],  # keep it reasonable
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2))


def notify():
    if not WEBHOOK:
        print("No DISCORD_WEBHOOK set")
        return

    requests.post(
        WEBHOOK,
        json={"content": f"CeX PSP page changed: {URL}"},
        timeout=20,
    )


def main():
    new_text = get_page_text()

    if "Performing security verification" in new_text or "Verify you are human" in new_text:
        print("Blocked by Cloudflare - not saving state")
        return

    new_hash = digest(new_text)
    old = load_old()

    if old is None:
        print("First run: saving baseline")
        save_state(new_text)
        return

    if old.get("hash") != new_hash:
        print("Change detected")
        notify()
        save_state(new_text)
    else:
        print("No change")


if __name__ == "__main__":
    main()
