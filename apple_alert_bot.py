#!/usr/bin/env python3
"""
Apple.ae Stock ALERT Bot - alert only, no purchase.

How it checks (mirrors what you do by hand):
  1. open a configurator page for the phone (Pro Max, any colour/size)
  2. click the blue store link under "Pick up in store" -> "iPhone Availability" popup
  3. inside the popup, switch Finish + Capacity and read every store's status:
       "Available Today" / "Available Tomorrow" / "Currently unavailable"
  4. email you for stores in your chosen cities that are available

TARGET_DATES: any            -> any "Available ..." store (Today, Tomorrow, a date)
              today,tomorrow -> only those words (day numbers also work with TARGET_MONTH)
The log prints the popup text for every check so a silent run can be diagnosed.
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
ENTRY_URL = os.environ.get("ENTRY_URL") or ""
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


def entry_url():
    """A configurator page that already has a model/colour/size chosen, so the
    'Pick up in store' store link exists (the bare model page has none)."""
    if ENTRY_URL:
        return ENTRY_URL
    if "inch-display" in MODEL_URL:
        return MODEL_URL
    slug = SCREEN_KEYWORD.replace("_", ".").replace("inch", "-inch-display")  # 6_9inch -> 6.9-inch-display
    cap = CAPACITY_KEYWORDS[0] if CAPACITY_KEYWORDS else "256gb"
    return f"{MODEL_URL.rstrip('/')}/{slug}-{cap}-glacier"


def pick_radio(page, name, value):
    """Click the visible label of a radio (inputs are invisible) by name+value."""
    return page.evaluate(
        """([name, value]) => {
            const i = document.querySelector(`input[name="${name}"][value="${value}"]`);
            if (!i) return false;
            const l = document.querySelector(`label[for="${i.id}"]`);
            (l || i).click();
            return true;
        }""",
        [name, value],
    )


def open_availability_popup(page):
    """Select no-trade-in / no-AppleCare (harmless if absent), then click the
    blue store link. Returns True if the 'iPhone Availability' popup is open."""
    for autom in ("choose-noTradeIn", "noapplecare"):
        try:
            page.evaluate(
                """(a) => { const i = document.querySelector(`input[data-autom="${a}"]`);
                    if (!i) return; const l = document.querySelector(`label[for="${i.id}"]`); (l || i).click(); }""",
                autom,
            )
            page.wait_for_timeout(700)
        except Exception:
            pass
    opened = page.evaluate(
        """() => {
            const b = Array.from(document.querySelectorAll('.rf-pickup-quote-overlay-trigger'))
                .find(e => e.offsetParent !== null);
            if (!b) return false;
            b.click();
            return true;
        }"""
    )
    if not opened:
        return False
    try:
        page.wait_for_function("document.body.innerText.includes('iPhone Availability')", timeout=8000)
    except Exception:
        return False
    return True


def popup_options(page, name):
    return page.evaluate(
        """(name) => Array.from(document.querySelectorAll(`input[name="${name}"]`)).map(i => i.value)""",
        name,
    )


def set_city(page, city):
    return page.evaluate(
        """(city) => {
            const s = Array.from(document.querySelectorAll('select')).find(
                x => Array.from(x.options).some(o => o.text.trim() === city));
            if (!s) return false;
            s.value = Array.from(s.options).find(o => o.text.trim() === city).value;
            s.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""",
        city,
    )


def popup_text(page):
    return visible_text(page)


def parse_stores(text):
    """Store cards look like:
         Apple Mall of the Emirates / Dubai / 13.14 km / Available Today / In-Store Pickup
    Returns [(store, city, status)]."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = []
    for i, line in enumerate(lines):
        if re.fullmatch(r"(available\b.*|currently unavailable|unavailable|not available.*)", line, re.IGNORECASE):
            store = lines[i - 3] if i >= 3 else ""
            city = lines[i - 2] if i >= 2 else ""
            if re.search(r"\bkm\b", city, re.IGNORECASE):  # layout without city line
                store, city = lines[i - 2], ""
            out.append((store, city, line))
    return out


def status_matches(status):
    low = status.lower()
    if "unavailable" in low or "not available" in low:
        return None
    if not low.startswith("available"):
        return None
    if "any" in TARGET_DATES:
        return "TODAY" if "today" in low else "AVAILABLE"
    for k in TARGET_DATES:
        if not k.isdigit() and k in low:
            return "TODAY" if k == "today" else k.upper()
    nums = [d for d in TARGET_DATES if d.isdigit()]
    if nums and re.search(rf"\b({'|'.join(nums)})\b.{{0,10}}{TARGET_MONTH}|{TARGET_MONTH}\w*.{{0,10}}\b({'|'.join(nums)})\b", low):
        return "TARGET DATE"
    return None


def poll_once(page):
    results, seen = [], set()
    t0 = time.time()
    url = entry_url()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    if "captcha" in visible_text(page).lower():
        print("  CAPTCHA detected - skipping this cycle", flush=True)
        return results
    print(f"  entry page loaded ({time.time()-t0:.1f}s): {url}", flush=True)

    if not open_availability_popup(page):
        txt = visible_text(page)
        idx = max(txt.lower().find("delivers to"), 0)
        print(f"  popup NOT opened. page near 'delivers to': {txt[idx:idx+200]!r}", flush=True)
        if not _debug_sent[0]:
            try:
                shot = f"debug-popup-{cycle_screenshot_tag()}.png"
                page.screenshot(path=shot, full_page=True)
                send_email("DEBUG - could not open the pickup popup",
                           f"URL: {url}\n\nPage text near delivery:\n{txt[idx:idx+400]}", attachment_path=shot)
                _debug_sent[0] = True
            except Exception as e:
                print(f"  could not email debug screenshot: {e}", flush=True)
        return results
    print("  popup opened", flush=True)

    colours = popup_options(page, "pl_dimensionColor")
    caps_available = popup_options(page, "pl_dimensionCapacity")
    print(f"  popup colours={colours} capacities={caps_available}", flush=True)
    pick_radio(page, "pl_dimensionScreensize", SCREEN_KEYWORD)
    page.wait_for_timeout(1200)
    colours = popup_options(page, "pl_dimensionColor") or colours

    for cap in CAPACITY_KEYWORDS:
        if cap not in caps_available:
            print(f"  capacity {cap} not offered - skipping", flush=True)
            continue
        for colour in colours:
            pick_radio(page, "pl_dimensionColor", colour)
            pick_radio(page, "pl_dimensionCapacity", cap)
            want = f"{cap} {colour}".lower()
            try:
                page.wait_for_function(
                    "(w) => document.body.innerText.toLowerCase().includes(w)", arg=want, timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            text = popup_text(page)
            stores = parse_stores(text)
            summary = "; ".join(f"{s} [{c}] {st}" for s, c, st in stores) or "(no store cards found)"
            print(f"  [{cap}/{colour}] {summary[:400]}", flush=True)
            for store, city, status in stores:
                tag = status_matches(status)
                if not tag:
                    continue
                if CITIES and city and not any(c.lower() == city.lower() for c in CITIES):
                    continue
                line = f"{store} ({city}) - {status}"
                key = (cap, colour, store, status)
                if key not in seen:
                    seen.add(key)
                    results.append((cap, colour, city or "?", tag, line))
    print(f"  check done ({time.time()-t0:.1f}s total)", flush=True)
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
                            f"Capacity: {capacity}\nColor: {color}\nCity: {city}\nMatch: {tag}\n\n{line}\n\n{entry_url()}",
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
                    f"{digest_lines}\n\n{entry_url()}",
                )
                print(f"Sent 5-cycle refresh digest with {len(cycle_results)} result(s)", flush=True)

            elapsed = time.time() - cycle_start
            print(f"Cycle {cycle_num}: full check took {elapsed:.1f}s - resting {REST_SECONDS}s", flush=True)
            time.sleep(REST_SECONDS)

        browser.close()
        print("Shift ended.", flush=True)


if __name__ == "__main__":
    main()
