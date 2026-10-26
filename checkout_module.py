#!/usr/bin/env python3
"""
Duo Watch checkout assist (guarded, guest checkout, PC only).

When a restock alert fires, this drives a VISIBLE browser through Apple's guest checkout
as far as the payment step, then hands it to you:
  - pulls the window to the front of your screen,
  - picks "Credit or Debit Card" and puts the cursor in the first empty card field,
  - beeps until you take over (or it leaves the payment page).
You type the card details / CVV and click "Place Your Order" yourself. The script never
enters payment details and never clicks the purchase button (FORBIDDEN_CLICK, enforced
in code).

Flow:  product page → No AppleCare → Add to Bag → Bag → Check Out → Continue as Guest →
       "I'll pick it up" → store ZIP → store → time slot → pickup contact → payment → YOU.

Why guest: Apple's sign-in never completes in a script-driven browser (it stalls after
2-factor), so signed-in checkout isn't possible. This project doesn't work around that.

Setup (once):
    pip install playwright
    python -m playwright install chromium
    copy checkout.local.example.json checkout.local.json     # fill in your pickup contact
    python checkout_module.py --setup      # optional: save your card in this browser's own
                                           # Chrome autofill so the card number fills in one click
Practice (needs something that's actually in stock for pickup, e.g. a cable):
    python checkout_module.py --rehearse --url "<apple.com accessory URL>" --store-zip 07078 --store-name "Short Hills"
    Goes all the way to payment, then stops. Nothing is bought. Remove the item from the bag after.

local_runner.py launches this automatically in its own window when
"checkout_assist": true is in config.local.json (one session at a time).

Private files (gitignored): checkout.local.json (pickup contact), .checkout_profile/ (this
browser's profile), checkout_debug/ (screenshots when a step fails).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, ".checkout_profile")
DEBUG_DIR = os.path.join(HERE, "checkout_debug")
LOCK_FILE = os.path.join(HERE, "checkout.lock")
CONFIG_FILE = os.path.join(HERE, "checkout.local.json")

BAG_URL = "https://www.apple.com/shop/bag"
STEP_TIMEOUT_MS = 20_000

# Hard guard: the script refuses to click anything whose text matches this.
FORBIDDEN_CLICK = re.compile(r"place (your )?order|pay now|buy now|complete purchase|submit order|confirm (order|purchase)", re.I)
PAYMENT_PAGE = re.compile(r"billing|payment", re.I)
QUEUE_PAGE = re.compile(r"you('|’)re in line|waiting room|high demand|please wait", re.I)
# Card fields in the order you'd fill them; the cursor goes to the first empty one.
CARD_FIELDS = (r"card number", r"expir|mm ?/ ?yy", r"security code|cvv|cvc")

DEFAULT_CONFIG = {
    "contact": {"first_name": "", "last_name": "", "email": "", "phone": ""},
    "browser_channel": "chrome",    # your installed Chrome; "" = Playwright's Chromium, or "msedge"
    "beep_seconds": 120,            # keep beeping this long at the payment step
    "close_on_failure": False,      # False keeps the window open so you can take over
}


class StepFailed(Exception):
    def __init__(self, step: str, detail: str = ""):
        super().__init__(f"{step}: {detail}" if detail else step)
        self.step = step


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG and k != "contact"})
        cfg["contact"].update(user.get("contact") or {})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def alarm(title: str, message: str, url: str = BAG_URL) -> None:
    """Beep + voice on the PC (reuses the runner's alarm) and a phone push. Never raises."""
    print("\a", end="", flush=True)
    try:
        sys.path.insert(0, HERE)
        sys.path.insert(0, os.path.join(HERE, "checker"))
        import check_stock as cs
        import local_runner as lr
        lr.pc_alarm(f"{title}. {message}")
        base = cs.load_json(os.path.join(HERE, "config.json"), {})
        local = cs.load_json(os.path.join(HERE, "config.local.json"), {})
        settings = cs.load_settings(lr.merge_config(base, local))
        cs.dispatch_alert(settings, title, message, url, urgent=False, priority="high", tags="computer,shopping_cart")
    except Exception as e:  # alarms are best-effort
        log(f"(alarm partly failed: {e})")


def beep() -> None:
    try:
        import winsound
        winsound.Beep(1400, 180)
        winsound.Beep(1000, 180)
    except Exception:
        print("\a", end="", flush=True)


def raise_window(title_part: str) -> bool:
    """Windows: bring the browser window whose title contains `title_part` to the front. Best-effort."""
    if os.name != "nt" or not title_part:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            n = user32.GetWindowTextLengthW(hwnd)
            if n and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title_part.lower() in buf.value.lower():
                    found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(each, 0)
        if not found:
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)                  # SW_RESTORE (un-minimize)
        user32.keybd_event(0x12, 0, 0, 0)           # tap Alt: Windows only lets the
        user32.keybd_event(0x12, 0, 2, 0)           # foreground app hand over focus otherwise
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def acquire_lock(max_age_s: int = 120) -> bool:
    """One session at a time. The lock is refreshed while a window is open."""
    try:
        if os.path.exists(LOCK_FILE) and time.time() - os.path.getmtime(LOCK_FILE) < max_age_s:
            return False
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def touch_lock() -> None:
    try:
        os.utime(LOCK_FILE, None)
    except OSError:
        pass


