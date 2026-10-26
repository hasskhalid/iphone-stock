#!/usr/bin/env python3
"""
iPhone pickup-stock checker.

Asks Apple's store pickup endpoint which Apple Stores within a radius of a ZIP
code have each configured iPhone model available, writes a status.json snapshot
plus a history.json log of restock / sell-out events for the dashboard, and
sends an urgent phone push (via ntfy) whenever a model becomes available at a
store where it wasn't available on the previous check.

Standard library only, so it runs anywhere Python 3.9+ is installed.

Usage:
  python checker/check_stock.py --config config.json \
      --prev prev_status.json --history-in prev_history.json \
      --out status.json --history-out history.json
  python checker/check_stock.py --fixture tests/fixtures/live_sample_nyc.json --dry-run
  python checker/check_stock.py --test-alert          # send a test push and exit

Environment variables:
  APPLE_ZIP   ZIP code to search around (overrides config "zip")
  NTFY_TOPIC  ntfy topic name to push alerts to (overrides config "ntfy_topic")
  NTFY_SERVER ntfy server base URL (default https://ntfy.sh)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

APPLE_URL = "https://www.apple.com/shop/retail/pickup-message"
BUY_URL = "https://www.apple.com/shop/buy-iphone"
BAG_URL = "https://www.apple.com/shop/bag"
HISTORY_LIMIT = 500
PC_OFFLINE_SECONDS = 900   # backup warns once if the PC runner has been silent this long
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BUY_URL,
}


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def build_url(parts: list[str], zip_code: str) -> str:
    params: list[tuple[str, str]] = [("pl", "true")]
    for i, part in enumerate(parts):
        params.append((f"parts.{i}", part))
    params.append(("location", zip_code))
    return f"{APPLE_URL}?{urllib.parse.urlencode(params)}"


def fetch_apple(parts: list[str], zip_code: str, retries: int = 2, timeout: int = 20) -> dict:
    url = build_url(parts, zip_code)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Apple request failed: {last_err}")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def store_miles(store: dict) -> float | None:
    """Distance in miles from Apple's numeric field, or parsed from '4.2 mi'."""
    d = store.get("storedistance")
    if isinstance(d, (int, float)):
        return float(d)
    text = str(store.get("storeDistanceWithUnit") or "").split()
    try:
        return float(text[0]) if text else None
    except ValueError:
        return None


def _https(url: str | None) -> str:
    if not url:
        return ""
    return "https://" + url.split("://", 1)[1] if url.startswith("http://") else url


def store_details(store: dict) -> dict:
    addr = store.get("address") or {}
    street = addr.get("address2") or ""
    city = store.get("city") or ""
    state = store.get("state") or ""
    postal = addr.get("postalCode") or ""
    line = ", ".join(x for x in (street, city, f"{state} {postal}".strip()) if x)
    query = urllib.parse.quote_plus(f"Apple {store.get('storeName', '')} {line}")
    return {
        "address": line,
        "zip": postal.split("-")[0],
        "phone": store.get("phoneNumber") or "",
        "store_url": _https(store.get("reservationUrl") or store.get("directionsUrl")),
        "maps_url": f"https://www.google.com/maps/search/?api=1&query={query}",
    }


