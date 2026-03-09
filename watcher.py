import os
import json
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from playwright.sync_api import sync_playwright

URL = "https://uk.webuy.com/search?stext=psp&stores=Edinburgh~Edinburgh+Cameron+Toll~Leith+Edinburgh"
STATE_FILE = Path("state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK")


def build_page_url(base_url: str, page_num: int) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def extract_price(text: str) -> float:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("£"):
            try:
                return float(line.replace("£", "").replace(",", "").strip())
            except ValueError:
                pass
    return -1.0


def extract_title(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    filtered = []
    for line in lines:
        lower = line.lower()

        if line.startswith("£"):
            continue
        if lower in {"5 year warranty", "add to basket"}:
            continue
        if lower.replace(".", "", 1).isdigit():
            continue
        if lower.startswith("★"):
            continue

        filtered.append(line)

    return filtered[0] if filtered else "Unknown item"


def get_page_items():
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

        page.goto(build_page_url(URL, 1), timeout=60000)

        try:
            page.get_by_text("Accept All").click(timeout=5000)
        except Exception:
            pass

        page.wait_for_timeout(4000)

        body_text = page.locator("body").inner_text()
        if "Performing security verification" in body_text or "Verify you are human" in body_text:
            page.screenshot(path="debug.png", full_page=True)
            browser.close()
            raise RuntimeError("Blocked by Cloudflare")

        page_count = 1
        try:
            nums = []
            links = page.locator("a").all()
            for link in links:
                try:
                    text = link.inner_text().strip()
                    if text.isdigit():
                        nums.append(int(text))
                except Exception:
                    pass
            if nums:
                page_count = max(nums)
        except Exception:
            pass

        print(f"Detected {page_count} page(s)")

        all_items = []

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
                    if not text:
                        continue

                    title = extract_title(text)
                    price = extract_price(text)

                    if price >= 0:
                        all_items.append({
                            "title": title,
                            "price": price,
                        })
                except Exception:
                    pass

        page.screenshot(path="debug.png", full_page=True)
        browser.close()

    all_items.sort(key=lambda x: (-x["price"], x["title"].lower()))

    deduped = []
    seen = set()
    for item in all_items:
        key = (item["title"], item["price"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped


def load_old():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return []


def save_state(items):
    STATE_FILE.write_text(json.dumps(items, indent=2))


def send_discord_message(message: str):
    if not WEBHOOK:
        print("No DISCORD_WEBHOOK set")
        return

    requests.post(
        WEBHOOK,
        json={"content": message[:1900]},
        timeout=20,
    )


def diff_items(old_items, new_items):
    old_by_title = {item["title"]: item for item in old_items}
    new_by_title = {item["title"]: item for item in new_items}

    new_titles = set(new_by_title.keys()) - set(old_by_title.keys())
    removed_titles = set(old_by_title.keys()) - set(new_by_title.keys())

    price_changed = []
    for title in set(old_by_title.keys()) & set(new_by_title.keys()):
        old_price = old_by_title[title]["price"]
        new_price = new_by_title[title]["price"]
        if old_price != new_price:
            price_changed.append({
                "title": title,
                "old_price": old_price,
                "new_price": new_price,
            })

    added = sorted(
        [new_by_title[t] for t in new_titles],
        key=lambda x: (-x["price"], x["title"].lower())
    )
    removed = sorted(
        [old_by_title[t] for t in removed_titles],
        key=lambda x: (-x["price"], x["title"].lower())
    )
    price_changed = sorted(
        price_changed,
        key=lambda x: (-x["new_price"], x["title"].lower())
    )

    return added, removed, price_changed


def format_message(added, removed, price_changed):
    parts = []

    if added:
        lines = ["**New items**"]
        for item in added[:15]:
            lines.append(f"£{item['price']:.2f} — {item['title']}")
        parts.append("\n".join(lines))

    if removed:
        lines = ["**Removed items**"]
        for item in removed[:15]:
            lines.append(f"£{item['price']:.2f} — {item['title']}")
        parts.append("\n".join(lines))

    if price_changed:
        lines = ["**Price changes**"]
        for item in price_changed[:15]:
            lines.append(
                f"{item['title']} — £{item['old_price']:.2f} → £{item['new_price']:.2f}"
            )
        parts.append("\n".join(lines))

    if not parts:
        return ""

    header = "**CeX PSP update**"
    return header + "\n\n" + "\n\n".join(parts)


def main():
    try:
        new_items = get_page_items()
    except Exception as e:
        print(f"Run failed: {e}")
        return

    old_items = load_old()

    if not old_items:
        print("First run: saving baseline")
        save_state(new_items)
        return

    added, removed, price_changed = diff_items(old_items, new_items)

    if added or removed or price_changed:
        print("Change detected")
        message = format_message(added, removed, price_changed)
        if message:
            send_discord_message(message)
        save_state(new_items)
    else:
        print("No change")


if __name__ == "__main__":
    main()
