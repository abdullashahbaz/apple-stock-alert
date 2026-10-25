#!/usr/bin/env python3
"""
Apple.ae Stock ALERT Bot - GitHub Actions version. Alert-only, no purchase.

Checks every colour of the chosen storage sizes for in-store PICKUP
availability in your chosen cities and emails you when it finds some.

TARGET_DATES controls what counts as an alert:
  any                -> ANY real pickup line (Today, Tomorrow, a dated store)
  today,18,19,20     -> only those (day numbers must sit next to TARGET_MONTH)
"Currently unavailable" and delivery-only lines never alert.

The log prints, for every check, whether the city switch worked and the exact
pickup lines the bot read, so a silent run can be diagnosed from the log.
"""

import os
import re
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright


def env_list(name, default):
    raw = os.environ.get(name) or default
    return [x.strip() for x in raw.split(",") if x.strip()]


MODEL_URL = os.environ.get("MODEL_URL") or "https://www.apple.com/ae/shop/buy-iphone/iphone-18-pro"
SCREEN_KEYWORD = os.environ.get("SCREEN_KEYWORD") or "6_9inch"
CAPACITY_KEYWORDS = env_list("CAPACITY_KEYWORDS", "256gb,512gb,1tb")
CITIES = env_list("CITIES", "Dubai,Abu Dhabi,Al Ain")
TARGET_DATES = [d.lower() for d in env_list("TARGET_DATES", "today,18,19,20")]
TARGET_MONTH = (os.environ.get("TARGET_MONTH") or "sep").lower()
REST_SECONDS = int(os.environ.get("REST_SECONDS") or "30")
SHIFT_MINUTES = int(os.environ.get("SHIFT_MINUTES") or "230")
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO") or GMAIL_ADDRESS

_debug_sent = [False]
_screenshot_counter = [0]


def cycle_screenshot_tag():
    _screenshot_counter[0] += 1
    return _screenshot_counter[0]


def send_email(subject, body, attachment_path=None):
    if attachment_path:
        msg = MIMEMultipart()
        msg["Subject"] = f"[Apple Alert Bot] {subject}"
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = ALERT_EMAIL_TO
        msg.attach(MIMEText(body))
        try:
            with open(attachment_path, "rb") as f:
                img = MIMEImage(f.read())
                img.add_header("Content-Disposition", "attachment", filename=os.path.basename(attachment_path))
                msg.attach(img)
        except Exception as e:
            msg.attach(MIMEText(f"\n(could not attach screenshot: {e})"))
    else:
        msg = MIMEText(body)
        msg["Subject"] = f"[Apple Alert Bot] {subject}"
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = ALERT_EMAIL_TO

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        print("Failed to send email:", e, flush=True)


def visible_text(page):
    return page.evaluate(
        """() => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script,style,noscript').forEach(e => e.remove());
            return clone.innerText;
        }"""
    )


def click_via_label(page, target):
    """Click an input's <label for="..."> instead of the input itself.
    Apple renders these inputs invisible with a visible label on top, so
    clicking the input directly gets blocked by the label."""
    input_id = target.get_attribute("id")
    clickable = target
    if input_id:
        label = page.locator(f'label[for="{input_id}"]')
        if label.count() > 0:
            clickable = label.first
    clickable.click(timeout=2000)


def click_option_matching(page, keyword, fallback_text=None):
    """Select a radio option by its data-autom keyword; if no such input
    exists, fall back to a visible label containing fallback_text."""
    target = page.locator(f'input[data-autom*="{keyword}"]').first
    if target.count() == 0:
        human = (fallback_text or keyword).upper()
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

    # Force a real change event even if already checked from a restored cookie.
    if target.is_checked():
        group_name = target.get_attribute("name")
        siblings = page.locator(f'input[type="radio"][name="{group_name}"]')
        if siblings.count() > 1:
            try:
                click_via_label(page, siblings.nth(1))
                page.wait_for_timeout(400)
            except Exception:
                pass
    try:
        click_via_label(page, target)
        page.wait_for_timeout(600)
        return True
    except Exception:
        return False


