<p align="center">
  <img src="static/iptv-preview.png" width="110" alt="">
</p>

<h1 align="center">IPTV</h1>

<p align="center">
  A fast local browser for Xtream IPTV subscriptions — instant search,
  favorites, offline downloads, and watch tracking that actually remembers
  which episode you're on.
</p>

---

Most IPTV apps make you scroll through thousands of unsorted channels, forget
what you watched the moment you close them, and reload their entire catalog
on every launch. This fixes all three.

## What it does

**Tells you where you left off.** The home screen says
*"You're on S02E07 — 23m in"* with a Resume button, and flips to
*"Play next — S02E08"* once you finish an episode. Nothing to mark by hand.

```
┌──────────────────────────────────────────────┐
│  ▐▐▐   Breaking Bad                          │
│  ▐▐▐   You're on S02E07 — 23m in             │
│  ▐▐▐   Next up: S02E08                       │
│  ▐▐▐   ████████████░░░░░░░░░░░░░             │
│  ▐▐▐   [ ▶ Resume S02E07 ]  [ Episodes ]     │
└──────────────────────────────────────────────┘
```

**Opens instantly.** The whole catalog lives in local SQLite. Nothing is
fetched from your provider at startup, so search works in milliseconds — and
keeps working when the provider is down.

**Finds things.** Full-text search across every channel, movie and show, with
diacritic folding so Arabic and Latin titles both match however you type them.
Tested against a ~24,000-item library: **sub-200ms**.

**Works offline.** Download any movie or episode. Interrupted downloads resume
at the exact byte they stopped at. Once downloaded, playback never touches the
network.

**Respects your connection limit.** Most subscriptions allow only one stream at
a time. The app reads that limit from your provider and automatically pauses
downloads while you're watching, resuming when you quit the player.

## How it works

The browser is the remote control — **mpv** does the actual playing.

```
Chrome (localhost:8000) ──► FastAPI ──► SQLite   catalog · favorites · history
                                │
                                └──────► mpv     playback + position tracking
```

This split is the reason the project works. Live channels are MPEG-TS and
movies are usually `.mkv`; no browser can decode either. Trying to fix that
with transcoding or HLS shims is where most IPTV web UIs die. Instead, clicking
Play launches mpv, and the app follows its progress over mpv's JSON IPC socket.

## Setup

Running this on your own PC takes about five minutes. You need two things:
**Python 3.11 or newer**, and **your own Xtream IPTV subscription** — no
subscription is included, provided, or sold here.

### 1. Install Python

