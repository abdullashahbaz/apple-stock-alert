#!/usr/bin/env python3
"""
Apple.ae Stock ALERT Bot — GitHub Actions version. Alert-only, no purchase.

Watches the iPhone configurator page for a given screen size + storage
config, checks every color offered, and emails you ONLY when a color is
available for in-store PICKUP and the pickup date is one of your target
dates (by default: Today, 18 Sep, 19 Sep, 20 Sep). Delivery-only
availability and pickup dates outside that list are ignored. "Today" is
flagged as the top-priority match since that's what you care about most.
Never adds to bag or checks out.

No login/session is required for this — availability is public info.
"""

import os
import re
import time
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

MODEL_URL = os.environ.get("MODEL_URL", "https://www.apple.com/ae/shop/buy-iphone/iphone-17-pro")
SCREEN_KEYWORD = os.environ.get("SCREEN_KEYWORD", "6_9inch")     # matches dimensionScreensize6_9inch
CAPACITY_KEYWORD = os.environ.get("CAPACITY_KEYWORD", "256gb")   # matches dimensionCapacity256gb
CITY = os.environ.get("CITY", "Dubai")
# Comma-separated target pickup dates. "today" is handled as a keyword;
# the rest are matched as day numbers against TARGET_MONTH below.
TARGET_DATES = [d.strip().lower() for d in os.environ.get("TARGET_DATES", "today,18,19,20").split(",") if d.strip()]
TARGET_MONTH = os.environ.get("TARGET_MONTH", "sep").lower()  # matches "Sep"/"September"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
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


def ensure_radio_selected(page, keyword):
    """Apple's page can load with a config already checked=true from a
    restored cookie, but React's own state doesn't sync to that,
    leaving Add to Bag disabled. Force a real change event by clicking
    a sibling option first, then the one we actually want."""
    target = page.locator(f'input[data-autom*="{keyword}"]').first
    if target.count() == 0:
        return
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
    except Exception:
        pass


def check_availability_for_current_selection(page):
    # Fast path: inline fulfillment quote, shown when the session
    # already has a known location.
    try:
        inline = page.locator(".rf-fulfillment-quote").first
        if inline.is_visible(timeout=1000):
            return inline.inner_text()
    except Exception:
        pass

    # Full path: open the store-list overlay and pick the city.
    trigger = page.locator('[data-autom^="productLocatorTriggerLink"], .rf-pickup-quote-overlay-trigger').first
    try:
        trigger.click(timeout=2000)
    except Exception:
        return None

    overlay_text = None
    try:
        city_select = page.locator('select[name="city"]')
        city_select.wait_for(timeout=3000)
        city_select.select_option(label=CITY)
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
    """Return the specific line(s) confirming in-store PICKUP on one of
    our target dates, tagged with whether it's a "today" hit (top
    priority) or a specific-date hit. Ignores delivery-only lines
    (e.g. "Order today. Delivers ...") entirely — those aren't pickup."""
    if not text:
        return []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    hits = []
    day_targets = [d for d in TARGET_DATES if d != "today"]
    # Matches "18 Sep", "Sep 18", "18 September" etc.
    date_pattern = re.compile(
        rf"\b({'|'.join(re.escape(d) for d in day_targets)})\b.{{0,10}}\b{TARGET_MONTH}\w*\b"
        rf"|\b{TARGET_MONTH}\w*\b.{{0,10}}\b({'|'.join(re.escape(d) for d in day_targets)})\b",
        re.IGNORECASE,
    ) if day_targets else None

    for i, line in enumerate(lines):
        low = line.lower()
        if "deliver" in low:  # delivery line, not pickup — skip regardless of date
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
    page.goto(MODEL_URL, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(1500)

    if "captcha" in visible_text(page).lower():
        return []

    ensure_radio_selected(page, SCREEN_KEYWORD)
    ensure_radio_selected(page, CAPACITY_KEYWORD)

    results = []
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
        orderable = False
        try:
            orderable = add_to_bag.is_enabled(timeout=1000)
        except Exception:
            pass
        if not orderable:
            continue

        avail_text = check_availability_for_current_selection(page)
        for tag, line in matching_pickup_lines(avail_text):
            results.append((color_name, tag, line))

    return results


def main():
    deadline = datetime.utcnow() + timedelta(minutes=SHIFT_MINUTES)
    already_alerted = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        while datetime.utcnow() < deadline:
            try:
                for color, tag, line in poll_once(page):
                    key = (color, line[:80])
                    if key not in already_alerted:
                        already_alerted.add(key)
                        priority = "TODAY — " if tag == "TODAY" else ""
                        send_email(
                            f"{priority}iPhone pickup available — {color}",
                            f"Config: {SCREEN_KEYWORD} / {CAPACITY_KEYWORD}\nColor: {color}\nMatch: {tag}\n\n{line}\n\n{MODEL_URL}",
                        )
                        print(f"Alerted [{tag}]: {color} — {line[:80]}")
            except Exception as e:
                print("Error during poll:", e)
            time.sleep(POLL_SECONDS)

        browser.close()
        print("Shift ended.")


if __name__ == "__main__":
    main()
