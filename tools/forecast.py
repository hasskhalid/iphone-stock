#!/usr/bin/env python3
"""Next-restock forecast for the watched stores (standard library only).

Why not LightGBM / regression: the tracker has only a handful of local restocks, so a
boosted model would memorize noise. Instead this is a smoothed probability model:

  * WHEN  - hour-of-day profile = national pattern (docs/priors.json, ~500 restocks)
            blended with local restocks (local data gets more say as it grows),
            times a restock rate per day from recent local history.
  * WHERE - store weights from local restocks with a 3-day half-life (recent stores count
            more) plus a small even prior so quiet stores never hit zero.
  * WHICH - colour share from the national 256GB split blended with local restocks.

Every prediction is back-tested on the tracker's own past restocks (each one predicted
using only what came before it) and the measured hit rates are published with it, so
the dashboard never claims more accuracy than it has shown.

Usage:
    python tools/forecast.py --status status.json --history history.json \
        --priors docs/priors.json --out forecast.json
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone

ET = timezone(timedelta(hours=-4))       # owner's zone (EDT); hour profiles are Eastern time
HOUR_PRIOR_STRENGTH = 12                 # national profile counts as 12 local restocks
COLOUR_PRIOR_STRENGTH = 10
STORE_PRIOR = 0.3                        # every watched store starts with this much weight
HALF_LIFE_DAYS = 3.0
WINDOW_HOURS = 6
FLAP_MINUTES = 15                        # in/out/in within this counts as one restock
TOP_K = 3


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def restock_events(history: dict, priors: dict, names_to_ids: dict, parts: set | None = None) -> list[dict]:
    """Restocks at the watched stores (names_to_ids) for the tracked models (parts), oldest first,
    with flapping merged and external sightings added. Old history from stores or models no longer
    watched (NY stores, 512GB) is ignored so the model learns only what the owner can buy."""
    watched = set(names_to_ids.values())
    ev = [{"t": _t(e["t"]), "store_id": str(e.get("store_id")), "store": e.get("store"), "color": e.get("color") or ""}
          for e in (history.get("events") or []) if e.get("kind") == "in" and e.get("t")
          and (not watched or str(e.get("store_id")) in watched)
          and (not parts or not e.get("part") or e.get("part") in parts)]
    for x in priors.get("external_local_restocks") or []:
        sid = names_to_ids.get(x.get("store"))
        if sid:
            ev.append({"t": _t(x["t"]), "store_id": sid, "store": x["store"], "color": x.get("color") or ""})
    ev.sort(key=lambda e: e["t"])
    out = []
    for e in ev:
        if any(o["store_id"] == e["store_id"] and o["color"] == e["color"]
               and abs((e["t"] - o["t"]).total_seconds()) < FLAP_MINUTES * 60 for o in out[-6:]):
            continue
        out.append(e)
    return out


def hour_profile(events: list[dict], priors: dict) -> list[float]:
    base = priors.get("hour_counts_et") or [1] * 24
    tot = sum(base) or 1
    local = [0] * 24
    for e in events:
        local[e["t"].astimezone(ET).hour] += 1
    n = len(events)
    return [(HOUR_PRIOR_STRENGTH * base[h] / tot + local[h]) / (HOUR_PRIOR_STRENGTH + n) for h in range(24)]


def colour_probs(events: list[dict], priors: dict) -> dict[str, float]:
    c = priors.get("colour_counts_256gb") or {"Night Sky": 1, "Star White": 1}
    tot = sum(c.values()) or 1
    out = {}
    for name in c:
        local = sum(1 for e in events if e["color"] == name)
        out[name] = (COLOUR_PRIOR_STRENGTH * c[name] / tot + local) / (COLOUR_PRIOR_STRENGTH + len(events))
    return out


def store_probs(events: list[dict], stores: dict[str, str], now: datetime) -> dict[str, float]:
    w = {sid: STORE_PRIOR for sid in stores}
    for e in events:
        if e["store_id"] in w:
            age = max(0.0, (now - e["t"]).total_seconds() / 86400)
            w[e["store_id"]] += 0.5 ** (age / HALF_LIFE_DAYS)
    tot = sum(w.values()) or 1
    return {sid: v / tot for sid, v in w.items()}


def best_window(hours: list[float], width: int = WINDOW_HOURS) -> tuple[int, float]:
    best = max(range(24), key=lambda s: sum(hours[(s + i) % 24] for i in range(width)))
    return best, sum(hours[(best + i) % 24] for i in range(width))


def daily_rate(events: list[dict], now: datetime) -> float:
    recent = [e for e in events if now - e["t"] <= timedelta(days=7)]
    if not recent:
        return 1.0
    span = max(1.0, min(7.0, (now - recent[0]["t"]).total_seconds() / 86400))
    return max(0.5, len(recent) / span)


def next_hours(hours: list[float], rate: float, now: datetime, horizon: int = 24) -> list[dict]:
    """Chance the NEXT restock lands in each of the coming hours (Poisson, hour-varying rate)."""
    out, survive = [], 1.0
    start = now.astimezone(ET).replace(minute=0, second=0, microsecond=0)
    for i in range(horizon):
        hr = start + timedelta(hours=i)
        lam = rate * hours[hr.hour] * (1 - now.astimezone(ET).minute / 60 if i == 0 else 1)
        p = survive * (1 - math.exp(-lam))
        out.append({"hour_et": hr.hour, "starts": hr.isoformat(timespec="minutes"), "p": round(p, 4)})
        survive *= math.exp(-lam)
    return out


def backtest(events: list[dict], priors: dict, stores: dict[str, str], min_train: int = 3) -> dict:
    """Predict each past restock from only the ones before it; count hits."""
    n = top = col = win = 0
    for k in range(min_train, len(events)):
        train, target, now = events[:k], events[k], events[k]["t"]
        if target["store_id"] not in stores:
            continue
        n += 1
        sp = store_probs(train, stores, now - timedelta(seconds=1))
        top += target["store_id"] in sorted(sp, key=sp.get, reverse=True)[:TOP_K]
        cp = colour_probs(train, priors)
        col += max(cp, key=cp.get) == target["color"]
        s, _ = best_window(hour_profile(train, priors))
        win += (target["t"].astimezone(ET).hour - s) % 24 < WINDOW_HOURS
    pct = lambda x: round(x / n, 3) if n else None
    return {"n": n, "top3_store": pct(top), "colour": pct(col), "time_window": pct(win),
            "hits": {"top3_store": top, "colour": col, "time_window": win}}


def forecast(status: dict, history: dict, priors: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stores = {}
    for m in status.get("models") or []:
        for st in m.get("stores") or []:
            stores[str(st["id"])] = st.get("name") or str(st["id"])
    names_to_ids = {v: k for k, v in stores.items()}
    parts = {m.get("part") for m in status.get("models") or [] if m.get("part")}
    events = restock_events(history, priors, names_to_ids, parts)
    hours = hour_profile(events, priors)
    colours = colour_probs(events, priors)
    sp = store_probs(events, stores, now)
    start, wp = best_window(hours)
    rate = daily_rate(events, now)
    top = sorted(sp, key=sp.get, reverse=True)[:5]
    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "model": "smoothed probability model (national hour/colour priors + recency-weighted local restocks)",
        "local_restocks": len(events),
        "rate_per_day": round(rate, 2),
        "hours_et": [round(h, 4) for h in hours],
        "window": {"start_et": start, "end_et": (start + WINDOW_HOURS) % 24, "p": round(wp, 3)},
        "colours": {k: round(v, 3) for k, v in colours.items()},
        "stores": [{"id": sid, "name": stores[sid], "p": round(sp[sid], 3)} for sid in top],
        "next_hours": next_hours(hours, rate, now),
        "backtest": backtest(events, priors, stores),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Next-restock forecast")
    ap.add_argument("--status", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--priors", default="docs/priors.json")
    ap.add_argument("--out", default="forecast.json")
    a = ap.parse_args(argv)
    load = lambda p, d: json.load(open(p, encoding="utf-8")) if p else d
    result = forecast(load(a.status, {}), load(a.history, {"events": []}), load(a.priors, {}))
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    b = result["backtest"]
    print(f"{result['local_restocks']} local restocks, {result['rate_per_day']}/day; window "
          f"{result['window']['start_et']}:00-{result['window']['end_et']}:00 ET; top {[s['name'] for s in result['stores'][:3]]}; "
          f"backtest n={b['n']} top3={b['top3_store']} colour={b['colour']} window={b['time_window']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
