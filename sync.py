"""Catalog synchronisation.

Rules of the house:
  * The UI never waits on the provider. Sync runs in the background and the
    UI reads whatever is already in SQLite.
  * Episode lists are fetched lazily per series and cached until the
    provider's own last_modified says the show changed.
"""
import asyncio
import re
import time
import unicodedata

import httpx

import config
import db
from xtream import Xtream

# How long a series' episode list is trusted before we re-check it.
EPISODE_TTL = 24 * 3600

# Kept gentle: 4 parallel calls with no pause got us temporarily blocked.
DETAILS_CONCURRENCY = 2
DETAILS_PAUSE = 0.25  # seconds each worker waits after a get_vod_info call

_sync_lock = asyncio.Lock()
_details_lock = asyncio.Lock()
_state = {"running": False, "stage": "", "progress": 0, "last_error": None}
_details = {"running": False, "done": 0, "total": 0}


def status() -> dict:
    last = db.get_meta("last_sync", "0")
    return {
        **_state,
        "details": dict(_details),
        "last_sync": int(last),
        "stale": (time.time() - int(last)) > config.SYNC_INTERVAL_HOURS * 3600,
        "counts": counts(),
    }


def counts() -> dict:
    def n(table):
        row = db.one(f"SELECT COUNT(*) c FROM {table}")
        return row["c"] if row else 0
    return {"live": n("live"), "vod": n("vod"),
            "series": n("series"), "episodes": n("episodes")}


def _rebuild_search() -> None:
    """Rebuild the FTS index from the catalog tables in one pass."""
    db.execute("DELETE FROM search")
    with db._lock:
        c = db.connect()
        c.execute("INSERT INTO search(name, kind, item_id) "
                  "SELECT name, 'live', stream_id FROM live")
        c.execute("INSERT INTO search(name, kind, item_id) "
                  "SELECT name, 'vod', stream_id FROM vod")
        c.execute("INSERT INTO search(name, kind, item_id) "
                  "SELECT name, 'series', series_id FROM series")
        c.commit()


async def full_sync(force: bool = False) -> dict:
    """Pull the whole catalog. Safe to call repeatedly; it upserts."""
    if _sync_lock.locked():
        return status()

    async with _sync_lock:
        _state.update(running=True, stage="connecting", progress=0,
                      last_error=None)
        api = Xtream()
        try:
            acct = await api.account()
            info = acct.get("user_info", {})
            if str(info.get("auth")) != "1":
                raise RuntimeError("Xtream authentication failed")
            db.set_meta("max_connections", info.get("max_connections", "1"))
            db.set_meta("account_status", info.get("status", ""))
            db.set_meta("account_expires", info.get("exp_date", ""))

            _state.update(stage="categories", progress=5)
            for kind, coro in (("live", api.live_categories()),
                               ("vod", api.vod_categories()),
                               ("series", api.series_categories())):
                cats = await coro
                db.executemany(
                    "INSERT INTO categories(kind,id,name) VALUES(?,?,?) "
                    "ON CONFLICT(kind,id) DO UPDATE SET name=excluded.name",
                    [(kind, str(c["category_id"]), c["category_name"])
                     for c in cats])

            _state.update(stage="live channels", progress=15)
            rows = await api.live_streams()
            db.executemany(
                "INSERT INTO live(stream_id,name,category_id,icon,"
                "epg_channel_id,tv_archive,num) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(stream_id) DO UPDATE SET "
                "name=excluded.name, category_id=excluded.category_id, "
                "icon=excluded.icon, tv_archive=excluded.tv_archive",
                [(int(r["stream_id"]), r.get("name", ""),
                  str(r.get("category_id") or ""), r.get("stream_icon") or "",
                  r.get("epg_channel_id") or "", int(r.get("tv_archive") or 0),
                  int(r.get("num") or 0)) for r in rows])

            _state.update(stage="movies", progress=45)
            rows = await api.vod_streams()
            db.executemany(
                "INSERT INTO vod(stream_id,name,category_id,icon,ext,rating,"
                "added,num,year) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(stream_id) DO UPDATE SET "
                "name=excluded.name, category_id=excluded.category_id, "
                "icon=excluded.icon, ext=excluded.ext, "
                # A rating or release year from get_vod_info beats the list's.
                "rating=CASE WHEN excluded.rating > 0 THEN excluded.rating "
                "  ELSE vod.rating END, "
                "year=CASE WHEN vod.details_synced_at > 0 AND vod.year > 0 "
                "  THEN vod.year ELSE excluded.year END",
                [(int(r["stream_id"]), r.get("name", ""),
                  str(r.get("category_id") or ""), r.get("stream_icon") or "",
                  r.get("container_extension") or "mp4",
                  _f(r.get("rating")), int(r.get("added") or 0),
                  int(r.get("num") or 0), year_from(r.get("name")))
                 for r in rows])

            _state.update(stage="series", progress=75)
            rows = await api.series()
            db.executemany(
                "INSERT INTO series(series_id,name,category_id,cover,plot,"
                "genre,rating,release_date,last_modified,year) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(series_id) DO UPDATE SET "
                "name=excluded.name, category_id=excluded.category_id, "
                "cover=excluded.cover, plot=excluded.plot, "
                "genre=excluded.genre, rating=excluded.rating, "
                "release_date=excluded.release_date, year=excluded.year, "
                # Reset the episode cache only when the show actually changed.
                "episodes_synced_at=CASE "
                "  WHEN excluded.last_modified > series.last_modified THEN 0 "
                "  ELSE series.episodes_synced_at END, "
                "last_modified=excluded.last_modified",
                [(int(r["series_id"]), r.get("name", ""),
                  str(r.get("category_id") or ""), r.get("cover") or "",
                  r.get("plot") or "", norm_genre(r.get("genre")),
                  _f(r.get("rating")), r.get("releaseDate") or "",
                  int(r.get("last_modified") or 0),
                  year_from(r.get("releaseDate")) or year_from(r.get("name")))
                 for r in rows])

            _state.update(stage="building search index", progress=92)
            _rebuild_search()

            db.set_meta("last_sync", db.now())
            _state.update(stage="done", progress=100)
            # Genre and release year for new movies, without holding up sync.
            asyncio.create_task(sync_movie_details())
        except Exception as e:  # surfaced in the UI, never fatal
            _state["last_error"] = f"{type(e).__name__}: {e}"
            _state["stage"] = "failed"
        finally:
            _state["running"] = False
            await api.close()

    return status()


