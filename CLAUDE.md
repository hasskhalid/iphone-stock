# Project notes for Claude

iPhone in-store pickup stock tracker. Owner gets phone push alerts and checks a
home-screen dashboard. Keep it free to run and dependency-light.

## Architecture
- `checker/check_stock.py`: Python standard library only (no pip installs). Calls
  `https://www.apple.com/shop/retail/pickup-message?pl=true&parts.N=...&location=ZIP`
  (verified working Oct 2026; `/shop/fulfillment-messages` now returns HTTP 541),
  parses `body.stores[].partsAvailability[part].pickupDisplay` ("available"/"unavailable"),
  filters stores by `storedistance` (miles) <= `radius_miles`,
  writes `status.json` + `history.json` (restock "in" / sell-out "out" events, last 500),
  and POSTs an urgent ntfy push (tap opens the color's buy page, which the Apple Store app
  claims as a universal link; `alert_click: "bag"` opens /shop/bag, which the app does NOT
  claim, so it opens Safari; buttons "Apple Store app" + "Bag in Safari") for stores that
  newly became available (compares against the previous status.json).
  `--test-alert` sends a test push and exits. `dispatch_alert()` sends every alert to all
  configured channels in parallel threads: ntfy, plus optional Telegram (bot token + chat id) and
  Pushover (app token + user key; restock alerts use emergency priority 2, retry 30s, expire 600s;
  nags skip Pushover). Config keys `telegram_bot_token/telegram_chat_id/pushover_token/pushover_user`
  or env/secrets `TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID/PUSHOVER_TOKEN/PUSHOVER_USER`. Measured: tracker
  to ntfy server ~5s; the lag was ntfy-on-iOS delivery, hence the extra channels.
  Alert format: title `COLOR @ Store` (+N), body lines `Store · ZIP 12345` (zip from Apple's
  address.postalCode, stored per store as `zip`) and `Bag → Check Out → pickup → type ZIP … → Apple Pay`.
  The Apple Store app's pickup search is centered on the user's saved location, so the store ZIP
  is what they type to find it. No distances in alerts.
- PC alarm (`pc_alarm`, Windows): 3x two-tone winsound beeps, then System.Speech reads
  "iPhone Duo in stock. Night Sky at Grand Central, zip 1 0 0 1 7" twice, in a background thread;
  console prints a banner with the alert. `pc_sound` / `pc_voice` in config.local.json (default on).
  `--test-alert` plays it too.
- `local_runner.py` (+ `start-runner.bat`): PC runner on the owner's always-on Windows PC.
  Polls on a pace ladder `Pace` (45/60/75/90/120s; `poll_seconds` = fastest allowed, floor 45:
  60s and 15s fixed polling got the home IP rate-limited by Apple with HTTP 541). Starts at 60 (owner's
  preference; or the setting if slower), steps one rung faster after a clean cooldown (20 min), one rung slower at
  the start of each 541 episode, and each block doubles the cooldown (max 4 h) so it settles on the
  fastest tolerated pace. Pace + active block persisted in gitignored `runner_state.json` (`pace`,
  `blocked_until`); a changed setting resets the ladder. Exponential backoff on errors (max 900s;
  while erroring it publishes only the transition, not heartbeats, so the GitHub backup's good
  results stay visible), nag mode (re-push every `nag_minutes` while in stock, max `nag_max`),
  publishes status/history to the `status` branch through the GitHub Git Data API
  (tree → orphan commit → force ref; no git needed) on availability change or every
  `heartbeat_seconds` (60, i.e. every check, so the app shows each check). Private files: `config.local.json` (zip, ntfy_topic,
  overrides; models merge by part) and `.env.local` (GITHUB_TOKEN, GITHUB_REPO).
  Local state: `prev_status.json`, `history.local.json`. All gitignored.
- `.github/workflows/check-stock.yml`: backup, runs every 5 min; `--defer-to-pc 300` makes it
  skip (and not publish) while the PC runner's status (`source: "pc"`) is < 5 min old and not erroring. Reads the previous status
  from the `status` branch, runs the checker, force-pushes a single-commit `status`
  branch containing `status.json` + `history.json` (keeps main history clean).
