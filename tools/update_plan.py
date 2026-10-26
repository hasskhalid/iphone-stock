#!/usr/bin/env python3
"""Daily plan: decide how often each search area (zone) is checked, from restock history.

Apple returns only the 12 nearest stores per search, so the tracker rotates between zones
(z0 = home, z1, z2 ... in the order of extra_zips). This script looks at recent restocks,
credits each one to the zones that can see that store (split if several can), and gives
busier zones more of the checks. Same total request rate to Apple; only the order changes.

Usage (run by the daily scheduled task, or by hand):
    python tools/update_plan.py --status status.json --history history.json --out docs/plan.json

Writes docs/plan.json (also shown on the dashboard's Patterns tab): {updated_at, zone_weights, window_days, restocks, summary, zones, top_stores,
peak_hours, colors, yesterday}. No ZIPs or locations beyond public store names.
"""

from __future__ import annotations

import argparse
import json
import math
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


def history_days(events: list, now: datetime) -> float:
    if not events:
        return 0.0
    first = min(datetime.fromisoformat(e["t"].replace("Z", "+00:00")) for e in events)
    return (now - first).total_seconds() / 86400


def known_stores(status: dict) -> dict[str, dict]:
    """Every store any zone has seen, with public info: name, state, store ZIP, coordinates."""
    out = {}
    for z in (status.get("zones") or {}).values():
        for m in z.get("models", []):
            for st in m.get("stores", []):
                if st.get("lat") is not None and st.get("lng") is not None:
                    out[str(st["id"])] = {"name": st.get("name"), "state": (st.get("state") or "").upper(),
                                          "zip": st.get("zip") or "", "lat": st["lat"], "lng": st["lng"]}
    return out


def _miles(a: dict, b: dict) -> float:
    r = 3958.8
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lng"] - a["lng"])
    return 2 * r * math.asin(math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))


def nearest(stores: dict, center: dict, k: int = 12) -> set[str]:
    return set(sorted(stores, key=lambda i: _miles(stores[i], center))[:k])


def suggest_extra_zips(status: dict, recent: list, history_days: float, keep_states: tuple = ("NJ",)) -> dict | None:
    """Weekly: would different EXTRA search ZIPs see more of the stores that actually restock?

    Home (z0) is never moved: its distances are what the dashboard sorts by. Candidates are
    Apple Store ZIPs (public). Apple returns the 12 nearest stores to a ZIP; we estimate that
    from the coordinates of every store the tracker has seen, so it's an estimate. A
    suggestion must still cover every store in keep_states (all of NJ).
    """
    if history_days < 6 or len(recent) < MIN_RESTOCKS:
        return None
    stores = known_stores(status)
    members = zone_members(status)
    if len(members) < 3 or not stores:
        return None
    home = members.get("z0", set())
    weight = Counter(str(e.get("store_id")) for e in recent)
    must = {i for i, st in stores.items() if st["state"] in keep_states}

    def value(z1: set, z2: set) -> float:
        # restocks only the extra zones can see; the near zone (z1) is checked ~4x as often as z2
        return sum(weight[i] for i in z1 - home) + 0.25 * sum(weight[i] for i in z2 - home - z1)

    cur_z1, cur_z2 = members.get("z1", set()), members.get("z2", set())
    best = (value(cur_z1, cur_z2), None)
    centers = {}
    for i, st in stores.items():
        if st["zip"] and st["zip"] not in centers:
            centers[st["zip"]] = (st, nearest(stores, st))
    for z1zip, (s1, n1) in centers.items():
        for z2zip, (s2, n2) in centers.items():
            if z1zip == z2zip or not must <= (home | n1 | n2):
                continue
            v = value(n1, n2)
            if v > best[0] + 0.99:                  # at least one more restock covered
                best = (v, (z1zip, s1["name"], z2zip, s2["name"], n1, n2))
    if not best[1]:
        return {"change": False, "why": "Your extra search ZIPs already cover the busiest stores well."}
    z1zip, n1name, z2zip, n2name, n1, n2 = best[1]
    gained = sorted({stores[i]["name"] for i in (n1 | n2) - home - cur_z1 - cur_z2 if weight[i]})
    return {
        "change": True,
        "extra_zips": [z1zip, z2zip],
        "near": n1name, "far": n2name,
        "why": (f"Searching near {n1name} ({z1zip}) and {n2name} ({z2zip}) would catch about "
                f"{best[0] - value(cur_z1, cur_z2):.0f} more of last week's restocks"
                + (f" (adds {', '.join(gained)})" if gained else "") + " while still covering every NJ store. Estimate."),
    }


def split_text(weights: list[int]) -> str:
    names = ["home area", "North Jersey area", "South Jersey area"] + [f"area {i}" for i in range(3, 10)]
    total = sum(weights)
    return ", ".join(f"{names[i]} {w / total:.0%}" for i, w in enumerate(weights))


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
               + (f". Checking split: {split_text(weights)}" if weights else ". Not enough data yet: default schedule"))
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
        "zip_suggestion": suggest_extra_zips(status, recent, history_days(events, now)),
        "yesterday": [{"t": e["t"], "store": e.get("store"), "color": e.get("color")} for e in y_events],
        "summary": summary,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--status", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--out", default="docs/plan.json")
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