def delivers_to(text, city):
    """True if the page's delivery line currently names this city."""
    return bool(re.search(rf"delivers to:?\s*{re.escape(city)}", text, re.IGNORECASE))


def check_availability_for_city(page, city):
    """Returns (page_text, note, confirmed). 'confirmed' means the page
    itself shows it is now quoting for this city."""
    text = visible_text(page)
    if delivers_to(text, city):
        return text, "page already on this city", True

    trigger = page.locator('[data-autom="deliveryDateChecker"], .rf-dude-quote-overlay-trigger').first
    try:
        trigger.click(timeout=2000)
    except Exception:
        return text, "location link NOT found - using default location", False

    note = "city switch attempted"
    try:
        city_select = page.locator('select[name="city"]')
        city_select.wait_for(timeout=3000)
        city_select.select_option(label=city)
        page.wait_for_timeout(800)
        view_options = page.locator('[data-autom="viewOptions"]')
        if view_options.count() > 0:
            view_options.first.click(timeout=2000)
        page.wait_for_timeout(1500)
    except Exception as e:
        note = f"city switch FAILED ({type(e).__name__})"

    try:
        page.locator('[data-autom="overlay-close"], [aria-label="Close"]').first.click(timeout=1000)
    except Exception:
        pass

    final_text = visible_text(page)
    confirmed = delivers_to(final_text, city)
    note += " - city confirmed" if confirmed else " - city NOT confirmed"
    return final_text, note, confirmed


def read_pickup_modal(page, city):
    """Open the pickup-store popup (the blue store link under 'Pick up in
    store'), choose the city if the popup has a city dropdown, and return
    (popup_text, note). Best-effort: always logs what it saw."""
    try:
        opened = page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('a,button'));
                const link = els.find(e => /apple\\s+[A-Z]/.test((e.innerText||'').trim()) &&
                    /pick[\\s-]*up|unavailable/i.test((e.closest('div')?.parentElement?.innerText)||'') &&
                    e.offsetParent !== null && !/today at apple/i.test(e.innerText));
                if (!link) return false;
                link.click();
                return true;
            }"""
        )
    except Exception as e:
        return "", f"could not open ({type(e).__name__})"
    if not opened:
        return "", "store link NOT found"
    page.wait_for_timeout(1500)
    note = "popup opened"
    try:
        sel = page.locator('[role="dialog"] select, .rf-overlay select, select[name*="city" i]').first
        if sel.count() > 0:
            sel.select_option(label=city, timeout=2000)
            page.wait_for_timeout(2000)
            note += f", city set to {city}"
        else:
            note += ", no city dropdown found"
    except Exception as e:
        note += f", city select failed ({type(e).__name__})"
    try:
        text = page.evaluate(
            """() => {
                const d = document.querySelector('[role="dialog"], .rf-overlay, .modal');
                return (d || document.body).innerText;
            }"""
        )
    except Exception:
        text = ""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass
    return text, note


def matching_modal_lines(text):
    """Store cards in the popup read like 'Available Today' / 'Available
    Tomorrow' (or a date) next to a store name. Skip 'unavailable'."""
    if not text:
        return []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    hits = []
    for i, line in enumerate(lines):
        low = line.lower()
        if "unavailable" in low or "not available" in low:
            continue
        if not re.search(r"\bavailable\b|\bin-store pickup\b|pick[\s-]*up", low):
            continue
        if "any" in TARGET_DATES:
            ok = re.search(r"\btoday\b|\btomorrow\b|\b\d{1,2}\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", low)
        else:
            ok = any(k in low for k in TARGET_DATES if not k.isdigit()) or re.search(
                rf"\b({'|'.join(re.escape(d) for d in TARGET_DATES if d.isdigit()) or 'x^'})\b.{{0,10}}{TARGET_MONTH}", low)
        if not ok:
            continue
        store = next((lines[j] for j in range(i - 1, max(i - 4, -1), -1) if "apple" in lines[j].lower() or "mall" in lines[j].lower()), "")
        hits.append(("TODAY" if "today" in low else "AVAILABLE", f"{store} - {line}" if store else line))
    return hits


