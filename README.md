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

Requires **Python 3.11+**. On Windows, mpv is installed for you.

```bash
git clone https://github.com/YOUR_USERNAME/iptv.git
cd iptv
python setup.py
```

`setup.py` installs dependencies, downloads a portable mpv into `bin/`, creates
your `.env`, and puts an **IPTV** shortcut on your desktop.

Then open `.env` and fill in your provider details:

```ini
XTREAM_HOST=http://your-provider-host:8080
XTREAM_USER=your-username
XTREAM_PASS=your-password
```

Double-click the desktop icon (or run `python app.py`) and browse to
**http://127.0.0.1:8000**. First launch takes about a minute to pull the
catalog; every launch after that is instant.

> You need your own IPTV subscription — none is included or provided here.
> Your credentials live in `.env`, which is gitignored and never leaves your
> machine.

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