Download it from [python.org/downloads](https://www.python.org/downloads/).

On Windows, **tick "Add python.exe to PATH"** on the first screen of the
installer. This is the single most common thing people miss — without it,
nothing below will find Python.

Check it worked by opening a terminal (Windows: press `Win`, type `cmd`,
press Enter) and running:

```bash
python --version
```

You should see `Python 3.11.x` or higher. If Windows says the command isn't
recognised, re-run the installer, choose **Modify**, and tick the PATH box.

### 2. Download the project

```bash
git clone https://github.com/amerhamdan3/iptv.git
cd iptv
```

No git? Use the green **Code → Download ZIP** button at the top of this page,
unzip it somewhere permanent like `C:\Users\YourName\iptv` (not your Downloads
folder), then `cd` into that folder.

### 3. Run the installer

```bash
python setup.py
```

This does four things, and is safe to re-run at any time — it skips whatever
is already in place:

1. installs the Python dependencies from `requirements.txt`
2. downloads a portable **mpv** (~40 MB) into `bin/` — on macOS and Linux it
   asks you to install mpv yourself instead, see step 6
3. creates your `.env` file by copying `.env.example`
4. puts an **IPTV** shortcut on your desktop (Windows only)

### 4. Put your IPTV account into `.env`

This is the only step that needs your own details. Open the `.env` file that
step 3 created — it sits next to `app.py` in the project folder. On Windows,
right-click it → **Open with** → **Notepad**.

You'll find three lines to fill in:

```ini
XTREAM_HOST=http://your-provider-host:8080
XTREAM_USER=your-username
XTREAM_PASS=your-password
```

**Where these come from.** When you buy an Xtream Codes subscription, your
provider emails you a login — usually as a portal address plus a username and
password, or as a single long "M3U URL". Either way, it contains all three
values:

```
http://line.example-provider.com:8080/get.php?username=ahmed123&password=Xy7pQ2&type=m3u_plus
└──────────── XTREAM_HOST ───────────┘          └ XTREAM_USER ┘ └ XTREAM_PASS ┘
```

Copied into `.env`, that example becomes:

```ini
XTREAM_HOST=http://line.example-provider.com:8080
XTREAM_USER=ahmed123
XTREAM_PASS=Xy7pQ2
```

Four rules that cover almost every mistake:

- **Include the port** (`:8080`, `:80`, `:25461` — whatever your provider gave
  you) and keep the `http://` or `https://` prefix.
- **Stop the host at the port.** No trailing slash, no `/c`, no `/get.php`,
  no `player_api.php`.
- **No quotes and no spaces** around the `=`. Write `XTREAM_PASS=Xy7pQ2`,
  not `XTREAM_PASS = "Xy7pQ2"`.
- **Save the file as `.env`** — exactly that, with the leading dot and no
  `.txt` on the end. Notepad likes to append `.txt`; in the Save dialog set
  *Save as type* to **All Files**.

Leave the rest of the file alone unless you want to change the port the web UI
listens on or where downloads are saved.

### 5. Start it

**Windows:** double-click the **IPTV** icon on your desktop. A black window
opens and stays open — that's the server, keep it there while you watch, and
close it when you're done. Your browser opens by itself after a few seconds.

**Any platform, from a terminal:**

```bash
python app.py
```

then open **http://127.0.0.1:8000** yourself.

The **first launch takes about a minute** while the entire catalog downloads
into a local database. Every launch after that is instant, because nothing is
fetched from your provider at startup.

### 6. macOS and Linux

Everything works, but `setup.py` can't install mpv for you — get it from your
package manager first:

```bash
brew install mpv        # macOS
sudo apt install mpv    # Debian / Ubuntu
```

Then `python setup.py`, edit `.env` as in step 4, and run `python app.py`.
There's no desktop shortcut; start it from the terminal each time.

### If something goes wrong

| What you see | What it means |
|---|---|
| `Missing Xtream credentials in .env` | `.env` is empty, missing, or saved as `.env.txt`. Redo step 4. |
| `No .env file yet` in the black window | Same — run `python setup.py` again, or copy `.env.example` to `.env` by hand. |
| Browser shows "can't connect" | The server window closed or never started. Run `python app.py` in a terminal to see the actual error. |
| Catalog is empty after the first sync | Host, username or password is wrong, or the subscription has expired. Check them against your provider's email, watching for a trailing slash on the host. |
| Clicking Play does nothing | mpv is missing. Confirm `bin/mpv.exe` exists, or that `mpv --version` works in a terminal. |
| Port 8000 is already taken | Set `PORT=8001` in `.env` and use http://127.0.0.1:8001. |

To start completely fresh, delete `iptv.db` and launch again — the catalog
re-syncs from scratch. Your `.env` is not touched.

> Your credentials live only in `.env`, which is listed in `.gitignore` and
> never leaves your machine. The app talks directly to your provider; there is
> no middleman server and nothing is sent anywhere else.

## Caching

| Cached | Where | Refreshed |
|---|---|---|
| Channels, movies, shows | `iptv.db` | daily in the background, or on demand |
| Episode lists, per show | `iptv.db` | first open, then only when the show changes |
| Posters | `cache/img/` | kept indefinitely |
| Favorites, history, positions | `iptv.db` | immediately |

Episode lists are fetched lazily and keyed on the provider's own
`last_modified`, so a show is only re-fetched when it actually gains episodes.
Cold open: ~0.9s. Cached: ~0.01s.

## Watch tracking

mpv reports its playback position every two seconds straight into SQLite, so
your place survives closing the player, closing the app, or a crash.

- Under 15 seconds — ignored, treated as "changed my mind"
- Past 90% — automatically marked watched
- The ✓ / ↺ button on any episode overrides tracking by hand

## Layout

```
app.py           API routes
config.py        .env loading, stream URL builders
xtream.py        Xtream player_api client
db.py            SQLite schema — catalog, favorites, history, downloads, FTS5
sync.py          catalog sync + per-series episode caching
player.py        mpv launching and position tracking over JSON IPC
downloader.py    resumable download queue
static/          the web UI (vanilla HTML/CSS/JS, no build step)
setup.py         dependencies, mpv, desktop shortcut
```

No frontend toolchain, no node_modules, no database server. Roughly 1,800
lines all in.

## API

The UI is a thin client over a plain JSON API, so it's easy to script:

| Endpoint | Purpose |
|---|---|
| `GET /api/search?q=` | full-text search across everything |
| `GET /api/browse?kind=&category=` | paged listings |
| `GET /api/series/{id}` | show details + episodes |
| `GET /api/continue` | continue-watching with next-episode resolution |
| `POST /api/play` | launch playback |
| `POST /api/download` | queue an offline download |
| `GET /api/status` | sync state, player state, disk usage |

## Notes

- Live channels can't be downloaded — they're continuous streams with no end.
- Delete `iptv.db` to reset everything and re-sync from scratch.
- Works on macOS and Linux too; install mpv yourself and run `python app.py`.

## License

MIT