def pickup_snippet(text):
    """The pickup-related lines on the page, for the log."""
    if not text:
        return "(no text)"
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = []
    for i, l in enumerate(lines):
        if re.search(r"pick[\s-]*up", l, re.IGNORECASE):
            out.append(l)
            if i + 1 < len(lines):
                out.append(lines[i + 1])
    seen, res = set(), []
    for l in out:
        if l not in seen:
            seen.add(l)
            res.append(l)
    if not res:
        return "(no pickup lines on page)"
    return " | ".join(res[:6])[:300]


def matching_pickup_lines(text):
    """Return lines confirming in-store PICKUP availability.

    TARGET_DATES = any  -> any real pickup line (Today, Tomorrow, dated),
                           ignoring "unavailable" messages.
    otherwise           -> only the listed keywords/dates.
    Delivery lines are always ignored.
    """
    if not text:
        return []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    hits = []
    keyword_targets = [d for d in TARGET_DATES if not d.isdigit() and d != "any"]
    day_number_targets = [d for d in TARGET_DATES if d.isdigit()]
    date_pattern = re.compile(
        rf"\b({'|'.join(re.escape(d) for d in day_number_targets)})\b.{{0,10}}\b{TARGET_MONTH}\w*\b"
        rf"|\b{TARGET_MONTH}\w*\b.{{0,10}}\b({'|'.join(re.escape(d) for d in day_number_targets)})\b",
        re.IGNORECASE,
    ) if day_number_targets else None

    for i, line in enumerate(lines):
        low = line.lower()
        if "deliver" in low:
            continue
        prev_low = lines[i - 1].lower() if i > 0 else ""
        if not (re.search(r"pick[\s-]*up", low) or re.search(r"pick[\s-]*up", prev_low)):
            continue  # not a pickup line (e.g. Apple's "Today at Apple" footer link)

        if "any" in TARGET_DATES:
            if re.search(r"unavailable|not available", low):
                continue
            # needs a real store or date, not just the bare "Pick up in store:" label
            if not re.search(
                r"\bat apple\b|\btoday\b|\btomorrow\b|\b\d{1,2}\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                low,
            ):
                continue
            context = lines[i - 1] if i > 0 and "apple" in lines[i - 1].lower() else ""
            hits.append(("AVAILABLE", f"{context} - {line}" if context else line))
            continue

        matched_keyword = next((k for k in keyword_targets if k in low), None)
        is_target_date = bool(date_pattern and date_pattern.search(low))
        if not (matched_keyword or is_target_date):
            continue
        context = lines[i - 1] if i > 0 and "apple" in lines[i - 1].lower() else ""
        line_out = f"{context} - {line}" if context else line
        tag = "TODAY" if matched_keyword == "today" else ("TARGET DATE" if is_target_date else matched_keyword.upper())
        hits.append((tag, line_out))
    return hits


