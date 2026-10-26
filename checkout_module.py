#!/usr/bin/env python3
"""
Duo Watch checkout assist (guarded).

Drives a VISIBLE browser through Apple's checkout as far as the payment step, then
stops, sounds an alarm and hands control to you. It never enters payment details and
never clicks "Place Your Order" (see FORBIDDEN_CLICK below: that is enforced in code).

Flow:  product page → No AppleCare → Add to Bag → Bag (sanity check) → Check Out →
       sign-in (your saved session) → "I'll pick it up" → store ZIP → store → time slot →
       pickup contact → STOP at payment → alarm → you finish in the open browser.

Setup (once):
    pip install playwright
    python -m playwright install chromium
    python checkout_module.py --setup          # sign in to apple.com in the window, then close it
    python checkout_module.py --rehearse --url "<buy page url>" --store-zip 10017
                                               # practice run: adds the phone to your bag and stops at the
                                               # store search. Remove it from your bag afterwards.

Use from code:
    from checkout_module import trigger_checkout
    trigger_checkout(url, store_zip="10017", store_name="Grand Central")

local_runner.py launches this automatically in its own window when
"checkout_assist": true is in config.local.json (one session at a time).

Private files (gitignored): checkout.local.json (pickup contact), .checkout_profile/ (your
browser session), checkout_debug/ (screenshots when a step fails).
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
SIGNIN_URL = "https://www.apple.com/shop/signIn"
STEP_TIMEOUT_MS = 20_000
HUMAN_WAIT_S = 240          # how long to wait for you to finish a sign-in / 2FA prompt

# Hard guard: the script refuses to click anything whose text matches this.
FORBIDDEN_CLICK = re.compile(r"place (your )?order|pay now|buy now|complete purchase|submit order|confirm (order|purchase)", re.I)
PAYMENT_PAGE = re.compile(r"billing|payment", re.I)
QUEUE_PAGE = re.compile(r"you('|’)re in line|waiting room|high demand|please wait", re.I)

DEFAULT_CONFIG = {
    "guest_checkout": False,        # True: use guest checkout with the contact below
    "contact": {"first_name": "", "last_name": "", "email": "", "phone": ""},
    "browser_channel": "",          # "" = Playwright's Chromium, or "chrome" / "msedge"
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
        cfg.update({k: v for k, v in user.items() if k != "contact"})
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


def acquire_lock(max_age_s: int = 600) -> bool:
    try:
        if os.path.exists(LOCK_FILE) and time.time() - os.path.getmtime(LOCK_FILE) < max_age_s:
            return False
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


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


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

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
    btn = page.locator('button[data-autom="add-to-cart"]:not([disabled]), button[name="add-to-cart"]:not([disabled])')
    try:
        btn.first.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    except Exception:
        raise StepFailed("add to bag", "Add to Bag never became clickable")
    safe_click(btn.first, "add to bag")
    log("Added to bag")
    # Wait for Apple's confirmation (Review Bag button or navigation away from the config page).
    first_visible(page, [page.get_by_role("button", name=re.compile(r"review bag", re.I)),
                         page.get_by_role("link", name=re.compile(r"review bag", re.I))], timeout_ms=8000)


def step_bag(page) -> None:
    log("Opening bag")
    page.goto(BAG_URL, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
    check_queue(page)
    page.wait_for_timeout(800)
    duo_lines = page.locator("h2, h3").filter(has_text=re.compile(r"iPhone Duo", re.I)).count()
    if duo_lines > 1:
        raise StepFailed("bag", f"bag has {duo_lines} iPhone Duo items; remove the extras, then check out")
    btn = first_visible(page, [page.locator('button[data-autom="checkout"]'),
                               page.get_by_role("button", name=re.compile(r"^\s*check ?out\s*$", re.I))])
    if not btn:
        raise StepFailed("bag", "Check Out button not found")
    safe_click(btn, "check out")


def step_signin(page, cfg: dict) -> None:
    page.wait_for_timeout(1200)
    if "signin" not in page.url.lower():
        return
    log("Sign-in step")
    if cfg.get("guest_checkout"):
        guest = first_visible(page, [page.get_by_role("button", name=re.compile(r"continue as guest", re.I))], 6000)
        if guest:
            safe_click(guest, "guest checkout")
            return
    cont = first_visible(page, [page.get_by_role("button", name=re.compile(r"^continue$", re.I))], 4000)
    if cont:
        safe_click(cont, "continue signed in")
        page.wait_for_timeout(1500)
    if "signin" in page.url.lower():
        alarm("Duo checkout: sign in needed", "Finish signing in on the PC window, the script will continue.")
        log(f"Waiting up to {HUMAN_WAIT_S}s for you to sign in")
        deadline = time.time() + HUMAN_WAIT_S
        while "signin" in page.url.lower() and time.time() < deadline:
            page.wait_for_timeout(1000)
        if "signin" in page.url.lower():
            raise StepFailed("sign in", "still on the sign-in page")


def step_pickup(page, store_zip: str | None, store_name: str | None, rehearse: bool) -> None:
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
    if rehearse:
        raise StepFailed("rehearsal stop", "reached store search; stopping as requested")
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
            values = [v for v in sel.locator("option").evaluate_all("els => els.filter(e => !e.disabled && e.value).map(e => e.value)")]
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
        if PAYMENT_PAGE.search(page.url):
            return True
        try:
            if page.get_by_role("heading", name=re.compile(r"how do you want to pay|payment", re.I)).first.is_visible():
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def trigger_checkout(product_url: str, store_zip: str | None = None, store_name: str | None = None,
                     rehearse: bool = False, cfg: dict | None = None) -> str:
    """Run the guarded checkout. Returns 'ready', 'busy' or 'failed:<step>'.

    Blocks until you close the browser window (so the session stays open for you).
    """
    from playwright.sync_api import sync_playwright  # imported here so the runner never needs it

    cfg = cfg or load_config()
    if not acquire_lock():
        log("Another checkout assist is already running; skipping.")
        return "busy"
    result = "failed:start"
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR, headless=False, channel=cfg.get("browser_channel") or None,
                viewport=None, args=["--start-maximized"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            started = time.time()
            try:
                step_product(page, product_url)
                step_bag(page)
                step_signin(page, cfg)
                step_pickup(page, store_zip, store_name, rehearse)
                step_contact(page, cfg)
                if not wait_for_payment(page):
                    raise StepFailed("payment", "did not reach the payment step")
                result = "ready"
                took = time.time() - started
                log(f"READY at payment after {took:.0f}s. Enter payment and place the order yourself.")
                alarm("Duo checkout READY on your PC",
                      f"{store_name or 'Store'} pickup is at the payment step. Finish it now on the PC.", product_url)
            except StepFailed as e:
                result = f"failed:{e.step}"
                shot = screenshot(page, e.step)
                log(f"Stopped at '{e.step}': {e}. Screenshot: {shot}")
                if e.step != "rehearsal stop":
                    alarm("Duo checkout needs you", f"Stuck at {e.step}. Take over in the PC browser or use your phone.", product_url)
                if cfg.get("close_on_failure") and e.step != "rehearsal stop":
                    ctx.close()
                    return result
            except Exception as e:  # timeouts, crashes, closed window
                result = "failed:unexpected"
                shot = screenshot(page, "unexpected")
                log(f"Unexpected error: {e}. Screenshot: {shot}")
                alarm("Duo checkout needs you", "Something went wrong. Take over in the PC browser or use your phone.", product_url)
                if cfg.get("close_on_failure"):
                    ctx.close()
                    return result
            log("Leaving the browser open for you. Close the window when you're done.")
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
    finally:
        release_lock()
    return result


def setup_profile(cfg: dict) -> None:
    """Open the persistent browser so you can sign in to apple.com once."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False,
                                                   channel=cfg.get("browser_channel") or None, viewport=None)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SIGNIN_URL)
        print("Sign in to your Apple Account in this window (tick 'Remember me' / trust this browser),")
        print("then close the window. Your session is saved in .checkout_profile/.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Guarded checkout assist (stops before payment)")
    ap.add_argument("--setup", action="store_true", help="Sign in once and save the browser session")
    ap.add_argument("--url", help="Product buy-page URL (use the *-unlocked links from config.json)")
    ap.add_argument("--store-zip", default=None)
    ap.add_argument("--store-name", default=None)
    ap.add_argument("--rehearse", action="store_true", help="Stop at the store search (for testing; empty your bag after)")
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
