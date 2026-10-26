# iPhone Stock Tracker

Checks Apple Store in-store pickup availability for the iPhones you choose, every
~5 minutes, and pushes an alert to your phone the moment one comes in stock nearby.
A small dashboard you can add to your home screen shows current stock by store.

**Cost: $0.** GitHub Actions runs the checker, GitHub Pages hosts the dashboard,
and ntfy delivers the push notifications.

```
GitHub Actions (every 5 min) ──> Apple pickup endpoint
        │
        ├──> ntfy push ──> your phone   (only when something NEWLY comes in stock)
        └──> status.json on "status" branch ──> dashboard (GitHub Pages)
```

## Setup (about 15 minutes)

### 1. Create the repo
1. On GitHub, create a new **public** repository (e.g. `iphone-stock`).
   It must be public: GitHub Pages and unlimited Actions minutes are free only for public repos.
   Your ZIP and ntfy topic stay private as secrets; the dashboard does show nearby store names.
2. Upload everything in this folder (drag and drop on github.com works, or `git push`).
   Make sure the hidden `.github` folder comes along.

### 2. Set up phone alerts
1. Install the **ntfy** app (iOS or Android).
2. Tap **+**, subscribe to a topic with a long, hard-to-guess name, for example
   `hass-iphone-7f3k9q2x`. Anyone who knows the name can read it, so make it random.

### 3. Add two secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `NTFY_TOPIC` | the topic name from step 2 |
| `APPLE_ZIP` | the ZIP code to search around |

### 4. Pick your iPhones (`config.json`)
Already set up for **iPhone Duo 256GB unlocked** in both colors (Night Sky `MK1F4LL/A`,
Star White `MK1E4LL/A`), stores within `radius_miles` (25) of `zip`.
To track something else:
Replace the placeholder part numbers with the real ones for the models you want:

```json
{ "label": "iPhone X Pro 256GB Black", "part": "MXXXXLL/A" }
```

**Finding a part number:** on apple.com, open the iPhone buy page and choose the
model, color, storage and carrier. Open your browser's developer tools → **Network**
tab, then click **Check availability** / pickup options. Look for a request named
`pickup-message` (or `fulfillment-messages`); its URL contains `parts.0=XXXXXLL/A`. That's the part number.
Part numbers differ by color, storage *and* carrier, so add one entry per combo.

`radius_miles` drops stores farther than that; `max_stores` caps how many are shown
(Apple returns the nearest ~12). `zip` and `ntfy_topic` can live in `config.json` or,
for a public repo, in the `APPLE_ZIP` / `NTFY_TOPIC` secrets (secrets win).

### 5. Turn on the dashboard
**Settings → Pages →** Source: *Deploy from a branch*, Branch: `main`, folder: `/docs`.
Your dashboard will be at `https://<your-username>.github.io/<repo-name>/`.

### 6. Run it
**Actions tab →** enable workflows if prompted → **Check iPhone stock → Run workflow**.
After it finishes (~30 s), open the dashboard on your phone and use
**Share → Add to Home Screen**. From then on it runs every 5 minutes on its own.

## Fastest mode: run it on your PC (recommended)
GitHub's schedule is a backup that often runs late. `local_runner.py` checks about once a minute
from your own computer, pushes alerts instantly, re-alerts every 2 min while it's still in
stock ("nag mode"), and keeps the phone app live. GitHub automatically stands down while
your PC is running and takes over if the PC goes offline.

1. **Get the latest code** into your folder (e.g. `Documents\iphone-stock`). If the folder
   is a git clone: rename any old `local_runner.py` to `local_runner_grok.py`, then `git pull`.
   Otherwise download the repo ZIP and copy your two private files into it.
2. **Private files** next to `local_runner.py` (both are gitignored; never commit them):
   - `config.local.json`: see `config.local.example.json` (ZIP, ntfy topic, optional tuning)
   - `.env.local`: `GITHUB_TOKEN=...` and `GITHUB_REPO=hasskhalid/iphone-stock`.
     Use a **fine-grained token** limited to this one repo with *Contents: Read and write*.
3. **Start it:** double-click `start-runner.bat` (it restarts itself if anything crashes).
   Test alerts: `python local_runner.py --test-alert`.
4. **Start with Windows:** press Win+R, type `shell:startup`, and put a shortcut to
   `start-runner.bat` in that folder.
5. **Keep the PC awake:** Settings → System → Power → Sleep: *Never* (screen can turn off).

The app header shows **"Live · your PC checks every 60s"** with a green dot while it's running.

## Test your alerts
- **Actions → Send test alert → Run workflow** sends a test push through the same path
  real alerts use (GitHub → ntfy → your phone).
- Or type your topic into the box at the bottom of the dashboard and tap **Send test push**.

## Fastest checkout from an alert
1. **Tonight:** in the Apple Store app, add the iPhone Duo 256GB, your color, unlocked,
   no AppleCare to your **bag**, and make sure Apple Pay is set up.
2. **When an alert hits:** tap it → the Apple Store app opens on your Duo → tap **Bag** →
   **Check Out** → choose the store from the alert → Apple Pay → Face ID.
   (Apple's app only opens product pages from links, not the bag; the alert's
   "Bag in Safari" button is a backup.)

## Check now from the app
Add a fine-grained GitHub token (this repo only, *Actions: Read and write*) in the box at
the bottom of the app. When your PC's data is old, the Refresh button turns into
**Check now** and has GitHub check Apple immediately (about a minute).

## How alerts work
- You get a push only when a model goes from unavailable to available at a store,
  not on every check while it stays in stock.
- The very first run alerts for anything already in stock.
- Tapping the notification opens the Apple Store.

## Troubleshooting
- **Dashboard says "Last check failed"**: open the latest run in the Actions tab.
  Apple sometimes blocks or rate-limits requests from cloud servers. The dashboard keeps
  showing the last good data. If it fails constantly, the checker may need to run from
  a different machine (a home computer or a cheap VPS) instead of GitHub Actions.
- **Checks stopped**: GitHub can pause scheduled workflows on inactive repos. Re-enable
  it in the Actions tab.
- **Runs aren't exactly every 5 minutes**: normal; GitHub delays scheduled jobs when busy.

## Developing
```
python -m unittest discover tests        # offline tests, no network needed
python checker/check_stock.py --fixture <apple-response.json> --dry-run
```
See `CLAUDE.md` for notes aimed at AI coding sessions.