- `.github/workflows/test-alert.yml`: "Send test alert" sends a DRILL restock alert via
  `check_stock.py --drill` (real alert builder, NTFY_TOPIC secret). Dispatchable from the app.
- PC heartbeat: every status carries `pc: {checked_at, error, poll_seconds, warned}`. The backup
  copies it forward (`pc_info_from(prev)`); while the PC is erroring it does NOT overwrite the
  backup's results, it reads status/history via the contents API and republishes them with
  only `pc` updated (`merge_pc_heartbeat`). App header shows "PC blocked by Apple" / "PC offline Xm".
- Watchdog: when the backup runs and `pc.checked_at` is older than `PC_OFFLINE_SECONDS` (900)
  and `pc.warned` is false, it pushes "your PC stopped checking" once and sets `warned`.
  GitHub's own cron has not fired reliably on this repo; an external cron (cron-job.org)
  POSTs workflow_dispatch for check-stock.yml every 5 min.
- `docs/`: static PWA "Duo Watch" served by GitHub Pages from `main:/docs`. Tabs:
  Stock (in-stock stores pinned on top as big cards with Buy buttons; the rest as a compact
  nearest-first list, so a store drops back to its usual spot when it sells out; `?demo=1`
  fakes one restock client-side for previewing), Recent changes, Patterns (restocks by hour /
  store). Fetches `status.json` + `history.json` from
  `raw.githubusercontent.com/<user>/<repo>/status/`, deriving user/repo from the Pages
  URL. Resolves the branch head sha via api.github.com (conditional request, 304s are
  free) then fetches files by sha from raw (raw caches branch names ~5 min, which made
  refresh look broken). Falls back to the branch URL if the API is rate limited.
  Syncs every 10s while open with a saved token (cache-busted, authenticated API), else every 20s, live "updated Xs ago", pull to refresh, source badge.
  `?repo=owner/name` overrides (handy for local testing), `?src=<url>` for local files. Has a
  "Send test push" box (validates the topic name; sends an alert identical to a real
  restock, labeled TEST); the ntfy topic is stored in the viewer's localStorage only.
  Optional GitHub token box (fine-grained, Actions read/write, localStorage only): when
  data isn't live (error, GitHub-sourced, or > 5 min old) Refresh becomes "Check now" and
  dispatches check-stock.yml; with the PC live, Refresh dispatches it with input
  `force: true` (skips --defer-to-pc) when the last check is > 45s old, so a tap always means
  a real Apple check. Then it syncs every 8s until a fresh check lands.
  Duo chibi mascots in the header (moods: watching / happy when in stock / sleepy when stale; rotating quips every ~14s; tap for random tricks). Live scan bar (fills toward the next check, "checks again in ~Xs"), shimmer across rows when a fresh check lands, sliding tab underline, 0.18s tab fade, staggered entrance on first load, stat pop on change, ↻ buttons spin continuously (fast while checking) and flip to a green ✓ wave top-to-bottom when a fresh check lands; twinkling star background, footer; per-store ↻ buttons (kick the same forced check; all stores are checked together). Stats strip (in stock / last seen / restocks), per-store "restocked Xm ago" from history,
  green page glow while anything is in stock. "Patterns & news" tab renders `docs/news.json`
  ({updated_at, summary, items:[{title, summary, source, url, date}]}), refreshed daily at 7:47 AM ET
  by a scheduled Claude task (paraphrased summaries with links, never copied text) and links out to other trackers (Manzana, Apple Inventory Check, iPhone Stock
  Checker); their data is not scraped or imported.
- Checkout automation was tried and removed (Oct 2026): Apple's sign-in stalls after 2FA and
  Add to Bag returns "page not found" in script-driven browsers. Don't re-add it or work around
  Apple's bot checks; the buying path is alert → Apple Store app → Apple Pay.
- `config.json`: models (`label`, `color`, `part`, `url`), `zip`, `radius_miles`, `max_stores`,
  optional `ntfy_topic`. Env vars `APPLE_ZIP` / `NTFY_TOPIC` (repo secrets) override.
