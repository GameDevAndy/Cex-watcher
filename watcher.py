import os
import json
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse, urljoin

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
        if lower in {
            "5 year warranty",
            "add to basket",
            "add to wishlist",
            "wishlist",
        }:
            continue
        if lower.replace(".", "", 1).isdigit():
            continue
        if lower.startswith("★"):
            continue

        filtered.append(line)

    return filtered[0] if filtered else "Unknown item"


def dismiss_cookie_banner(page):
    try:
        accept_button = page.get_by_role("button", name="Accept All")
        accept_button.wait_for(timeout=10000)
        accept_button.click(force=True)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"Cookie banner not dismissed on click attempt: {e}")

    try:
        page.get_by_role("button", name="Accept All").wait_for(
            state="hidden",
            timeout=5000,
        )
        print("Cookie banner dismissed")
    except Exception:
        print("Cookie banner still visible or not present")


def get_page_count(page) -> int:
    page_count = 1

    try:
        links = page.locator("a[href*='page=']").all()
        nums = []

        for link in links:
            try:
                href = link.get_attribute("href")
                if not href:
                    continue

                parsed = urlparse(href)
                query = parse_qs(parsed.query)
                if "page" in query:
                    value = query["page"][0]
                    if value.isdigit():
                        nums.append(int(value))
            except Exception:
                pass

        if nums:
            page_count = max(nums)
    except Exception as e:
        print(f"Failed to detect page count cleanly: {e}")

    return max(page_count, 1)


def get_product_url(product):
    possible_selectors = [
        "a[href*='/product-detail']",
        "a[href*='/product']",
        "a",
    ]

    for selector in possible_selectors:
        try:
            locator = product.locator(selector).first
            href = locator.get_attribute("href")
            if href:
                return urljoin("https://uk.webuy.com", href)
        except Exception:
            pass

    return None


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
        first_url = build_page_url(URL, 1)
        print(f"Opening first page: {first_url}")
        page.goto(first_url, timeout=60000, wait_until="domcontentloaded")

        dismiss_cookie_banner(page)
        page.wait_for_timeout(4000)

        body_text = page.locator("body").inner_text()
        if "Performing security verification" in body_text or "Verify you are human" in body_text:
            page.screenshot(path="debug.png", full_page=True)
            browser.close()
            raise RuntimeError("Blocked by Cloudflare")

        page_count = get_page_count(page)
        print(f"Detected {page_count} page(s)")

        all_items = []

        for page_num in range(1, page_count + 1):
            url = build_page_url(URL, page_num)
            print(f"Checking page {page_num}: {url}")

            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            dismiss_cookie_banner(page)
            page.wait_for_timeout(3000)

            body_text = page.locator("body").inner_text()
            if "Performing security verification" in body_text or "Verify you are human" in body_text:
                page.screenshot(path=f"debug-page-{page_num}.png", full_page=True)
                print(f"Blocked by Cloudflare on page {page_num}")
                continue

            products = page.locator("div[data-testid='product-card']").all()
            print(f"Found {len(products)} products on page {page_num}")

            for product in products:
                try:
                    text = product.inner_text().strip()
                    if not text:
                        continue

                    title = extract_title(text)
                    price = extract_price(text)
                    product_url = get_product_url(product)

                    if price < 0:
                        continue
                    if not product_url:
                        print(f"Skipping item with no URL: {title}")
                        continue

                    all_items.append({
                        "id": product_url,
                        "title": title,
                        "price": price,
                        "url": product_url,
                    })

                except Exception as e:
                    print(f"Failed to parse product: {e}")

        page.screenshot(path="debug.png", full_page=True)
        browser.close()

    all_items.sort(key=lambda x: (-x["price"], x["title"].lower(), x["id"]))

    deduped = []
    seen = set()

    for item in all_items:
        key = item["id"]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    print(f"Total scraped items before dedupe: {len(all_items)}")
    print(f"Total scraped items after dedupe: {len(deduped)}")

    return deduped


def load_old():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, list):
                return data
        except Exception as e:
            print(f"Failed to load state.json: {e}")
    return []


def save_state(items):
    STATE_FILE.write_text(json.dumps(items, indent=2))


def send_discord_message(message: str):
    if not WEBHOOK:
        print("No DISCORD_WEBHOOK set")
        return

    try:
        response = requests.post(
            WEBHOOK,
            json={"content": message[:1900]},
            timeout=20,
        )
        print(f"Discord webhook status: {response.status_code}")
        response.raise_for_status()
    except Exception as e:
        print(f"Failed to send Discord message: {e}")


def diff_items(old_items, new_items):
    old_by_id = {item["id"]: item for item in old_items if "id" in item}
    new_by_id = {item["id"]: item for item in new_items if "id" in item}

    added_ids = set(new_by_id.keys()) - set(old_by_id.keys())
    removed_ids = set(old_by_id.keys()) - set(new_by_id.keys())

    price_changed = []
    for item_id in set(old_by_id.keys()) & set(new_by_id.keys()):
        old_price = old_by_id[item_id]["price"]
        new_price = new_by_id[item_id]["price"]
        if old_price != new_price:
            price_changed.append({
                "title": new_by_id[item_id]["title"],
                "url": new_by_id[item_id]["url"],
                "old_price": old_price,
                "new_price": new_price,
            })

    added = sorted(
        [new_by_id[item_id] for item_id in added_ids],
        key=lambda x: (-x["price"], x["title"].lower(), x["id"])
    )
    removed = sorted(
        [old_by_id[item_id] for item_id in removed_ids],
        key=lambda x: (-x["price"], x["title"].lower(), x["id"])
    )
    price_changed = sorted(
        price_changed,
        key=lambda x: (-x["new_price"], x["title"].lower(), x["url"])
    )

    return added, removed, price_changed


def format_message(added, removed, price_changed):
    parts = []

    if added:
        lines = ["**New items**"]
        for item in added[:15]:
            lines.append(f"£{item['price']:.2f} — {item['title']}")
            lines.append(item["url"])
        parts.append("\n".join(lines))

    if removed:
        lines = ["**Removed items**"]
        for item in removed[:15]:
            lines.append(f"£{item['price']:.2f} — {item['title']}")
            lines.append(item["url"])
        parts.append("\n".join(lines))

    if price_changed:
        lines = ["**Price changes**"]
        for item in price_changed[:15]:
            lines.append(
                f"{item['title']} — £{item['old_price']:.2f} → £{item['new_price']:.2f}"
            )
            lines.append(item["url"])
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

    print(f"Loaded old items: {len(old_items)}")
    print(f"Scraped new items: {len(new_items)}")

    if not old_items:
        print("First run: saving baseline")
        save_state(new_items)
        return

    added, removed, price_changed = diff_items(old_items, new_items)

    print(
        f"Added: {len(added)}, Removed: {len(removed)}, Price changed: {len(price_changed)}"
    )

    if added or removed or price_changed:
        print("Change detected")
        message = format_message(added, removed, price_changed)
        if message:
            send_discord_message(message)
        save_state(new_items)
    else:
        print("No change")
        save_state(new_items)


if __name__ == "__main__":
    main()
