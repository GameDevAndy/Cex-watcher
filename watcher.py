import os
import json
import hashlib
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://uk.webuy.com/search?stext=psp&stores=Edinburgh~Edinburgh+Cameron+Toll~Leith+Edinburgh"
STATE_FILE = Path("state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK")


def get_page_text():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            locale="en-GB",
            viewport={"width":1280,"height":900}
        )

        page = context.new_page()

        page.goto(URL, timeout=60000)

        # accept cookie popup if present
        try:
            page.get_by_text("Accept All").click(timeout=5000)
        except:
            pass

        page.wait_for_timeout(5000)

        products = page.locator("div[data-testid='product-card']").all()

        items = []

        for p in products:
            text = p.inner_text()
            items.append(text)

        page.screenshot(path="debug.png", full_page=True)

        browser.close()

        return "\n".join(items)


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
