#!/usr/bin/env python3
"""
Apple.ae Stock ALERT Bot — GitHub Actions version. Alert-only, no purchase.

Watches the iPhone configurator page across MULTIPLE storage capacities
and MULTIPLE cities, checks every color offered, and emails you ONLY
when a color is available for in-store PICKUP and the pickup date is
one of your target dates (default: Today, 18/19/20 Sep). Delivery-only
availability and pickup dates outside that list are ignored. Never adds
to bag or checks out.

No login/session is required — availability is public info.
"""

import os
import re
import time
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

MODEL_URL = os.environ.get("MODEL_URL", "https://www.apple.com/ae/shop/buy-iphone/iphone-17-pro")
SCREEN_KEYWORD = os.environ.get("SCREEN_KEYWORD", "6_9inch")
CAPACITY_KEYWORDS = [c.strip() for c in os.environ.get("CAPACITY_KEYWORDS", "256gb,512gb,1tb").split(",") if c.strip()]
CITIES = [c.strip() for c in os.environ.get("CITIES", "Dubai,Abu Dhabi,Al Ain").split(",") if c.strip()]
TARGET_DATES = [d.strip().lower() for d in os.environ.get("TARGET_DATES", "today,18,19,20").split(",") if d.strip()]
TARGET_MONTH = os.environ.get("TARGET_MONTH", "sep").lower()
REST_SECONDS = int(os.environ.get("REST_SECONDS", "30"))  # fixed rest after each full check, regardless of how long it took
SHIFT_MINUTES = int(os.environ.get("SHIFT_MINUTES", "230"))
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", GMAIL_ADDRESS)


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = f"[Apple Alert Bot] {subject}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ALERT_EMAIL_TO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        print("Failed to send email:", e)


def visible_text(page):
    return page.evaluate(
        """() => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script,style,noscript').forEach(e => e.remove());
            return clone.innerText;
        }"""
    )


def click_option_matching(page, keyword):
    """Select a radio option two ways: (1) match Apple's real
    data-autom attribute containing the keyword (confirmed reliable
    for screen size and 256GB capacity), or (2) fall back to finding
    a visible label whose TEXT contains the human-readable version of
    the keyword (e.g. "512gb" -> "512GB") and clicking its radio —
    used for capacities we haven't verified the attribute name for."""
    target = page.locator(f'input[data-autom*="{keyword}"]').first
    found = target.count() > 0

    if not found:
        # Fallback: text-based match, e.g. "512gb" -> "512GB", "1tb" -> "1TB"
        human = keyword.upper()
        handle = page.evaluate_handle(
            """(human) => {
                const labels = Array.from(document.querySelectorAll('label, span, div'));
                const match = labels.find(el => (el.innerText || '').trim().toUpperCase().includes(human) && el.offsetParent !== null);
                if (!match) return null;
                return match.closest('label') || match.querySelector('input[type="radio"]') || match.previousElementSibling;
            }""",
            human,
        )
        el = handle.as_element()
        if el is None:
            return False
        try:
            el.click(timeout=2000)
            page.wait_for_timeout(600)
            return True
        except Exception:
            return False

    # Force a real change event even if already checked=true from a
    # restored cookie (React's state doesn't always sync to that).
    if target.is_checked():
        group_name = target.get_attribute("name")
        siblings = page.locator(f'input[type="radio"][name="{group_name}"]')
        if siblings.count() > 1:
            try:
                siblings.nth(1).click(timeout=1000)
                page.wait_for_timeout(400)
            except Exception:
                pass
    try:
        target.click(timeout=2000)
        page.wait_for_timeout(600)
        return True
    except Exception:
        return False