async def sync_episodes(series_id: int, force: bool = False) -> list[dict]:
    """Fetch and cache one series' episodes. Returns the cached rows."""
    row = db.one("SELECT episodes_synced_at FROM series WHERE series_id=?",
                 (series_id,))
    fresh = row and (db.now() - (row["episodes_synced_at"] or 0)) < EPISODE_TTL
    if fresh and not force:
        return episodes(series_id)

    api = Xtream()
    try:
        info = await api.series_info(series_id)
    finally:
        await api.close()

    batch = []
    for season, eps in (info.get("episodes") or {}).items():
        for e in eps:
            meta = e.get("info") or {}
            batch.append((
                int(e["id"]), series_id,
                int(e.get("season") or season or 0),
                int(e.get("episode_num") or 0),
                e.get("title") or "",
                e.get("container_extension") or "mkv",
                int(meta.get("duration_secs") or 0),
                int(e.get("added") or 0),
            ))

    if batch:
        db.executemany(
            "INSERT INTO episodes(episode_id,series_id,season,ep_num,title,"
            "ext,duration_secs,added) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(episode_id) DO UPDATE SET "
            "season=excluded.season, ep_num=excluded.ep_num, "
            "title=excluded.title, ext=excluded.ext, "
            "duration_secs=excluded.duration_secs", batch)

    db.execute("UPDATE series SET episodes_synced_at=? WHERE series_id=?",
               (db.now(), series_id))
    return episodes(series_id)


def episodes(series_id: int) -> list[dict]:
    return db.query(
        "SELECT e.*, h.position_sec, h.completed, h.watched_at, "
        "       d.status AS download_status, d.local_path "
        "FROM episodes e "
        "LEFT JOIN history h ON h.kind='episode' AND h.item_id=e.episode_id "
        "LEFT JOIN downloads d ON d.kind='episode' AND d.item_id=e.episode_id "
        "WHERE e.series_id=? ORDER BY e.season, e.ep_num", (series_id,))


_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def year_from(text) -> int:
    """Release year from "2014-03-02", "Ouija (2014)" or "Orphan - 2009".

    Takes the last plausible year, so "Blade Runner 2049 (2017)" is 2017 and
    "2012 (2009)" is 2009.
    """
    latest = time.gmtime().tm_year + 1
    years = [int(y) for y in _YEAR.findall(str(text or ""))]
    years = [y for y in years if 1900 <= y <= latest]
    return years[-1] if years else 0