def parse_response(data: dict, models: list[dict], max_stores: int,
                   radius_miles: float | None = None) -> list[dict]:
    """Turn Apple's response into one entry per configured model.

    Handles the current retail/pickup-message shape (body.stores) and the older
    fulfillment-messages shape (body.content.pickupMessage.stores).
    """
    body = data.get("body") or {}
    legacy = (body.get("content") or {}).get("pickupMessage") or {}
    error = body.get("errorMessage") or legacy.get("errorMessage")
    if error:
        raise RuntimeError(f"Apple returned an error: {error}")
    stores = body.get("stores") or legacy.get("stores") or []
    if radius_miles:
        stores = [s for s in stores if (store_miles(s) is None or store_miles(s) <= radius_miles)]
    stores = stores[:max_stores]

    results = []
    for model in models:
        part = model["part"]
        store_rows = []
        for store in stores:
            avail = (store.get("partsAvailability") or {}).get(part)
            if not avail:
                continue
            display = (avail.get("pickupDisplay") or "").lower()
            row = {
                "id": str(store.get("storeNumber", "")),
                "name": store.get("storeName", "Apple Store"),
                "city": ", ".join(x for x in (store.get("city"), store.get("state")) if x),
                "distance": store.get("storeDistanceWithUnit", ""),
                "miles": store_miles(store),
                "available": display == "available",
                "quote": avail.get("pickupSearchQuote") or "",
            }
            row.update(store_details(store))
            store_rows.append(row)
        results.append({
            "part": part,
            "label": model.get("label", part),
            "color": model.get("color", ""),
            "url": model.get("url") or BUY_URL,
            "any_available": any(s["available"] for s in store_rows),
            "stores": store_rows,
        })
    return results


# --------------------------------------------------------------------------
# Change detection, history + alerts
# --------------------------------------------------------------------------

def available_set(status: dict) -> set[tuple[str, str]]:
    out = set()
    for m in status.get("models", []) or []:
        for s in m.get("stores", []) or []:
            if s.get("available"):
                out.add((m.get("part", ""), s.get("id", "")))
    return out


def new_availability(prev: dict, models: list[dict]) -> dict[str, list[dict]]:
    """Return {part: [store rows]} for stores that just became available."""
    before = available_set(prev)
    fresh: dict[str, list[dict]] = {}
    for m in models:
        for s in m["stores"]:
            if s["available"] and (m["part"], s["id"]) not in before:
                fresh.setdefault(m["part"], []).append(s)
    return fresh


def change_events(prev: dict, models: list[dict], when: str) -> list[dict]:
    """Restock ('in') and sell-out ('out') events between two checks."""
    before = available_set(prev)
    now = {(m["part"], s["id"]) for m in models for s in m["stores"] if s["available"]}
    lookup = {(m["part"], s["id"]): (m, s) for m in models for s in m["stores"]}
    prev_lookup = {
        (m.get("part"), s.get("id")): (m, s)
        for m in prev.get("models", []) or [] for s in m.get("stores", []) or []
    }
    events = []
    for key in sorted(now - before):
        m, s = lookup[key]
        events.append({"t": when, "kind": "in", "part": m["part"], "label": m["label"],
                       "color": m.get("color", ""), "store_id": s["id"], "store": s["name"],
                       "quote": s.get("quote", "")})
    for key in sorted(before - now):
        m, s = lookup.get(key) or prev_lookup.get(key) or ({}, {})
        events.append({"t": when, "kind": "out", "part": key[0], "label": m.get("label", key[0]),
                       "color": m.get("color", ""), "store_id": key[1],
                       "store": s.get("name", key[1]), "quote": ""})
    return events


def _ascii(text: str) -> str:
    return text.encode("ascii", "ignore").decode("ascii")


def alert_actions(buy_url: str) -> str:
    """ntfy action buttons. The buy page is a universal link the Apple Store app opens;
    /shop/bag is not (Apple's app-site-association only claims shared bags), so the bag
    button opens Safari and is the backup."""
    return f"view, Apple Store app, {buy_url}; view, Bag in Safari, {BAG_URL}"


