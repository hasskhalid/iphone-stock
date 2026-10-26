#!/usr/bin/env python3
"""Daily plan: decide how often each search area (zone) is checked, from restock history.

Apple returns only the 12 nearest stores per search, so the tracker rotates between zones
(z0 = home, z1, z2 ... in the order of extra_zips). This script looks at recent restocks,
credits each one to the zones that can see that store (split if several can), and gives
busier zones more of the checks. Same total request rate to Apple; only the order changes.

Usage (run by the daily scheduled task, or by hand):
    python tools/update_plan.py --status status.json --history history.json --out plan.json

Writes plan.json: {updated_at, zone_weights, window_days, restocks, summary, zones, top_stores,
peak_hours, colors, yesterday}. No ZIPs or locations beyond public store names.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

WINDOW_DAYS = 7
MIN_RESTOCKS = 3          # below this, keep the built-in schedule (not enough signal)
MAX_WEIGHT = 6
HOME_MIN_WEIGHT = 2       # home stays frequent: closest stores, and where alerts are most useful
NEAR_MIN_WEIGHT = 4       # z1 too: missed restocks never reach the history, so a rarely-checked zone
                          # looks quiet and would get starved (that's how Garden State Plaza was missed)
ET = timezone(timedelta(hours=-4))   # owner's time zone for the hour summary (EDT; close enough)


def zone_members(status: dict) -> dict[str, set[str]]:
    out = {}
    for key, z in (status.get("zones") or {}).items():
        ids = set()
        for m in z.get("models", []):
            ids.update(str(s.get("id")) for s in m.get("stores", []))
        out[key] = ids
    return out


def plan(status: dict, history: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    events = [e for e in (history.get("events") or []) if e.get("kind") == "in" and e.get("t")]
    recent = [e for e in events if now - datetime.fromisoformat(e["t"].replace("Z", "+00:00")) <= timedelta(days=WINDOW_DAYS)]
    members = zone_members(status)
    keys = sorted(members, key=lambda k: int(k[1:])) if members else []

    score = {k: 0.0 for k in keys}
    unseen = 0
    for e in recent:
        seen_in = [k for k in keys if str(e.get("store_id")) in members[k]]
        if not seen_in:
            unseen += 1
            continue
        for k in seen_in:                       # a store only one zone can see counts fully there
            score[k] += 1 / len(seen_in)

    weights = None
    if len(keys) > 1 and len(recent) >= MIN_RESTOCKS:
        top = max(score.values()) or 1
        weights = [max(1, min(MAX_WEIGHT, round(1 + (MAX_WEIGHT - 1) * score[k] / top))) for k in keys]
        weights[0] = max(weights[0], HOME_MIN_WEIGHT)
        weights[1] = max(weights[1], NEAR_MIN_WEIGHT)
        weights[0] = min(weights[0], weights[1] + 2)   # near zone keeps roughly a third of checks or more

    stores = Counter(e.get("store", "?") for e in recent)
    hours = Counter(datetime.fromisoformat(e["t"].replace("Z", "+00:00")).astimezone(ET).hour for e in recent)
    colors = Counter((e.get("color") or e.get("label") or "?") for e in recent)
    yday = (now.astimezone(ET) - timedelta(days=1)).date()
    y_events = [e for e in events if datetime.fromisoformat(e["t"].replace("Z", "+00:00")).astimezone(ET).date() == yday]

    def hour_label(h: int) -> str:
        return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"

    peak = [hour_label(h) for h, _ in hours.most_common(3)]
    summary = (f"{len(recent)} restocks in the last {WINDOW_DAYS} days"
               + (f"; busiest: {', '.join(f'{n} ({c})' for n, c in stores.most_common(3))}" if stores else "")
               + (f"; peak hours {', '.join(peak)}" if peak else "")
               + (f". Yesterday: {len(y_events)} restock(s)" if y_events else ". Yesterday: none seen")
               + (f". Area weights {weights}" if weights else ". Not enough data yet: default schedule"))
    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "zone_weights": weights,
        "window_days": WINDOW_DAYS,
        "restocks": len(recent),
        "unseen_restocks": unseen,
        "zones": {k: round(score[k], 2) for k in keys},
        "top_stores": stores.most_common(5),
        "peak_hours": peak,
        "colors": dict(colors),
        "yesterday": [{"t": e["t"], "store": e.get("store"), "color": e.get("color")} for e in y_events],
        "summary": summary,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--status", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--out", default="plan.json")
    args = ap.parse_args(argv)
    with open(args.status, encoding="utf-8") as f:
        status = json.load(f)
    with open(args.history, encoding="utf-8") as f:
        history = json.load(f)
    result = plan(status, history)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print(result["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