def release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def safe_click(locator, what: str) -> None:
    """Click, but never anything that would place an order."""
    try:
        text = (locator.inner_text(timeout=2000) or "") + " " + (locator.get_attribute("aria-label") or "")
    except Exception:
        text = ""
    if FORBIDDEN_CLICK.search(text):
        raise StepFailed(what, f"refused to click '{text.strip()[:60]}' (final purchase is yours)")
    locator.click(timeout=STEP_TIMEOUT_MS)


def screenshot(page, step: str) -> str:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', step.lower())}.png")
    try:
        page.screenshot(path=path, full_page=True)
    except Exception:
        pass
    return path


def check_queue(page) -> None:
    try:
        if QUEUE_PAGE.search(page.locator("body").inner_text(timeout=3000)[:4000]):
            raise StepFailed("queue", "Apple is showing a waiting/queue page")
    except StepFailed:
        raise
    except Exception:
        pass


def first_visible(page, candidates, timeout_ms: int = STEP_TIMEOUT_MS):
    """Return the first locator from `candidates` that becomes visible."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for loc in candidates:
            try:
                if loc.first.is_visible():
                    return loc.first
            except Exception:
                pass
        page.wait_for_timeout(250)
    return None


def product_from_url(url: str) -> tuple[str, str]:
    """('512GB', 'Night Sky') from .../7.6-inch-display-512gb-night-sky-unlocked."""
    m = re.search(r"(\d+(?:gb|tb))-([a-z-]+?)(?:-unlocked)?/?$", url.lower())
    if not m:
        return "", ""
    return m.group(1).upper(), m.group(2).replace("-", " ").title()


def duo_lines(page) -> list[str]:
    try:
        return [t.strip() for t in page.locator("h2, h3").filter(has_text=re.compile(r"iPhone Duo", re.I)).all_inner_texts()]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def step_bag_precheck(page, storage: str, color: str) -> bool:
    """Returns True if the right phone is already in the bag (skip adding). Never removes anything."""
    page.goto(BAG_URL, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
    page.wait_for_timeout(800)
    lines = duo_lines(page)
    if not lines:
        return False
    if len(lines) == 1 and storage and color and storage.lower() in lines[0].lower() and color.lower() in lines[0].lower():
        log(f"Already in bag: {lines[0]}")
        return True
    raise StepFailed("already in bag", f"your bag already has: {lines[0]}")


def step_product(page, url: str) -> None:
    log("Opening product page")
    page.goto(url, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
    check_queue(page)
    # The *-unlocked URL preselects color, storage, carrier, no trade-in and full price.
    # Make sure "unlocked" and "No AppleCare coverage" are chosen; both enable Add to Bag.
    for label_re in (r"connect to any carrier later", r"no trade-?in", r"no applecare coverage"):
        lab = page.locator("label").filter(has_text=re.compile(label_re, re.I))
        try:
            if lab.count():
                inp_id = lab.first.get_attribute("for")
                checked = inp_id and page.locator(f"#{inp_id}").is_checked()
                if not checked:
                    lab.first.click(timeout=5000)
        except Exception:
            pass
    btn = first_visible(page, [
        page.locator('button[data-autom="add-to-cart"]:not([disabled]), button[name="add-to-cart"]:not([disabled])'),
        page.get_by_role("button", name=re.compile(r"^\s*add to bag\s*$", re.I)),
    ])
    if not btn:
        raise StepFailed("add to bag", "Add to Bag never became clickable")
    safe_click(btn, "add to bag")
    log("Added to bag")
    first_visible(page, [page.get_by_role("button", name=re.compile(r"review bag", re.I)),
                         page.get_by_role("link", name=re.compile(r"review bag", re.I))], timeout_ms=8000)


def step_bag(page) -> None:
    log("Opening bag")
    page.goto(BAG_URL, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
    check_queue(page)
    page.wait_for_timeout(800)
    n = len(duo_lines(page))
    if n > 1:
        raise StepFailed("bag", f"bag has {n} iPhone Duo items; remove the extras, then check out")
    btn = first_visible(page, [page.locator('button[data-autom="checkout"]'),
                               page.get_by_role("button", name=re.compile(r"^\s*check ?out\s*$", re.I))])
    if not btn:
        raise StepFailed("bag", "Check Out button not found")
    safe_click(btn, "check out")


def step_guest(page) -> None:
    page.wait_for_timeout(1200)
    if "signin" not in page.url.lower():
        return
    log("Continuing as guest")
    guest = first_visible(page, [page.get_by_role("button", name=re.compile(r"continue as guest", re.I)),
                                 page.get_by_text(re.compile(r"continue as guest", re.I))], 10_000)
    if not guest:
        raise StepFailed("guest checkout", "Apple didn't offer 'Continue as Guest'")
    safe_click(guest, "guest checkout")


def step_pickup(page, store_zip: str | None, store_name: str | None) -> None:
    log("Choosing in-store pickup")
    pick = first_visible(page, [page.locator('[data-autom="fulfillment-pickup-button"]'),
                                page.get_by_role("button", name=re.compile(r"pick (it )?up", re.I)),
                                page.get_by_text(re.compile(r"I'?ll pick it up", re.I))])
    if not pick:
        raise StepFailed("pickup", "pickup option not found")
    safe_click(pick, "pickup option")

    if store_zip:
        box = first_visible(page, [page.get_by_role("textbox", name=re.compile(r"zip|location|city", re.I)),
                                   page.locator('input[placeholder*="ZIP" i], input[placeholder*="location" i]')], 10_000)
        if box:
            box.fill(store_zip)
            box.press("Enter")
            page.wait_for_timeout(1500)
    if store_name:
        store = first_visible(page, [page.get_by_text(re.compile(rf"\b{re.escape(store_name)}\b", re.I))], 12_000)
        if not store:
            raise StepFailed("store", f"'{store_name}' not listed (sold out already, or outside the search)")
        safe_click(store, "store")
        page.wait_for_timeout(800)
    # Time slot: pick the first available option if Apple shows a dropdown.
    try:
        sel = page.locator("select").filter(has=page.locator("option")).first
        if sel.count() and sel.is_visible():
            values = sel.locator("option").evaluate_all("els => els.filter(e => !e.disabled && e.value).map(e => e.value)")
            if values:
                sel.select_option(values[0])
    except Exception:
        pass
    cont = first_visible(page, [page.get_by_role("button", name=re.compile(r"continue", re.I))])
    if not cont:
        raise StepFailed("pickup", "Continue button not found after choosing the store")
    safe_click(cont, "continue to pickup details")


def step_contact(page, cfg: dict) -> None:
    page.wait_for_timeout(1200)
    if PAYMENT_PAGE.search(page.url):
        return
    log("Pickup contact")
    c = cfg.get("contact") or {}
    fields = [(r"first name", c.get("first_name")), (r"last name", c.get("last_name")),
              (r"email", c.get("email")), (r"phone", c.get("phone"))]
    for label_re, value in fields:
        if not value:
            continue
        try:
            box = page.get_by_label(re.compile(label_re, re.I)).first
            if box.is_visible() and not box.input_value():
                box.fill(value)
        except Exception:
            pass
    cont = first_visible(page, [page.get_by_role("button", name=re.compile(r"continue to payment|continue", re.I))], 10_000)
    if cont:
        safe_click(cont, "continue to payment")


def wait_for_payment(page) -> bool:
    deadline = time.time() + STEP_TIMEOUT_MS / 1000
    while time.time() < deadline:
        if on_payment_page(page):
            return True
        page.wait_for_timeout(300)
    return False


def on_payment_page(page) -> bool:
    try:
        if PAYMENT_PAGE.search(page.url):
            return True
        return page.get_by_role("heading", name=re.compile(r"how do you want to pay|payment", re.I)).first.is_visible()
    except Exception:
        return False


def first_empty_field(fields) -> int | None:
    """Index of the first field whose value is empty. `fields` = list of current values (None = not found)."""
    for i, v in enumerate(fields):
        if v is not None and not v.strip():
            return i
    return None


def step_hand_over(page) -> str:
    """Front window, pick card payment, cursor in the first empty card field. Returns what got focus."""
    try:
        page.bring_to_front()
        raise_window(page.title())
    except Exception:
        pass
    card = first_visible(page, [page.get_by_label(re.compile(r"credit or debit card", re.I)),
                                page.get_by_text(re.compile(r"^\s*credit or debit card\s*$", re.I))], 5000)
    if card:
        try:
            safe_click(card, "card payment option")
            page.wait_for_timeout(800)
        except StepFailed:
            raise
        except Exception:
            pass
    # Card fields can sit inside iframes; look in every frame.
    boxes, values = [], []
    for label_re in CARD_FIELDS:
        found = None
        for frame in page.frames:
            try:
                loc = frame.get_by_label(re.compile(label_re, re.I)).first
                if loc.is_visible():
                    found = loc
                    break
            except Exception:
                pass
        boxes.append(found)
        try:
            values.append(found.input_value() if found else None)
        except Exception:
            values.append(None)
    i = first_empty_field(values)
    if i is None:
        return "nothing (card fields not found; click into them yourself)"
    try:
        boxes[i].focus()
        return ("card number", "expiration", "security code")[i]
    except Exception:
        return "nothing (couldn't focus the card field)"


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def trigger_checkout(product_url: str, store_zip: str | None = None, store_name: str | None = None,
                     rehearse: bool = False, cfg: dict | None = None) -> str:
    """Run the guarded guest checkout. Returns 'ready', 'busy' or 'failed:<step>'.

    Blocks until you close the browser window (it stays open for you).
    """
    from playwright.sync_api import sync_playwright  # imported here so the runner never needs it

    cfg = cfg or load_config()
    if not acquire_lock():
        log("Another checkout assist is already running; skipping.")
        return "busy"
    result = "failed:start"
    tag = "PRACTICE " if rehearse else ""
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR, headless=False, channel=cfg.get("browser_channel") or None,
                viewport=None, args=["--start-maximized"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            started = time.time()
            storage, color = product_from_url(product_url)
            try:
                if not step_bag_precheck(page, storage, color):
                    step_product(page, product_url)
                step_bag(page)
                step_guest(page)
                step_pickup(page, store_zip, store_name)
                step_contact(page, cfg)
                if not wait_for_payment(page):
                    raise StepFailed("payment", "did not reach the payment step")
                result = "ready"
                focused = step_hand_over(page)
                log(f"READY at payment after {time.time() - started:.0f}s. Cursor in: {focused}.")
                log("Type your card details, then click Place Your Order yourself.")
                alarm(f"{tag}Duo checkout READY on your PC",
                      f"{store_name or 'Store'} pickup is at payment. Type your card and place the order.", product_url)
            except StepFailed as e:
                result = f"failed:{e.step}"
                shot = screenshot(page, e.step)
                log(f"Stopped at '{e.step}': {e}. Screenshot: {shot}")
                alarm(f"{tag}Duo checkout needs you", f"Stuck at {e.step}. Take over in the PC browser or use your phone.", product_url)
                if cfg.get("close_on_failure"):
                    ctx.close()
                    return result
            except Exception as e:  # timeouts, crashes, closed window
                result = "failed:unexpected"
                shot = screenshot(page, "unexpected")
                log(f"Unexpected error: {e}. Screenshot: {shot}")
                alarm(f"{tag}Duo checkout needs you", "Something went wrong. Take over in the PC browser or use your phone.", product_url)
                if cfg.get("close_on_failure"):
                    ctx.close()
                    return result

            # Never auto-close and never touch the bag again: you finish the purchase yourself.
            log("Leaving the browser open. Close the window when you're done.")
            beep_until = time.time() + float(cfg.get("beep_seconds") or 0) if result == "ready" else 0
            while True:
                try:
                    if page.is_closed():
                        break
                    touch_lock()
                    if time.time() < beep_until and on_payment_page(page):
                        beep()
                        page.wait_for_timeout(4000)
                    else:
                        beep_until = 0
                        page.wait_for_timeout(30_000)
                except Exception:
                    break
            try:
                ctx.close()
            except Exception:
                pass
    finally:
        release_lock()
    return result


def setup_profile(cfg: dict) -> None:
    """Optional: open this browser's card settings so Chrome's own autofill can fill the card number."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False,
                                                   channel=cfg.get("browser_channel") or None, viewport=None)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("chrome://settings/payments")
        except Exception:
            pass
        print("Optional: under 'Payment methods' click Add and save your card in THIS browser,")
        print("so at checkout you click the card number box and pick it. Chrome stores it, not this script.")
        print("Close the window when done.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Guarded guest checkout assist (stops at payment)")
    ap.add_argument("--setup", action="store_true", help="Optional: save a card in this browser's Chrome autofill")
    ap.add_argument("--url", help="Product page URL")
    ap.add_argument("--store-zip", default=None)
    ap.add_argument("--store-name", default=None)
    ap.add_argument("--rehearse", action="store_true",
                    help="Practice run with an in-stock item: goes to payment and stops. Nothing is bought.")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.setup:
        setup_profile(cfg)
        return 0
    if not args.url:
        ap.error("--url is required")
    print(trigger_checkout(args.url, args.store_zip, args.store_name, rehearse=args.rehearse, cfg=cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