def check_availability_for_city(page, city):
    # Fast path: inline fulfillment quote, shown when the session
    # already has a known location matching this city.
    try:
        inline = page.locator(".rf-fulfillment-quote").first
        if inline.is_visible(timeout=1000):
            text = inline.inner_text()
            if city.lower() in text.lower():
                return text
    except Exception:
        pass

    trigger = page.locator('[data-autom^="productLocatorTriggerLink"], .rf-pickup-quote-overlay-trigger').first
    try:
        trigger.click(timeout=2000)
    except Exception:
        return None

    overlay_text = None
    try:
        city_select = page.locator('select[name="city"]')
        city_select.wait_for(timeout=3000)
        city_select.select_option(label=city)
        page.wait_for_timeout(1500)
        overlay_text = page.locator(".rf-productlocator-overlay").inner_text()
    except Exception:
        pass

    try:
        page.locator('[data-autom="overlay-close"]').click(timeout=1000)
    except Exception:
        pass

    return overlay_text


def matching_pickup_lines(text):
    """Return lines confirming in-store PICKUP on a target date, tagged
    TODAY (top priority) or TARGET DATE. Skips delivery-only lines."""
    if not text:
        return []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    hits = []
    day_targets = [d for d in TARGET_DATES if d != "today"]
    date_pattern = re.compile(
        rf"\b({'|'.join(re.escape(d) for d in day_targets)})\b.{{0,10}}\b{TARGET_MONTH}\w*\b"
        rf"|\b{TARGET_MONTH}\w*\b.{{0,10}}\b({'|'.join(re.escape(d) for d in day_targets)})\b",
        re.IGNORECASE,
    ) if day_targets else None

    for i, line in enumerate(lines):
        low = line.lower()
        if "deliver" in low:
            continue
        is_today = "today" in TARGET_DATES and "today" in low
        is_target_date = bool(date_pattern and date_pattern.search(low))
        if not (is_today or is_target_date):
            continue
        context = lines[i - 1] if i > 0 and "apple" in lines[i - 1].lower() else ""
        line_out = f"{context} — {line}" if context else line
        hits.append(("TODAY" if is_today else "TARGET DATE", line_out))
    return hits


def poll_once(page):
    results = []

    for capacity in CAPACITY_KEYWORDS:
        page.goto(MODEL_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)

        if "captcha" in visible_text(page).lower():
            continue

        click_option_matching(page, SCREEN_KEYWORD)
        capacity_ok = click_option_matching(page, capacity)
        if not capacity_ok:
            print(f"Could not select capacity '{capacity}' — skipping this capacity this cycle.")
            continue

        color_radios = page.locator('input[data-autom^="dimensionColor"]')
        count = color_radios.count()
        for i in range(count):
            radio = color_radios.nth(i)
            color_name = radio.get_attribute("data-autom")
            try:
                radio.click(timeout=2000)
                page.wait_for_timeout(800)
            except Exception:
                continue

            add_to_bag = page.locator('[data-autom="add-to-cart"]')
            try:
                if not add_to_bag.is_enabled(timeout=1000):
                    continue
            except Exception:
                continue

            for city in CITIES:
                avail_text = check_availability_for_city(page, city)
                for tag, line in matching_pickup_lines(avail_text):
                    results.append((capacity, color_name, city, tag, line))

    return results


def main():
    deadline = datetime.utcnow() + timedelta(minutes=SHIFT_MINUTES)
    already_alerted = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        cycle_num = 0
        while datetime.utcnow() < deadline:
            cycle_num += 1
            cycle_start = time.time()
            try:
                for capacity, color, city, tag, line in poll_once(page):
                    key = (capacity, color, city, line[:80])
                    if key not in already_alerted:
                        already_alerted.add(key)
                        priority = "TODAY — " if tag == "TODAY" else ""
                        send_email(
                            f"{priority}iPhone pickup available — {capacity} {color} ({city})",
                            f"Capacity: {capacity}\nColor: {color}\nCity checked: {city}\nMatch: {tag}\n\n{line}\n\n{MODEL_URL}",
                        )
                        print(f"Alerted [{tag}]: {capacity} {color} {city} — {line[:80]}")
            except Exception as e:
                print("Error during poll:", e)

            elapsed = time.time() - cycle_start
            print(f"Cycle {cycle_num}: full check took {elapsed:.1f}s — resting {REST_SECONDS}s")
            time.sleep(REST_SECONDS)

        browser.close()
        print("Shift ended.")


if __name__ == "__main__":
    main()
