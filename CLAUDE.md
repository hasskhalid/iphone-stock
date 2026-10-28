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
  overrides; models merge by part: repo models are always tracked, local entries override fields) and `.env.local` (GITHUB_TOKEN, GITHUB_REPO).
  Local state: `prev_status.json`, `history.local.json`. All gitignored.
- Pacing (measured Oct 27; the old Pace ladder is kept only for state compatibility): 75s ran all
  day with no blocks; 60s was blocked after 2h49m and again after 2h42m; blocks lasted 17-32 min.
  `pick_poll(now, no_peak_day, slow_day)`: 60s inside `PEAK_WINDOWS` (8:30-10:30 AM and
  12:30-2:30 PM PC-local Eastern time; owner's request), 90s in `QUIET_HOURS` (11 PM-7 AM, owner's
  choice Oct 27), 75s otherwise. Any block
  sets `no_peak_day` = today (75s for the rest of the day); a block while already at 75s+ also sets
  `slow_day` (90s rest of day). Both are saved in runner_state.json so a restart can't forget a block
  (a restart right after a block used to go straight back to 60s and get blocked again). Next day
  starts fresh. On HTTP 541, `block_wait()`: 15 min before the first retry, then every 5 min.
  Non-541 errors keep exponential backoff (max 900s).
  Owner chose (Oct 27 night) `pace_mode: "windows"` (both 60s windows). Experiments (config.json `pace_mode`; default "windows" = PEAK_WINDOWS schedule): "burst" (Oct 27
  test, owner's request) = 60s for BURST_MINUTES (120) from the first check of the day, then 75s;
  "flip" = alternate 60s/75s every check. A block ends the experiment for that day (75s after).
  Owner chose to keep the cooldown at 15 min then 5-min retries (a block on Oct 27 still failed at
  17.5 min). `runner_state.flip` {day, started_at, burst_done_at, blocked_at, checks} is published as
  status.runner.test for review. Don't change pace_mode after a test without the owner's input.
- Block log: every Apple error is appended to gitignored `blocks.local.jsonl` (one JSON per line);
  when a block clears, a record {start, end, minutes, at_least_minutes, pace} is logged too, kept
  in runner_state (`block_open`, `blocks_today`, survives restarts) and published as
  status.runner.blocks_today. Oct 27 blocks: 6:45 AM (60s, 18-32 min), 2:04 PM, ~2:16 PM (60s),
  4:30 PM (60s after 49 min, 25-30 min), 6:54 PM (75s after 1h54m, 25-30 min).
- `.github/workflows/check-stock.yml`: backup, runs every 5 min (uses the runner's system python3,
  no setup-python step, to start faster; never make it check more often to cover a PC block); `--defer-to-pc 300` makes it
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
  green page glow while anything is in stock.
  Oct 27 redesign: galaxy sky (tiled stars, violet nebula, crescent moon, ringed planet, shooting star;
  static on desktop), Halloween mode Oct 1-Nov 2 or `?spooky=1/0` (orange/purple palette, witch hat +
  cat ears, bats, boo/bats tap tricks). Chibis act per tab: Stock = watching (spooky costumes), Changes =
  trying on outfits every 2.8s (`data-fit` 1-6), Patterns = detectives (deerstalker, magnifier, notepad).
  Tap one chibi = solo trick (other looks over), every 3rd tap = duo trick; eyes follow finger/mouse;
  `say(text, ms)` holds the bubble. Themes (`THEMES`, Settings picker saved in localStorage, `?theme=`):
  auto = halloween Oct 1-Nov 2, harvest rest of Nov (falling leaves, beanie, fall fit), holidays Dec 1-Jan 2
  (snow, Santa hats, sweater fit), galaxy otherwise. Badges are hollow (outline) when not in stock,
  filled with a green glow when in stock. Forecast banner = the Duos stirring a cauldron with a rotating spell.
  Storage badges everywhere (`badge()`): Night Sky = blue circle/white text, Star White = white circle/
  blue text, "256"/"512". Watching rows show badges + last restock only (weekly "🔥 N× this week"
  lives in Patterns → Hot stores with per-variant counts). Patterns hour chart is swipeable
  (pointer events, touch-action pan-y; your NJ restocks + forecast hours_et ghost bars) with an hour
  detail card (count, share, daily odds, PC pace); starts on a whole-day card, double-tap returns to it.
  Changes / stats / Patterns show only history for watched stores + tracked parts (`watchedHistory()`);
  forecast.py and update_plan.py likewise ignore old NY / 512GB events. Plan summary is a one-line
  "short memory". Halloween scan bar = toxic green fizzing cauldron. Halloween Stock tab: every 10s a POOF cloud
  (`data-pc` a/b restarts it) swaps monster costumes (`data-cos` 0-4: witch & cat, Frankenstein & mummy,
  skeleton & Dracula, werewolf & pumpkin, zombie & ghost). Hide-and-seek ghost (`.hideghost`): random spot
  from SPOTS each page load (never the same twice), tap = found count in localStorage, re-hides after 45s.
  Sky decor: witch flying past the moon, pumpkins, skull pile, dangling spider (static on desktop).
  Every theme now has the same kit (Oct 28): costume set swapped in a POOF every 10s on Stock
  (`COSTUME_SETS`: halloween cos0-4, harvest cos5-8 beanie/turkey+pilgrim/scarecrow+chef/game day,
  holidays cos9-12 Santas/Rudolph+Frosty/gingerbread+angel/elf+present, galaxy -1 & cos13-15
  astronaut+alien/UFO+Saturn/rocket+stars), hidden friend (`HIDERS`: ghost / turkey / gingerbread / alien,
  found counts per theme), sky flyers (witch / geese / Santa's sleigh / UFO + floating astronaut),
  scan bar (toxic cauldron / autumn / candy cane / starfield), forecast pot (cider / hot cocoa / stardust,
  per-theme mini hats, `oracle` title), spells (`SPELL_SETS`), duo tricks (boo+bats / leaves / snow / UFO beam),
  title font (Creepster / Pacifico / Mountains of Christmas / Orbitron via Google Fonts).
  Lockout + backup: `lockState()` = "locked" when the PC's error is HTTP 541 (pc.block_since / pc.retry_at,
  published by the runner via `block_info()`), "backup" when the backup published and the PC is quiet.
  Locked = Duos behind jail bars with a wiggling padlock, frowns, sweat drops, padlock banner with minutes
  locked, next retry countdown and a 30-min progress bar. Backup = Duos float on a cloud holding a
  "BACKUP ON" sign + cloud banner. No "other trackers" section any more.
  "Patterns & news" tab renders `docs/news.json`
  ({updated_at, summary, items:[{title, summary, source, url, date}]}), refreshed daily at 7:47 AM ET
  by a scheduled Claude task (paraphrased summaries with links, never copied text). Other trackers'
  data is never scraped or imported (owner asked Oct 27 to pull their status as a backup: declined).
- Forecast: `tools/forecast.py` (stdlib) = smoothed probability model, not ML: hour-of-day profile
  (national counts in `docs/priors.json`, read off iPhone Stock Checker's Patterns page as shared by
  the owner, never scraped; blended with local restocks), store weights (local restocks, 3-day
  half-life, small even prior), colour share (national 256GB split + local), daily rate; flapping
  in/out within 15 min counts once. Rolling back-test predicts each past restock from earlier ones
  only and publishes hit rates (top-3 store, colour, 6h window). LightGBM/regression rejected: ~10
  local events. Runner recomputes on each new history event and hourly; backup runs it each check;
  both publish `forecast.json` on the status branch (must be included in every orphan commit or it
  disappears). Dashboard banner shows "🔮 Next likely" + live next-hour odds + measured accuracy
  ("still learning" under 20 tests); never claim accuracy the back-test hasn't shown.
- Checkout automation was tried and removed (Oct 2026): Apple's sign-in stalls after 2FA and
  Add to Bag returns "page not found" in script-driven browsers. Don't re-add it or work around
  Apple's bot checks; the buying path is alert → Apple Store app → Apple Pay.
- `config.json`: models (`label`, `color`, `part`, `url`), `zip`, `radius_miles`, `max_stores`,
  optional `ntfy_topic`. Env vars `APPLE_ZIP` / `NTFY_TOPIC` (repo secrets) override.
- Tracking: iPhone Duo unlocked 256GB only, both colours: Night Sky `MK1F4LL/A` / Star White
  `MK1E4LL/A` (512GB `MK1H4LL/A`/`MK1G4LL/A` was added Oct 27 then removed the same night at the
  owner's request; don't re-add without asking).
  NJ stores only: config `only_states: ["NJ"]` makes `filter_area()` keep only NJ (any distance,
  all 12 NJ Apple Stores), dropping NY. Owner's search ZIPs for that: home + Rockaway area (8 NJ
  stores incl. Garden State Plaza, Willowbrook, Tice's Corner) + South Jersey; the American Dream
  (NYC-overlap) zone is no longer needed. In stock = pickupDisplay "available" AND
  storePickEligible not false (Apple's response has no isBuyable/inventory fields; checked Oct 27). Models carry `storage`; alert titles use `color_of()` = label minus
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
  each restock in the last 7 days to the zones that can see that store, and writes `docs/plan.json`
  (served with the app, shown on Patterns as "This week's plan") with `zone_weights` (1..6 per zone; home >= 2, near zone z1 >= 4 and home <= z1+2,
  because missed restocks never reach history and a quiet-looking zone would get starved), plus a
  summary (top stores, peak hours, colours, yesterday, plain-English checking split) and
  `zip_suggestion`: once history spans 6+ days, `suggest_extra_zips()` estimates (from store
  coordinates, Apple's 12-nearest rule) whether other Apple Store ZIPs as extra search centers
  would see more of last week's restocks while still covering all NJ; home ZIP is never moved
  (its distances drive sorting). Owner applies it by hand (config.local.json + APPLE_EXTRA_ZIPS).
  Dashboard rows show "🔥 N× this week" (7-day restocks). `zone_schedule(n, weights)` interleaves by
  smooth weighted round robin; wrong-length/invalid weights fall back to the default. The backup
  reads docs/plan.json from its checkout; the PC runner re-reads it from GitHub main every hour. Extra zones are not
  cut by radius; `filter_area()` keeps stores within `radius_miles` of home plus every store in
  `keep_states` (config: ["NJ"], i.e. all 12 NJ Apple Stores). Owner's extra zips (Oct 26): first
  = American Dream's store ZIP, whose 12 nearest include the NYC stores (Fifth Ave, Grand Central,
  SoHo, WTC, W 14th, Williamsburg, Brooklyn), so those sit in BOTH frequent searches (a Fifth Ave
  restock was missed when it was only the 12th store of the home search); then Rockaway area
  (Rockaway, Tice's Corner) and South Jersey. Planner names areas by each zone's nearest store. Rows carry `state`. If the home zone (z0) isn't cached and fresh (restart after >30 min, first run), the next check always searches home first, so distances/sorting never come from another area.
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
