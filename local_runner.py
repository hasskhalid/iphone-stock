#!/usr/bin/env python3
"""
Duo Watch PC runner: checks Apple about once a minute from your own computer.

- Same checking + alert logic as the GitHub checker (checker/check_stock.py).
- Push alert the moment a color lands at a store within your radius.
- Nag mode: while it's still in stock, re-alerts every few minutes so you can't miss it.
- Publishes status.json + history.json to the repo's "status" branch through the
  GitHub API (no git install needed): immediately when stock changes, otherwise every
  couple of minutes as a heartbeat. The phone app reads from there.
- Backs off automatically if Apple starts refusing requests, so you don't get blocked.

Private files (gitignored, never commit them), next to this script:
  config.local.json   {"zip": "...", "ntfy_topic": "...", optional overrides below}
  .env.local          GITHUB_TOKEN=...   GITHUB_REPO=hasskhalid/iphone-stock

Optional settings in config.local.json (defaults shown):
  "poll_seconds": 90, "heartbeat_seconds": 60, "nag_minutes": 2, "nag_max": 15
  poll_seconds is the FASTEST pace allowed (min 45). The runner moves along 45/60/75/90/120s:
  it starts at 60 (or your setting if slower), steps one faster after a clean cooldown, and one
  slower whenever Apple blocks it (HTTP 541). Each block doubles the cooldown (30 min → 4 h),
  so it settles on the fastest pace Apple tolerates.

Run:  python local_runner.py          (or double-click start-runner.bat on Windows)
      python local_runner.py --once   (single check, handy for testing)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "checker"))
sys.path.insert(0, os.path.join(HERE, "tools"))
import check_stock as cs  # noqa: E402

DEFAULTS = {"poll_seconds": 90, "heartbeat_seconds": 60, "nag_minutes": 2, "nag_max": 15}
MIN_POLL = 45          # faster than this gets your connection rate-limited by Apple (HTTP 541)
MAX_BACKOFF = 900      # non-Apple errors (network etc.): wait up to 15 min between tries
# Measured Oct 27: 75s ran all day with no blocks; 60s got blocked after 2h49m and again after 2h42m;
# blocks lasted 17-32 min. So: 75s normally, 60s only in the morning rush (2h, under the ~2h45m
# limit), and on a block wait 15 min before the first retry, then retry every 5 min.
STEADY_POLL = 75
PEAK_POLL = 60
# 60s windows, local (Eastern) time on the PC: the morning rush and the early-afternoon wave
# (Oct 27 restocks: 8:50-9:52 and 1:19-1:51 PM), each 2h (under the ~2h45m limit) with 2h at 75s between.
PEAK_WINDOWS = (((8, 30), (10, 30)), ((12, 30), (14, 30)))
SLOW_POLL = 90                         # only if Apple blocks even 75s on a given day
BLOCK_FIRST_WAIT = 900
BLOCK_PROBE = 300
MAX_ADAPTIVE_POLL = 120
PACE_LADDER = (45, 60, 75, 90, 120)   # seconds between checks
START_PACE = 60                       # start here; owner prefers 60/45 (a block steps back to 75)
PLAN_REFRESH_SECONDS = 3600          # re-read plan.json from GitHub this often
PACE_VERSION = 2                      # bump to make every runner restart from START_PACE
COOLDOWN_START = 1200                 # clean time at a pace before trying the next faster one
COOLDOWN_MAX = 4 * 3600               # each block doubles the cooldown, up to this
API = "https://api.github.com"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_env(path: str) -> dict:
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def merge_config(base: dict, local: dict) -> dict:
    """Overlay config.local.json on config.json. Models merge by part number so a
    local file with just {"part": ...} keeps the repo's label, color and buy link."""
    out = {**base, **{k: v for k, v in local.items() if k != "models"}}
    if local.get("models"):
        by_part = {m.get("part"): m for m in base.get("models", [])}
        out["models"] = [{**by_part.get(m.get("part"), {}), **m} for m in local["models"]]
    for k, v in DEFAULTS.items():
        out.setdefault(k, v)
    return out


def mask(text: str, keep: int = 4) -> str:
    """Hide most of a secret in console output (safe to screen-share)."""
    if not text:
        return "(not set)"
    return text[:keep] + "…" + text[-2:] if len(text) > keep + 2 else "…"


