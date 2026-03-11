import os
import re
import json
from pathlib import Path
from urllib.parse import (
    urlencode,
    urlparse,
    parse_qs,
    urlunparse,
    urljoin,
)

import requests
from playwright.sync_api import sync_playwright

URL = "https://uk.webuy.com/search?stext=psp&stores=Edinburgh~Edinburgh+Cameron+Toll~Leith+Edinburgh"
STATE_FILE = Path("state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
BASE_URL = "https://uk.webuy.com"


def build_page_url(base_url: str, page_num: int) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def extract_price(text: str) -> float:
    match = re.search(r"£\s*([\d,]+(?:\.\d{2})?)", text)
    if not match:
        return -1.0
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return -1.0


def extract_store(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for i, line in enumerate(lines):
        lower = line.lower()

        if lower in {"nearest store:", "nearest store"}:
            if i + 1 < len(lines):
                return lines[i + 1]

        if lower.startswith("nearest store:"):
            parts = line.split(":", 1)
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()

    return "Unknown store"


def clean_title(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    junk = {
        "add to basket",
        "add to wishlist",
        "wishlist",
        "5 year warranty",
    }

    filtered = []
    for line in lines:
        lower = line.lower()
        if lower in junk:
            continue
        if line.startswith("£"):
            continue
        if lower.startswith("from £"):
            continue
        filtered.append(line)

    return filtered[0] if filtered else "Unknown item"


def normalise_product_id(product_url: str) -> str:
    parsed = urlparse(product_url)
    query = parse_qs(parsed.query)
    return query.get("id", [product_url])[0]


def dismiss_cookie_banner(page):
    try:
        btn = page.get_by_role("button", name="Accept All")
        btn.click(timeout=5000, force=True)
        page.wait_for_timeout(1500)
        print("Cookie banner dismissed")
    except Exception:
        print("Cookie banner not present or already dismissed")


def get_page_count(page) -> int:
    nums = set()

    try:
        hrefs = page.locator("a[href*='page=']").evaluate_all(
            "(els) => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
        for href in hrefs:
            parsed = urlparse(href)
            query = parse_qs(parsed.query)
            for val in query.get("page", []):
                if val.isdigit():
                    nums.add(int(val))
    except Exception as e:
        print(f"Page count detection failed: {e}")

    return max(nums) if nums else 1


def scrape_products_from_page(page):
    raw = page.evaluate("""
    () => {
        const links = Array.from(document.querySelectorAll('a[href*="/product-detail?id="]'));

        return links.map((a) => {
            const href = a.getAttribute('href') || '';
            const text = (a.innerText || a.textContent || '').trim();

            let el = a;
            let blockText = '';
            let depth = 0;

            while (el && depth < 8) {
                const t = (el.innerText || el.textContent || '').trim();
                if (t && t.includes('£')) {
                    blockText = t;
                    break;
                }
                el = el.parentElement;
                depth += 1;
            }

            return {
                href,
                text,
                blockText
            };
        });
    }
    """)

    print(f"Found {len(raw)} product-detail links on page")

    items = []
    seen = set()

    for i, row in enumerate(raw):
        href = (row.get("href", "") or "").strip()
        text = (row.get("text", "") or "").strip()
        block_text = (row.get("blockText", "") or "").strip()

        if not href or not text:
            continue

        full_url = urljoin(BASE_URL, href)

        if i < 20:
            print(
                f"DEBUG PRODUCT {i}: href={full_url} | "
                f"text={text[:60]!r} | block={block_text[:120]!r}"
            )

        title = clean_title(text)
        if title == "Unknown item":
            continue

        price = extract_price(block_text)
        if price < 0:
            continue

        store = extract_store(block_text)
        product_id = normalise_product_id(full_url)

        if product_id in seen:
            continue

        seen.add(product_id)

        items.append({
            "id": product_id,
            "title": title,
            "price": price,
            "store": store,
            "url": full_url,
        })

    for item in items[:5]:
        print(
            f"Sample page item: {item['title']} | £{item['price']:.2f} | "
            f"{item['store']} | {item['url']}"
        )

    return items


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
        page.wait_for_timeout(3000)

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
            page.wait_for_timeout(2000)

            body_text = page.locator("body").inner_text()
            if "Performing security verification" in body_text or "Verify you are human" in body_text:
                page.screenshot(path=f"debug-page-{page_num}.png", full_page=True)
                print(f"Blocked by Cloudflare on page {page_num}")
                continue

            page_items = scrape_products_from_page(page)
            print(f"Found {len(page_items)} products on page {page_num}")
            all_items.extend(page_items)

        page.screenshot(path="debug.png", full_page=True)
        browser.close()

    all_items.sort(key=lambda x: (-x["price"], x["title"].lower(), x["id"]))

    deduped = []
    seen = set()
    for item in all_items:
        if item["id"] not in seen:
            seen.add(item["id"])
            deduped.append(item)

    print(f"Total scraped items before dedupe: {len(all_items)}")
    print(f"Total scraped items after dedupe: {len(deduped)}")

    for item in deduped[:5]:
        print(
            f"Sample item: {item['title']} | £{item['price']:.2f} | "
            f"{item['store']} | {item['url']}"
        )

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
    old_by_id = {item["id"]: item for item in old_items}
    new_by_id = {item["id"]: item for item in new_items}

    added = []
    removed = []
    price_changed = []

    for item_id in new_by_id:
        if item_id not in old_by_id:
            added.append(new_by_id[item_id])

    for item_id in old_by_id:
        if item_id not in new_by_id:
            removed.append(old_by_id[item_id])

    for item_id in set(old_by_id.keys()) & set(new_by_id.keys()):
        old_item = old_by_id[item_id]
        new_item = new_by_id[item_id]

        old_price = old_item["price"]
        new_price = new_item["price"]

        old_store = old_item.get("store", "Unknown store")
        new_store = new_item.get("store", "Unknown store")

        if old_price != new_price or old_store != new_store:
            price_changed.append({
                "title": new_item["title"],
                "url": new_item["url"],
                "old_price": old_price,
                "new_price": new_price,
                "old_store": old_store,
                "new_store": new_store,
            })

    added.sort(key=lambda x: (-x["price"], x["title"].lower(), x["id"]))
    removed.sort(key=lambda x: (-x["price"], x["title"].lower(), x["id"]))
    price_changed.sort(key=lambda x: (-x["new_price"], x["title"].lower()))

    return added, removed, price_changed


def format_message(added, removed, price_changed):
    parts = []

    if added:
        lines = ["**New items**"]
        for item in added[:15]:
            lines.append(
                f"£{item['price']:.2f} — {item['title']} — {item.get('store', 'Unknown store')}"
            )
            lines.append(item["url"])
        parts.append("\n".join(lines))

    if removed:
        lines = ["**Removed items**"]
        for item in removed[:15]:
            lines.append(
                f"£{item['price']:.2f} — {item['title']} — {item.get('store', 'Unknown store')}"
            )
            lines.append(item["url"])
        parts.append("\n".join(lines))

    if price_changed:
        lines = ["**Price / store changes**"]
        for item in price_changed[:15]:
            old_store = item.get("old_store", "Unknown store")
            new_store = item.get("new_store", "Unknown store")
            lines.append(
                f"{item['title']} — £{item['old_price']:.2f} → £{item['new_price']:.2f} | {old_store} → {new_store}"
            )
            lines.append(item["url"])
        parts.append("\n".join(lines))

    if not parts:
        return ""

    return "**CeX PSP update**\n\n" + "\n\n".join(parts)


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
        f"Added: {len(added)}, Removed: {len(removed)}, "
        f"Price changed: {len(price_changed)}"
    )

    if added or removed or price_changed:
        print("Change detected")
        message = format_message(added, removed, price_changed)
        if message:
            send_discord_message(message)
    else:
        print("No change")

    save_state(new_items)


if __name__ == "__main__":
    main()