def send_ntfy(topic: str, title: str, message: str, server: str, dry_run: bool,
              click: str = BUY_URL, priority: str = "urgent", tags: str = "iphone,rotating_light",
              actions: str | None = None) -> None:
    if dry_run:
        print(f"[dry-run] push -> {title}\n{message}\n(click: {click})\n")
        return
    req = urllib.request.Request(
        f"{server.rstrip('/')}/{urllib.parse.quote(topic)}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": _ascii(title),
            "Priority": priority,
            "Tags": tags,
            "Click": click,
            **({"Actions": _ascii(actions)} if actions else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def send_telegram(token: str, chat_id: str, title: str, message: str, url: str) -> None:
    body = json.dumps({
        "chat_id": chat_id, "text": f"{title}\n{message}", "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[{"text": "Open Apple Store", "url": url}]]},
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def send_pushover(token: str, user: str, title: str, message: str, url: str, urgent: bool) -> None:
    fields = {"token": token, "user": user, "title": title[:250], "message": message[:1000],
              "url": url, "url_title": "Open Apple Store", "sound": "siren" if urgent else "pushover"}
    if urgent:   # emergency priority: repeats every 30s until you acknowledge (max 10 min)
        fields.update(priority="2", retry="30", expire="600")
    else:
        fields["priority"] = "1"
    req = urllib.request.Request("https://api.pushover.net/1/messages.json",
                                 data=urllib.parse.urlencode(fields).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def dispatch_alert(settings: dict, title: str, message: str, click: str, *, dry_run: bool = False,
                   urgent: bool = True, actions: str | None = None, priority: str = "urgent",
                   tags: str = "iphone,rotating_light") -> list[str]:
    """Send one alert to every configured channel at the same time. Returns error strings."""
    import threading
    jobs = []
    if settings.get("topic") or dry_run:
        jobs.append(("ntfy", lambda: send_ntfy(settings.get("topic", ""), title, message, settings.get("server", "https://ntfy.sh"),
                                               dry_run, click=click, actions=actions, priority=priority, tags=tags)))
    if not dry_run and settings.get("telegram_token") and settings.get("telegram_chat"):
        jobs.append(("telegram", lambda: send_telegram(settings["telegram_token"], settings["telegram_chat"], title, message, click)))
    if not dry_run and settings.get("pushover_token") and settings.get("pushover_user"):
        jobs.append(("pushover", lambda: send_pushover(settings["pushover_token"], settings["pushover_user"], title, message, click, urgent)))
    errors: list[str] = []

    def run(name, fn):
        try:
            fn()
        except Exception as e:
            errors.append(f"{name}: {e}")

    threads = [threading.Thread(target=run, args=job, daemon=True) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)
    return errors


def color_of(m: dict) -> str:
    return m.get("color") or m.get("label", "").replace("iPhone Duo 256GB", "").strip() or m.get("part", "")


def store_line(s: dict) -> str:
    """'Grand Central · ZIP 10017' (falls back to city when Apple gave no ZIP)."""
    return f"{s['name']} · ZIP {s['zip']}" if s.get("zip") else f"{s['name']} · {s.get('city', '')}".rstrip(" ·")


def build_alerts(fresh: dict[str, list[dict]], models: list[dict]) -> list[tuple[str, str, str]]:
    """Short, glanceable alerts: COLOR @ STORE in the title, ZIP first in the body."""
    by_part = {m["part"]: m for m in models}
    alerts = []
    for part, stores in fresh.items():
        m = by_part.get(part, {})
        color, first = color_of(m).upper(), stores[0]
        more = f" +{len(stores) - 1}" if len(stores) > 1 else ""
        title = f"{color} @ {first['name']}{more}"
        lines = [store_line(s) for s in stores]
        zip_hint = f"ZIP {first['zip']}" if first.get("zip") else first["name"]
        lines.append(f"Bag → Check Out → pickup → type {zip_hint} → Apple Pay")
        alerts.append((title, "\n".join(lines), m.get("url") or BUY_URL))
    return alerts


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_json(path: str | None, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def load_settings(config: dict) -> dict:
    """Resolve settings from a config dict plus APPLE_ZIP / NTFY_TOPIC env vars."""
    radius = config.get("radius_miles")
    return {
        "models": [m for m in config.get("models", []) if m.get("part") and "REPLACE" not in m["part"]],
        "zip": os.environ.get("APPLE_ZIP") or str(config.get("zip", "")),
        "max_stores": int(config.get("max_stores", 12)),
        "radius": float(radius) if radius else None,
        "topic": (os.environ.get("NTFY_TOPIC") or str(config.get("ntfy_topic", ""))).strip(),
        "server": os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
        "alert_click": str(config.get("alert_click", "product")),   # "product" (opens app) or "bag" (Safari)
        # Optional extra channels that deliver to iPhone faster than ntfy (all fire in parallel).
        "telegram_token": (os.environ.get("TELEGRAM_BOT_TOKEN") or str(config.get("telegram_bot_token", ""))).strip(),
        "telegram_chat": (os.environ.get("TELEGRAM_CHAT_ID") or str(config.get("telegram_chat_id", ""))).strip(),
        "pushover_token": (os.environ.get("PUSHOVER_TOKEN") or str(config.get("pushover_token", ""))).strip(),
        "pushover_user": (os.environ.get("PUSHOVER_USER") or str(config.get("pushover_user", ""))).strip(),
    }


def seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def check_once(settings: dict, prev: dict, history: dict, *, source: str = "cli",
               fixture: str | None = None, dry_run: bool = False, log=print) -> tuple[dict, dict, list]:
    """One full check: fetch, parse, log history events, send alerts.

    Returns (status, history, alerts) where alerts is a list of (title, message, click).
    Never raises; failures are reported in status["error"] with the last good data kept.
    """
    if not isinstance(history, dict) or not isinstance(history.get("events"), list):
        history = {"events": []}
    models = settings["models"]
    status = {
        "source": source,
        "radius_miles": settings["radius"],
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": None,
        "models": [],
    }
    alerts: list = []
    try:
        if not models:
            raise RuntimeError("No models configured. Add part numbers to config.json.")
        if not fixture and not settings["zip"]:
            raise RuntimeError("No ZIP code. Set the APPLE_ZIP secret or 'zip' in config.json.")
        data = load_json(fixture, None) if fixture else fetch_apple([m["part"] for m in models], settings["zip"])
        if data is None:
            raise RuntimeError(f"Could not read fixture {fixture}")
        status["models"] = parse_response(data, models, settings["max_stores"], settings["radius"])
    except Exception as e:  # keep the last good data visible on the dashboard
        status["error"] = str(e)
        status["models"] = prev.get("models", [])
        status["last_success_at"] = prev.get("last_success_at") or prev.get("checked_at")
        log(f"ERROR: {e}")
        return status, history, alerts

    status["last_success_at"] = status["checked_at"]
    base = prev if (prev.get("models") is not None or prev.get("checked_at")) else {}
    history["events"].extend(change_events(base, status["models"], status["checked_at"]))
    history["events"] = history["events"][-HISTORY_LIMIT:]
    history["updated_at"] = status["checked_at"]

    fresh = new_availability(prev, status["models"])
    alerts = build_alerts(fresh, status["models"])
    for title, message, click in alerts:
        tap = BAG_URL if settings.get("alert_click", "product") == "bag" else click
        for err in dispatch_alert(settings, title, message, tap, dry_run=dry_run, actions=alert_actions(click)):
            log(f"WARN: push failed: {err}")
        log(f"ALERT: {title}")
    return status, history, alerts


def pc_info_from(prev: dict) -> dict | None:
    """The PC runner's last heartbeat, whether the PC or the backup published last."""
    if prev.get("source") == "pc":
        return {"checked_at": prev.get("checked_at"), "error": prev.get("error"),
                "poll_seconds": prev.get("poll_seconds"), "warned": False}
    pc = prev.get("pc")
    return dict(pc) if isinstance(pc, dict) and pc.get("checked_at") else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check Apple Store pickup stock for iPhones.")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--prev", default=None, help="Previous status.json (for change detection)")
    ap.add_argument("--out", default="status.json")
    ap.add_argument("--history-in", default=None, help="Previous history.json")
    ap.add_argument("--history-out", default=None, help="Where to write the updated history.json")
    ap.add_argument("--fixture", default=None, help="Read Apple response from a file instead of the network")
    ap.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them")
    ap.add_argument("--test-alert", action="store_true", help="Send a test push notification and exit")
    ap.add_argument("--drill", action="store_true",
                    help="Send a fake restock alert through the real alert code (title starts with DRILL)")
    ap.add_argument("--source", default="cli", help="Label for who produced this status (github, pc, cli)")
    ap.add_argument("--defer-to-pc", type=int, default=0, metavar="SECONDS",
                    help="Skip (write nothing) if the PC runner published within this many seconds")
    args = ap.parse_args(argv)

    settings = load_settings(load_json(args.config, {}))

    has_channel = settings["topic"] or settings["telegram_token"] or settings["pushover_token"]
    if args.test_alert:
        if not has_channel and not args.dry_run:
            print("ERROR: no alert channel set (NTFY_TOPIC / TELEGRAM_* / PUSHOVER_*)", file=sys.stderr)
            return 1
        url = settings["models"][0].get("url", BUY_URL) if settings["models"] else BUY_URL
        errs = dispatch_alert(settings, "Test: iPhone stock alerts are working",
                              "This is what a restock alert looks like. Tap to open the Apple Store.",
                              url, dry_run=args.dry_run, urgent=False, priority="high", tags="iphone,white_check_mark")
        print("Test alert sent." + (f" Errors: {errs}" if errs else ""))
        return 0

    if args.drill:
        if not has_channel and not args.dry_run:
            print("ERROR: no alert channel set (NTFY_TOPIC / TELEGRAM_* / PUSHOVER_*)", file=sys.stderr)
            return 1
        if not settings["models"]:
            print("ERROR: no models configured", file=sys.stderr)
            return 1
        fake_store = {"id": "DRILL", "name": "Drill Store (not real)", "distance": "0 mi", "zip": "00000",
                      "available": True, "quote": "Available Today"}
        model = settings["models"][0]
        for title, message, click in build_alerts({model["part"]: [fake_store]}, settings["models"]):
            tap = BAG_URL if settings["alert_click"] == "bag" else click
            errs = dispatch_alert(settings, "DRILL - " + title, "DRILL, not a real restock.\n" + message,
                                  tap, dry_run=args.dry_run, actions=alert_actions(click))
            for e in errs:
                print(f"WARN: {e}", file=sys.stderr)
        print("Drill alert sent.")
        return 0

    prev = load_json(args.prev, {})
    if args.defer_to_pc and prev.get("source") == "pc" and not prev.get("error"):
        age = seconds_since(prev.get("checked_at"))
        if age is not None and age < args.defer_to_pc:
            print(f"PC runner is live (last check {int(age)}s ago); skipping this backup check.")
            return 0

    # Watchdog: the PC's last heartbeat is carried in status["pc"] across backup publishes,
    # so this works even after a backup (or app-forced) check overwrote the PC's status.
    # Warn once per outage ("warned" flag), reset when the PC publishes again.
    pc = pc_info_from(prev) if args.source == "github" else None
    if pc and settings["topic"] and not pc.get("warned"):
        age = seconds_since(pc.get("checked_at"))
        if age is not None and age > PC_OFFLINE_SECONDS:
            pc["warned"] = True
            try:
                send_ntfy(settings["topic"], "Duo Watch: your PC stopped checking",
                          f"No check from your PC for {int(age // 60)} min (restart, sleep or crash?). "
                          "The GitHub backup is checking every ~5 min until it's back.",
                          settings["server"], args.dry_run, click=settings["models"][0]["url"] if settings["models"] else BUY_URL,
                          priority="high", tags="warning,computer")
                print(f"Watchdog: PC silent {int(age)}s, warning sent.")
            except Exception as e:
                print(f"WARN: watchdog push failed: {e}", file=sys.stderr)

    history = load_json(args.history_in, {"events": []})
    status, history, _ = check_once(settings, prev, history, source=args.source,
                                    fixture=args.fixture, dry_run=args.dry_run,
                                    log=lambda m: print(m, file=sys.stderr if m.startswith(("ERROR", "WARN")) else sys.stdout))

    if pc:
        status["pc"] = pc
    history["updated_at"] = status["checked_at"]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    if args.history_out:
        with open(args.history_out, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=1)

    for m in status["models"]:
        n = sum(s["available"] for s in m["stores"])
        print(f"{m['label']}: available at {n}/{len(m['stores'])} stores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