# --------------------------------------------------------------------------
# Publishing to GitHub (status branch) via the Git Data API
# --------------------------------------------------------------------------

class GitHubPublisher:
    def __init__(self, token: str, repo: str, branch: str = "status", request=None):
        self.token, self.repo, self.branch = token, repo, branch
        self._request = request or self._http

    def _http(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{API}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "duo-watch-local-runner",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                return e.code, {}

    def get_json(self, path: str, ref: str | None = None) -> dict | None:
        """Read a file (default: from the status branch) via the contents API (not cached like raw)."""
        code, data = self._request("GET", f"/repos/{self.repo}/contents/{path}?ref={ref or self.branch}")
        if code != 200 or not isinstance(data, dict) or "content" not in data:
            return None
        try:
            import base64
            return json.loads(base64.b64decode(data["content"]))
        except Exception:
            return None

    def publish(self, files: dict[str, str], message: str, attempts: int = 3) -> str:
        """Publish, retrying briefly on network hiccups (GitHub timeouts happen now and then)."""
        for i in range(attempts):
            try:
                return self._publish_once(files, message)
            except Exception:
                if i == attempts - 1:
                    raise
                time.sleep(2 * (i + 1))
        raise RuntimeError("unreachable")

    def _publish_once(self, files: dict[str, str], message: str) -> str:
        """Replace the branch with a single commit containing `files`. Returns commit sha."""
        r = f"/repos/{self.repo}"
        code, tree = self._request("POST", f"{r}/git/trees", {
            "tree": [{"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in files.items()]
        })
        if code != 201:
            raise RuntimeError(f"create tree failed ({code}): {tree.get('message', '')}")
        code, commit = self._request("POST", f"{r}/git/commits",
                                     {"message": message, "tree": tree["sha"], "parents": []})
        if code != 201:
            raise RuntimeError(f"create commit failed ({code}): {commit.get('message', '')}")
        code, ref = self._request("PATCH", f"{r}/git/refs/heads/{self.branch}",
                                  {"sha": commit["sha"], "force": True})
        if code == 422 or code == 404:  # branch doesn't exist yet
            code, ref = self._request("POST", f"{r}/git/refs",
                                      {"ref": f"refs/heads/{self.branch}", "sha": commit["sha"]})
        if code not in (200, 201):
            raise RuntimeError(f"update branch failed ({code}): {ref.get('message', '')}")
        return commit["sha"]


def fetch_remote_history(repo: str) -> dict | None:
    """Seed local history from the status branch so restock patterns carry over."""
    try:
        url = f"https://raw.githubusercontent.com/{repo}/status/history.json?t={int(time.time())}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data.get("events"), list) else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Nag mode
# --------------------------------------------------------------------------

def in_stock_rows(status: dict) -> list[tuple[dict, dict]]:
    return [(m, s) for m in status.get("models", []) for s in m.get("stores", []) if s.get("available")]


class Nagger:
    """Re-alerts every `minutes` while anything stays in stock, up to `max_count` times."""

    def __init__(self, minutes: float, max_count: int):
        self.interval = minutes * 60
        self.max_count = max_count
        self.last: float | None = None
        self.count = 0

    def update(self, now: float, status: dict, alerted: bool) -> list[tuple[dict, dict]] | None:
        rows = in_stock_rows(status)
        if not rows or status.get("error"):
            if not rows:
                self.last, self.count = None, 0
            return None
        if alerted or self.last is None:
            self.last = now
            return None
        if self.interval > 0 and self.count < self.max_count and now - self.last >= self.interval:
            self.last = now
            self.count += 1
            return rows
        return None


def nag_message(rows: list[tuple[dict, dict]]) -> tuple[str, str, str]:
    m0, s0 = rows[0]
    lines = [f"{cs.color_of(m).upper()} · {cs.store_line(s)}" for m, s in rows[:6]]
    hint = f"ZIP {s0['zip']}" if s0.get("zip") else s0["name"]
    lines.append(f"STILL THERE. Bag → Check Out → pickup → type {hint} → Apple Pay")
    return f"STILL: {cs.color_of(m0).upper()} @ {s0['name']}", "\n".join(lines), m0.get("url") or cs.BUY_URL


# --------------------------------------------------------------------------
# PC alarm: loud beeps + spoken announcement (Windows), so you hear it at the desk
# --------------------------------------------------------------------------

class Pace:
    """Pace ladder: one step slower on each Apple block, one step faster after a clean cooldown.

    `fastest` is poll_seconds from config (never below MIN_POLL). Each block doubles the
    cooldown, so after a few tries it stays on the fastest pace that doesn't get blocked.
    """

    def __init__(self, fastest: int, state: dict | None = None):
        fastest = max(MIN_POLL, int(fastest))
        self.rungs = [r for r in PACE_LADDER if r >= fastest] or [fastest]
        if self.rungs[0] != fastest:
            self.rungs.insert(0, fastest)
        st = (state or {}).get("pace") or {}
        if st.get("rungs") == self.rungs and st.get("v") == PACE_VERSION:   # same setting: resume
            self.idx = min(int(st.get("idx", 0)), len(self.rungs) - 1)
            self.cooldown = int(st.get("cooldown", COOLDOWN_START))
            self.clean = float(st.get("clean", 0))
        else:                                         # new or changed setting: start safe
            self.idx = max(i for i, r in enumerate(self.rungs) if r <= max(START_PACE, fastest))
            self.cooldown, self.clean = COOLDOWN_START, 0.0

    @property
    def poll(self) -> int:
        return self.rungs[self.idx]

    def ok(self, seconds: float) -> bool:
        """A clean check. Returns True if the pace just got faster."""
        self.clean += seconds
        if self.idx > 0 and self.clean >= self.cooldown:
            self.idx -= 1
            self.clean = 0.0
            return True
        return False

    def blocked(self) -> None:
        """Start of an Apple block (541): one step slower, and wait longer before speeding up again."""
        self.idx = min(self.idx + 1, len(self.rungs) - 1)
        self.cooldown = min(COOLDOWN_MAX, self.cooldown * 2)
        self.clean = 0.0

    def to_state(self) -> dict:
        return {"v": PACE_VERSION, "rungs": self.rungs, "idx": self.idx, "cooldown": self.cooldown, "clean": round(self.clean)}


def pick_poll(now: time.struct_time, no_peak_day: str | None = None, slow_day: str | None = None) -> int:
    """Seconds until the next check, from today's record (dates are YYYY-MM-DD, saved across restarts):
    - blocked today while already at 75s -> 90s for the rest of the day
    - blocked today at all               -> 75s for the rest of the day (no 60s windows)
    - otherwise                          -> 60s inside PEAK_WINDOWS, 75s the rest of the time."""
    today = time.strftime("%Y-%m-%d", now)
    if slow_day == today:
        return SLOW_POLL
    minutes = now.tm_hour * 60 + now.tm_min
    if no_peak_day != today and any(h1 * 60 + m1 <= minutes < h2 * 60 + m2 for (h1, m1), (h2, m2) in PEAK_WINDOWS):
        return PEAK_POLL
    return STEADY_POLL


def flip_poll(n: int) -> int:
    """60/75 flip experiment: alternate every check (average ~67s)."""
    return PEAK_POLL if n % 2 == 0 else STEADY_POLL


def block_wait(errors_in_row: int) -> int:
    """Apple 541 block: one long wait, then light probes (fewer requests while blocked)."""
    return BLOCK_FIRST_WAIT if errors_in_row <= 1 else BLOCK_PROBE


def spoken_text(alerts_or_rows) -> str:
    """'Night Sky at Grand Central, zip 1 0 0 1 7.' ZIP digits spaced so they're read one by one."""
    parts = []
    for title, message, _ in alerts_or_rows:
        first = message.split("\n")[0]                  # 'Grand Central · ZIP 10017'
        color = title.split(" @ ")[0].replace("STILL: ", "").title()
        store, _, z = first.partition(" · ZIP ")
        parts.append(f"{color} at {store}" + (f", zip {' '.join(z.strip())}" if z.strip() else ""))
    return "iPhone Duo in stock. " + ". ".join(parts) + "."


def pc_alarm(text: str, enabled_sound: bool = True, enabled_voice: bool = True) -> None:
    """Fire-and-forget: beeps, then reads the alert aloud. Never blocks or crashes the runner."""
    import threading

    def run():
        try:
            if enabled_sound:
                try:
                    import winsound
                    for _ in range(3):
                        winsound.Beep(1400, 220); winsound.Beep(1000, 220)
                except Exception:
                    print("\a" * 3, end="", flush=True)        # terminal bell elsewhere
            if enabled_voice and sys.platform == "win32":
                import subprocess
                safe = text.replace("'", "").replace('"', "")
                ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      f"$s.Rate = 0; $s.Volume = 100; $s.Speak('{safe}'); $s.Speak('{safe}')")
                subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                               timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def merge_pc_heartbeat(remote: dict | None, pc: dict) -> dict | None:
    """While the PC can't reach Apple, keep the backup's (fresher) results on the status
    branch and only update the PC heartbeat block. Returns None if there's nothing to merge."""
    if not isinstance(remote, dict) or remote.get("source") == "pc" or not remote.get("models"):
        return None
    merged = dict(remote)
    merged["pc"] = pc
    return merged


def availability_signature(status: dict) -> tuple:
    return (bool(status.get("error")), tuple(sorted(cs.available_set(status))))


def summary(status: dict) -> str:
    if status.get("error"):
        return f"ERR {status['error'][:90]}"
    parts = []
    for m in status.get("models", []):
        n = sum(s["available"] for s in m["stores"])
        parts.append(f"{m.get('color') or m['label']}: {n}/{len(m['stores'])}")
    return "OK   " + "  ·  ".join(parts)


def save_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Duo Watch PC runner")
    ap.add_argument("--once", action="store_true", help="Run a single check and exit")
    ap.add_argument("--no-publish", action="store_true", help="Don't push status to GitHub")
    ap.add_argument("--test-alert", action="store_true", help="Send a test push and exit")
    args = ap.parse_args(argv)

    os.chdir(HERE)
    base = cs.load_json("config.json", {})
    local = cs.load_json("config.local.json", {})
    config = merge_config(base, local)
    env = {**load_env(".env.local"), **{k: v for k, v in os.environ.items() if k in ("GITHUB_TOKEN", "GITHUB_REPO")}}
    settings = cs.load_settings(config)
    plan = cs.load_plan(os.path.join(HERE, "docs", "plan.json"))
    settings["zone_weights"] = plan.get("zone_weights")
    # Remember the pace and any active Apple block across restarts.
    state = cs.load_json("runner_state.json", {})
    pace = Pace(int(config["poll_seconds"]), state)
    # Days on which Apple blocked us (saved, so a restart doesn't forget and get blocked again).
    no_peak_day, slow_day = state.get("no_peak_day"), state.get("slow_day")
    # pace_mode "flip" (config.json, an experiment): alternate 60s/75s all day; a block ends the flip
    # for that day (75s after). Its start/block times are published so the result can be reviewed.
    pace_mode = str(config.get("pace_mode") or "windows")
    flip = dict(state.get("flip") or {})
    poll = pick_poll(time.localtime(), no_peak_day, slow_day)
    blocked_until = float(state.get("blocked_until") or 0)
    heartbeat = max(30, int(config["heartbeat_seconds"]))

    if args.test_alert:
        if not (settings["topic"] or settings["telegram_token"] or settings["pushover_token"]):
            print("No alert channel in config.local.json"); return 1
        errs = cs.dispatch_alert(settings, "Test: Duo Watch PC runner", "Alerts from your PC are working.",
                                 settings["models"][0]["url"] if settings["models"] else cs.BUY_URL,
                                 urgent=False, priority="high", tags="computer,white_check_mark")
        for e in errs:
            print("Channel error:", e)
        print("Playing the PC alarm (beeps + voice)…")
        pc_alarm(spoken_text([("NIGHT SKY @ Grand Central", "Grand Central · ZIP 10017", "")]).replace("in stock", "test alert"),
                 bool(config.get("pc_sound", True)), bool(config.get("pc_voice", True)))
        time.sleep(12)
        print("Test alert sent."); return 0

    publisher = None
    repo = env.get("GITHUB_REPO", "")
    if not args.no_publish and env.get("GITHUB_TOKEN") and repo:
        publisher = GitHubPublisher(env["GITHUB_TOKEN"], repo)

    print("Duo Watch PC runner")
    print(f"  Config:   config.json + {'config.local.json' if local else '(no config.local.json!)'}")
    print(f"  ZIP:      {mask(settings['zip'], 2)}   Radius: {settings['radius']} mi   Poll: every {poll}s (range {pace.rungs[0]}–{pace.rungs[-1]}s)")
    extra = [n for n, k in (("Telegram", "telegram_token"), ("Pushover", "pushover_token")) if settings.get(k)]
    print(f"  Alerts:   ntfy topic {mask(settings['topic'])}{' + ' + ' + '.join(extra) if extra else ''}   Nag: every {config['nag_minutes']} min (max {config['nag_max']})")
    print(f"  Publish:  {repo + ' (status branch, heartbeat ' + str(heartbeat) + 's)' if publisher else 'off'}")
    print("-" * 64)
    if not settings["zip"]:
        print("Add your ZIP to config.local.json, e.g. {\"zip\": \"12345\", \"ntfy_topic\": \"...\"}")
        return 1

    prev = cs.load_json("prev_status.json", {})
    history = cs.load_json("history.local.json", None)
    if history is None:
        history = (fetch_remote_history(repo) if repo else None) or {"events": []}

    nagger = Nagger(float(config["nag_minutes"]), int(config["nag_max"]))
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    checks, errors_in_row, last_publish, last_sig = 0, 0, 0.0, None

    wait0 = blocked_until - time.time()
    if wait0 > 0 and not args.once:
        print(f"Apple was blocking this connection recently; waiting {int(wait0)}s before the first check")
        print("(the GitHub backup keeps checking meanwhile; restarting won't make this faster)")
        time.sleep(wait0)
    plan_checked = time.time()
    fc_json, fc_events, fc_at = None, -1, 0.0
    try:
        while True:
            t0 = time.time()
            # The daily task updates plan.json on GitHub (which areas to check most); pick it up hourly.
            if publisher and t0 - plan_checked > PLAN_REFRESH_SECONDS:
                plan_checked = t0
                try:
                    fresh_plan = publisher.get_json("docs/plan.json", ref="main")
                except Exception:
                    fresh_plan = None
                w = (fresh_plan or {}).get("zone_weights")
                if fresh_plan is not None and w != settings.get("zone_weights"):
                    settings["zone_weights"] = w
                    print(f"           Daily plan updated: area weights {w} (from restock history)")
            status, history, alerts = cs.check_once(
                settings, prev, history, source="pc",
                log=lambda m: print(f"[{time.strftime('%H:%M:%S')}] !!! {m}") if m.startswith("WARN") else None)
            checks += 1
            poll = pick_poll(time.localtime(), no_peak_day, slow_day)
            if pace_mode == "flip":
                today = time.strftime("%Y-%m-%d")
                if flip.get("day") != today:
                    flip = {"day": today, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "blocked_at": None, "checks": 0}
                if not flip.get("blocked_at") and slow_day != today:
                    poll = flip_poll(flip["checks"])
                    flip["checks"] += 1
            status["poll_seconds"] = poll
            status["runner"] = {"started_at": started, "checks": checks, "mode": pace_mode}
            if pace_mode == "flip":
                status["runner"]["flip"] = flip
            pc_hb = {"checked_at": status["checked_at"], "error": status.get("error"), "poll_seconds": poll, "warned": False}
            status["pc"] = pc_hb

            for title, message, _ in alerts:
                print("\n" + "!" * 64)
                print(f"[{time.strftime('%H:%M:%S')}]  IN STOCK  >>>  {title}")
                for line in message.split("\n"):
                    print(f"            {line}")
                print("!" * 64 + "\n")
            if alerts:
                pc_alarm(spoken_text(alerts), bool(config.get("pc_sound", True)), bool(config.get("pc_voice", True)))
            nag = nagger.update(t0, status, bool(alerts))
            if nag and (settings["topic"] or settings["telegram_token"]):
                title, msg, click = nag_message(nag)
                try:
                    tap = cs.BAG_URL if settings.get("alert_click", "product") == "bag" else click
                    # Nags go everywhere except Pushover emergency (it already repeats until acknowledged).
                    errs = cs.dispatch_alert({**settings, "pushover_token": ""}, title, msg, tap,
                                             actions=cs.alert_actions(click))
                    print(f"[{time.strftime('%H:%M:%S')}] >>> NAG SENT: {title}" + (f" (errors: {errs})" if errs else ""))
                    pc_alarm(spoken_text([(title, msg, click)]).replace("in stock", "still in stock"),
                             bool(config.get("pc_sound", True)), bool(config.get("pc_voice", True)))
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] nag push failed: {e}")

            # Forecast: recompute when a restock is logged, or hourly (time-of-day odds shift).
            n_ev = len(history.get("events") or [])
            if n_ev != fc_events or t0 - fc_at > 3600:
                try:
                    import forecast as fc
                    fc_json = json.dumps(fc.forecast(status, history, cs.load_json(os.path.join(HERE, "docs", "priors.json"), {})), indent=1)
                    fc_events, fc_at = n_ev, t0
                except Exception as e:  # the forecast must never stop the tracker
                    print(f"           (forecast skipped: {e})")
            sig = availability_signature(status)
            changed = sig != last_sig
            note = ""
            # While erroring, only publish the change into the error state (so the app and the
            # GitHub backup know); skip heartbeats so the backup's good results aren't overwritten.
            due = t0 - last_publish >= heartbeat and not status.get("error")
            if publisher and (changed or due):
                try:
                    files = {"status.json": json.dumps(status, indent=1), "history.json": json.dumps(history, indent=1)}
                    if fc_json:
                        files["forecast.json"] = fc_json
                    publisher.publish(files, f"status {status['checked_at']}")
                    last_publish, last_sig = t0, sig
                    note = "  ↑ published"
                except Exception as e:
                    note = f"  ↑ publish failed: {e}"
            elif publisher and status.get("error") and t0 - last_publish >= heartbeat:
                # Blocked: don't overwrite the backup's good data, just refresh our heartbeat.
                try:
                    remote = publisher.get_json("status.json")
                    merged = merge_pc_heartbeat(remote, pc_hb)
                    if merged:
                        remote_hist = publisher.get_json("history.json") or history
                        files = {"status.json": json.dumps(merged, indent=1), "history.json": json.dumps(remote_hist, indent=1)}
                        remote_fc = publisher.get_json("forecast.json")
                        if remote_fc:
                            files["forecast.json"] = json.dumps(remote_fc, indent=1)
                        publisher.publish(files, f"pc heartbeat {status['checked_at']}")
                        note = "  ↑ heartbeat (backup's data kept)"
                    last_publish = t0
                except Exception as e:
                    note = f"  ↑ heartbeat failed: {e}"
            elif not publisher:
                last_sig = sig

            print(f"[{time.strftime('%H:%M:%S')}] {summary(status)}{note}")
            if not status.get("error"):
                prev = status
                errors_in_row = 0
            else:
                errors_in_row += 1
                # Start of an Apple rate-limit episode: one step slower from now on.
                if errors_in_row == 1 and "541" in (status.get("error") or ""):
                    today = time.strftime("%Y-%m-%d")
                    if pace_mode == "flip" and flip.get("day") == today and not flip.get("blocked_at"):
                        flip["blocked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        mins = (datetime.now(timezone.utc) - datetime.fromisoformat(flip["started_at"])).total_seconds() / 60
                        print(f"           60/75 flip test: blocked after {mins:.0f} min ({flip['checks']} checks); 75s for the rest of today")
                    elif poll >= STEADY_POLL:
                        slow_day = today            # even 75s was too fast today
                    no_peak_day = today
                    poll = pick_poll(time.localtime(), no_peak_day, slow_day)
                    status["poll_seconds"] = poll
                    print(f"           Apple rate limit hit; checking every {poll}s once it clears (for the rest of today)")
            save_json("prev_status.json", prev if prev else status)
            save_json("history.local.json", history)

            if args.once:
                return 0
            if not errors_in_row:
                wait = poll
            elif "541" in (status.get("error") or ""):
                wait = block_wait(errors_in_row)
            else:
                wait = min(MAX_BACKOFF, poll * 2 ** errors_in_row)
            try:
                save_json("runner_state.json", {"pace": pace.to_state(), "no_peak_day": no_peak_day, "slow_day": slow_day, "flip": flip,
                                                "blocked_until": (time.time() + wait) if errors_in_row else 0})
            except OSError:
                pass
            if errors_in_row:
                hint = " (Apple is rate-limiting this connection; it clears on its own)" if "541" in (status.get("error") or "") else ""
                print(f"           backing off {wait}s after {errors_in_row} error(s){hint}")
            time.sleep(max(1.0, wait + random.uniform(0, 3) - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nStopped. Alerts are off until you start it again (GitHub keeps checking every ~5 min).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