- Tracking: iPhone Duo unlocked 256GB only (owner dropped 512GB): Night Sky `MK1F4LL/A` /
  Star White `MK1E4LL/A`. Models carry `storage`; alert titles use `color_of()` = label minus
  "iPhone Duo" → "256GB NIGHT SKY @ Store".
- Zones: Apple returns only the ~12 nearest stores per ZIP, so `extra_zips` (config) /
  `APPLE_EXTRA_ZIPS` (secret, comma-separated) add search centers. Each check queries ONE zone
  (`next_zone` rotates), results cached per zone in `status.zones` keyed `z0, z1…` (never the
  ZIP), merged by `merge_zones()` (freshest wins, nearest first); zones unseen > 30 min dropped.
  `zone_schedule()`: home and the FIRST extra zip (the "near" zone) alternate, further zips get one
  slot per cycle (3 zones -> 0,1,0,1,0,1,0,2). Apple returns only 12 stores per search (no param
  raises it; checked Oct 2026), and a Garden State Plaza restock was missed when its zone ran only
  every 4th check. Rush: when any restock is detected, `status.rush_zones` sends the next checks to
  every other zone immediately (restocks land in waves), then the schedule resumes. Daily plan:
  `tools/update_plan.py` (run by the daily scheduled task) reads status zones + history, credits
  each restock in the last 7 days to the zones that can see that store, and writes `plan.json`
  (repo root) with `zone_weights` (1..6 per zone; home >= 2, near zone z1 >= 4 and home <= z1+2,
  because missed restocks never reach history and a quiet-looking zone would get starved), plus a
  summary (top stores, peak hours, colours, yesterday). `zone_schedule(n, weights)` interleaves by
  smooth weighted round robin; wrong-length/invalid weights fall back to the default. The backup
  reads plan.json from its checkout; the PC runner re-reads it from GitHub main every hour. Extra zones are not
  cut by radius; `filter_area()` keeps stores within `radius_miles` of home plus every store in
  `keep_states` (config: ["NJ"], i.e. all 12 NJ Apple Stores; owner's extra zips cover North and
  South Jersey). Rows carry `state`.
  Apple's `storedistance` is from the SEARCHED zip, so merge_zones keeps z0 (home) distances and
  recomputes other stores' miles from home: `estimate_home()` trilaterates the home point from z0
  stores' `lat`/`lng` (storelatitude/storelongitude) + miles, then `haversine_miles()`.
  Same request rate to Apple; each zone refreshes every N checks.
  `tests/fixtures/live_sample_nyc.json` is a trimmed real response for reference.

## status.json shape
`{source ("pc"|"github"|"cli"), poll_seconds?, runner?, radius_miles, checked_at, last_success_at, error, models:[{part, label, color, url,
any_available, stores:[{id, name, city, distance, miles, available, quote, address,
phone, store_url, maps_url}]}], pc?: {checked_at, error, poll_seconds, warned}}`. The public repo must never contain the ZIP or topic.
On failure the checker keeps the previous `models` and sets `error`.

## Conventions
- Run `python -m unittest discover tests` before committing; add a test for any
  parsing or alert-logic change. Tests are offline (fixtures built in the test file).
- Dashboard: single HTML file, no build step, no frameworks, always dark (Night Sky + Star
  White palette, liquid-glass panels via backdrop-filter on containers only, sticky glass tabs, two chibi mascots: Night Sky left, Star White right), works at iPhone PWA and
  desktop widths. Store list is closest first (numbered); in-stock stores jump to the top as
  cards and drop back when they sell out. Animations are transform/opacity only and pause when hidden. Desktop (hover+fine pointer) only: no drifting glow / star twinkle behind the glass (Chrome re-blurred every frame and lagged other tabs), lighter blur, and animations pause when the window loses focus; iPhone unchanged. Bump `CACHE` in `docs/sw.js` when changing shell files.
- Alerts must stay transition-based (no repeat pushes while still in stock).
- No auto-checkout: purchases stay manual (owner taps the alert, buys with Apple Pay).