def poll_once(page):
    results = []
    seen = set()

    for capacity in CAPACITY_KEYWORDS:
        t0 = time.time()
        page.goto(MODEL_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
        print(f"  [{capacity}] page loaded ({time.time()-t0:.1f}s)", flush=True)
        page_snippet = visible_text(page)[:300].replace("\n", " | ")

        if "captcha" in visible_text(page).lower():
            print(f"  [{capacity}] CAPTCHA detected - skipping", flush=True)
            continue

        click_option_matching(page, SCREEN_KEYWORD)

        color_radios = page.locator('input[data-autom^="dimensionColor"]')
        count = color_radios.count()
        print(f"  [{capacity}] found {count} color option(s)", flush=True)

        for i in range(count):
            radio = color_radios.nth(i)
            color_attr = radio.get_attribute("data-autom") or ""
            color_name = color_attr.replace("dimensionColor", "") or f"color{i}"
            try:
                click_via_label(page, radio)
                page.wait_for_timeout(800)
            except Exception:
                print(f"  [{capacity}/{color_name}] could not click color - skipping", flush=True)
                continue

            capacity_ok = click_option_matching(page, capacity)
            if not capacity_ok:
                print(f"  [{capacity}/{color_name}] could not select this capacity - skipping", flush=True)
                if not _debug_sent[0]:
                    try:
                        shot_path = f"debug-{capacity}-{cycle_screenshot_tag()}.png"
                        page.screenshot(path=shot_path, full_page=True)
                        send_email(
                            f"DEBUG - could not select capacity {capacity}",
                            f"Page text snippet:\n{page_snippet}\n\nScreenshot attached.",
                            attachment_path=shot_path,
                        )
                        _debug_sent[0] = True
                    except Exception as e:
                        print(f"  could not email debug screenshot: {e}", flush=True)
                continue

            # Apple requires an explicit no-trade-in and no-AppleCare choice
            # before the pickup dates appear.
            click_option_matching(page, "noTradeIn", "No trade-in")
            click_option_matching(page, "noapplecare", "No AppleCare")
            page.wait_for_timeout(500)

            # Step 1: the pickup line on the page itself (your default store).
            page_text = visible_text(page)
            print(f"    [{capacity}/{color_name}] page pickup lines: {pickup_snippet(page_text)}", flush=True)
            for tag, line in matching_pickup_lines(page_text):
                key = (capacity, color_name, "default store", line)
                if key not in seen:
                    seen.add(key)
                    results.append((capacity, color_name, "default store", tag, line))

            # Step 2: open the blue store link -> "iPhone Availability" popup,
            # and read every store in each configured city.
            for city in CITIES:
                tc = time.time()
                modal_text, note = read_pickup_modal(page, city)
                print(f"    [{capacity}/{color_name}/{city}] modal: {note} ({time.time()-tc:.1f}s)", flush=True)
                print(f"      modal text: {modal_text[:350].replace(chr(10), ' | ')}", flush=True)
                for tag, line in matching_modal_lines(modal_text):
                    key = (capacity, color_name, city, line)
                    if key not in seen:
                        seen.add(key)
                        results.append((capacity, color_name, city, tag, line))

        print(f"  [{capacity}] done ({time.time()-t0:.1f}s total)", flush=True)

    return results


def main():
    print(
        f"Config: url={MODEL_URL} capacities={CAPACITY_KEYWORDS} cities={CITIES} "
        f"target_dates={TARGET_DATES} month={TARGET_MONTH} rest={REST_SECONDS}s",
        flush=True,
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=SHIFT_MINUTES)
    already_alerted = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        cycle_num = 0
        while datetime.now(timezone.utc) < deadline:
            cycle_num += 1
            cycle_start = time.time()
            cycle_results = []
            try:
                for capacity, color, city, tag, line in poll_once(page):
                    cycle_results.append((capacity, color, city, tag, line))
                    key = (capacity, color, city, line[:80])
                    if key not in already_alerted:
                        already_alerted.add(key)
                        priority = "TODAY - " if tag == "TODAY" else ""
                        send_email(
                            f"{priority}iPhone pickup available - {capacity} {color} ({city})",
                            f"Capacity: {capacity}\nColor: {color}\nCity: {city}\nMatch: {tag}\n\n{line}\n\n{MODEL_URL}",
                        )
                        print(f"Alerted [{tag}]: {capacity} {color} {city} - {line[:80]}", flush=True)
            except Exception as e:
                print("Error during poll:", e, flush=True)

            if not cycle_results:
                print("No pickup matches this cycle.", flush=True)

            # Every 5th cycle, resend a digest of everything CURRENTLY matching.
            if cycle_num % 5 == 0 and cycle_results:
                digest_lines = "\n\n".join(
                    f"[{tag}] {capacity} {color} ({city})\n{line}" for capacity, color, city, tag, line in cycle_results
                )
                send_email(
                    f"5-cycle refresh - {len(cycle_results)} pickup match(es) still current",
                    f"{digest_lines}\n\n{MODEL_URL}",
                )
                print(f"Sent 5-cycle refresh digest with {len(cycle_results)} result(s)", flush=True)

            elapsed = time.time() - cycle_start
            print(f"Cycle {cycle_num}: full check took {elapsed:.1f}s - resting {REST_SECONDS}s", flush=True)
            time.sleep(REST_SECONDS)

        browser.close()
        print("Shift ended.", flush=True)


if __name__ == "__main__":
    main()