def norm_genre(g) -> str:
    """Canonical "A, B, C" form, so a genre filter can match on ", A,"."""
    # NFKC folds Arabic presentation forms, so "ﺩﺭاﻣﺎ" and "دراما" are one genre.
    g = unicodedata.normalize("NFKC", str(g or ""))
    parts = [p.strip() for p in re.split(r"[,/|]", g)]
    return ", ".join(dict.fromkeys(p for p in parts if p))


def backfill_years() -> None:
    """One-off: fill year/genre columns for catalogs synced before they existed."""
    if db.get_meta("years_backfilled"):
        return
    db.executemany("UPDATE vod SET year=? WHERE stream_id=?",
                   [(year_from(r["name"]), r["stream_id"])
                    for r in db.query("SELECT stream_id, name FROM vod")])
    db.executemany(
        "UPDATE series SET year=?, genre=? WHERE series_id=?",
        [(year_from(r["release_date"]) or year_from(r["name"]),
          norm_genre(r["genre"]), r["series_id"])
         for r in db.query("SELECT series_id, name, genre, release_date "
                           "FROM series")])
    db.set_meta("years_backfilled", 1)


async def sync_movie_details() -> None:
    """Fill genre, release year and plot for movies that don't have them yet.

    The bulk movie list carries none of these, so each movie needs its own
    get_vod_info call. Slow the first time (one call per movie), cheap after:
    only movies never fetched before are queued.
    """
    if _details_lock.locked():
        return
    async with _details_lock:
        todo = [r["stream_id"] for r in db.query(
            "SELECT stream_id FROM vod WHERE details_synced_at=0 "
            "ORDER BY added DESC")]
        if not todo:
            return
        _details.update(running=True, done=0, total=len(todo))
        api = Xtream()
        sem = asyncio.Semaphore(DETAILS_CONCURRENCY)

        def row(sid: int, info: dict) -> tuple:
            y = year_from(info.get("releasedate") or info.get("release_date"))
            return (norm_genre(info.get("genre")),
                    info.get("plot") or info.get("description") or "",
                    y, y, _f(info.get("rating")), db.now(), sid)

        async def fetch(sid: int) -> tuple[str, int, dict]:
            async with sem:
                try:
                    data = await api._call("get_vod_info", vod_id=sid)
                except httpx.HTTPStatusError as e:
                    return ("missing" if e.response.status_code == 404
                            else "error", sid, {})
                except Exception:
                    return "error", sid, {}
                finally:
                    await asyncio.sleep(DETAILS_PAUSE)
            info = data.get("info") if isinstance(data, dict) else None
            return "ok", sid, info if isinstance(info, dict) else {}

        async def provider_up() -> bool:
            try:
                await api.account()
                return True
            except Exception:
                return False

        try:
            for i in range(0, len(todo), 50):
                results = await asyncio.gather(
                    *(fetch(s) for s in todo[i:i + 50]))
                done = [row(sid, info) for st, sid, info in results
                        if st == "ok"]
                missing = [sid for st, sid, _ in results if st == "missing"]
                errors = sum(st == "error" for st, _, _ in results)

                # A 404 means "no such movie" only while the rest of the API
                # still answers. When the provider is blocking us it 404s
                # everything, and those movies must stay queued, not be
                # recorded as having no genre.
                blocked = (missing or errors) and not await provider_up()
                if not blocked:
                    done += [row(sid, {}) for sid in missing]
                if done:
                    db.executemany(
                        "UPDATE vod SET genre=?, plot=?, "
                        "year=CASE WHEN ? > 0 THEN ? ELSE year END, "
                        "rating=CASE WHEN rating > 0 THEN rating ELSE ? END, "
                        "details_synced_at=? WHERE stream_id=?", done)
                    _details["done"] += len(done)
                # Blocked or flaky: stop and pick up where we left off next run.
                if blocked or errors > len(results) // 2:
                    break
        finally:
            _details["running"] = False
            await api.close()


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def background_refresh() -> None:
    """Refresh on startup only if the cache is stale. Never blocks the UI."""
    last = int(db.get_meta("last_sync", "0"))
    if (time.time() - last) > config.SYNC_INTERVAL_HOURS * 3600:
        await full_sync()
    else:
        await sync_movie_details()
