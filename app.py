"""IPTV browser: FastAPI backend serving a local web UI.

Run:  python app.py     then open http://127.0.0.1:8000
"""
import asyncio
import hashlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import db
import downloader
import player
import sync

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    downloader.start_supervisor()
    # Refresh in the background; the UI is already usable from cache.
    asyncio.create_task(sync.background_refresh())
    yield
    player.stop()


app = FastAPI(title="IPTV", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- status

@app.get("/api/status")
def api_status():
    return {
        "sync": sync.status(),
        "player": player.current(),
        "account": {
            "status": db.get_meta("account_status", ""),
            "expires": db.get_meta("account_expires", ""),
            "max_connections": db.get_meta("max_connections", "1"),
        },
        "downloads": {
            "concurrency": downloader.concurrency(),
            "disk_bytes": downloader.disk_usage(),
        },
        "mpv": bool(config.find_mpv()),
    }


@app.post("/api/sync")
async def api_sync(force: bool = False):
    asyncio.create_task(sync.full_sync(force=force))
    return {"started": True}


# ---------------------------------------------------------------- browse

@app.get("/api/categories")
def api_categories(kind: str):
    table = {"live": "live", "vod": "vod", "series": "series"}.get(kind)
    if not table:
        raise HTTPException(400, "bad kind")
    id_col = "series_id" if kind == "series" else "stream_id"
    return db.query(
        f"SELECT c.id, c.name, COUNT(t.{id_col}) AS count "
        f"FROM categories c LEFT JOIN {table} t ON t.category_id = c.id "
        f"WHERE c.kind=? GROUP BY c.id, c.name HAVING count > 0 "
        f"ORDER BY c.name", (kind,))


@app.get("/api/browse")
def api_browse(kind: str, category: str = "", offset: int = 0,
               limit: int = 120, favorites: bool = False):
    if kind == "series":
        base = ("SELECT s.series_id AS id, s.name, s.cover AS icon, s.rating, "
                "s.genre, 'series' AS kind, "
                "(f.item_id IS NOT NULL) AS favorite "
                "FROM series s LEFT JOIN favorites f "
                "  ON f.kind='series' AND f.item_id=s.series_id")
        where, params = [], []
    elif kind in ("live", "vod"):
        base = (f"SELECT t.stream_id AS id, t.name, t.icon, "
                f"{'t.rating' if kind == 'vod' else '0'} AS rating, "
                f"'' AS genre, '{kind}' AS kind, "
                f"(f.item_id IS NOT NULL) AS favorite "
                f"FROM {kind} t LEFT JOIN favorites f "
                f"  ON f.kind='{kind}' AND f.item_id=t.stream_id")
        where, params = [], []
    else:
        raise HTTPException(400, "bad kind")

    if category:
        where.append("t.category_id=?" if kind != "series"
                     else "s.category_id=?")
        params.append(category)
    if favorites:
        where.append("f.item_id IS NOT NULL")

    sql = base + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY favorite DESC, name LIMIT ? OFFSET ?"
    params += [limit, offset]
    return db.query(sql, params)


@app.get("/api/search")
def api_search(q: str, kind: str = "", limit: int = 80):
    q = q.strip()
    if len(q) < 2:
        return []

    # Prefix-match every term so results appear as you type.
    terms = [t for t in q.replace('"', " ").split() if t]
    fts = " ".join(f'"{t}"*' for t in terms)

    sql = ("SELECT s.kind, s.item_id AS id, s.name FROM search s "
           "WHERE search MATCH ?")
    params: list = [fts]
    if kind:
        sql += " AND s.kind=?"
        params.append(kind)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = db.query(sql, params)
    except Exception:
        # Fall back to LIKE if the query upsets the FTS parser.
        like = f"%{q}%"
        sql = ("SELECT kind, item_id AS id, name FROM search "
               "WHERE name LIKE ?")
        params = [like]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " LIMIT ?"
        params.append(limit)
        rows = db.query(sql, params)

    # Decorate with artwork and favourite state.
    for r in rows:
        if r["kind"] == "series":
            extra = db.one("SELECT cover AS icon, rating FROM series "
                           "WHERE series_id=?", (r["id"],))
        else:
            extra = db.one(f"SELECT icon, "
                           f"{'rating' if r['kind'] == 'vod' else '0 AS rating'}"
                           f" FROM {r['kind']} WHERE stream_id=?", (r["id"],))
        r.update(extra or {"icon": "", "rating": 0})
        fav = db.one("SELECT 1 FROM favorites WHERE kind=? AND item_id=?",
                     (r["kind"], r["id"]))
        r["favorite"] = bool(fav)
    return rows


@app.get("/api/series/{series_id}")
async def api_series(series_id: int, refresh: bool = False):
    info = db.one("SELECT * FROM series WHERE series_id=?", (series_id,))
    if not info:
        raise HTTPException(404, "unknown series")
    eps = await sync.sync_episodes(series_id, force=refresh)
    fav = db.one("SELECT 1 FROM favorites WHERE kind='series' AND item_id=?",
                 (series_id,))
    info["favorite"] = bool(fav)
    return {"series": info, "episodes": eps,
            "progress": _series_progress(series_id)}


# ------------------------------------------------- continue watching

def _next_episode(series_id: int, season: int, ep_num: int) -> dict | None:
    return db.one(
        "SELECT * FROM episodes WHERE series_id=? "
        "AND (season > ? OR (season = ? AND ep_num > ?)) "
        "ORDER BY season, ep_num LIMIT 1",
        (series_id, season, season, ep_num))


def _series_progress(series_id: int) -> dict | None:
    """The most recently watched episode of a show, plus what comes next."""
    cur = db.one(
        "SELECT h.*, e.season, e.ep_num, e.title AS ep_title, e.ext, "
        "       e.duration_secs "
        "FROM history h JOIN episodes e ON e.episode_id=h.item_id "
        "WHERE h.kind='episode' AND h.series_id=? "
        "ORDER BY h.watched_at DESC LIMIT 1", (series_id,))
    if not cur:
        return None

    nxt = _next_episode(series_id, cur["season"], cur["ep_num"])
    resume = (not cur["completed"]
              and cur["position_sec"] > player.MIN_TRACK_SECONDS)
    return {
        "current": cur,
        "next": nxt,
        # If you were mid-episode, that's where you go; otherwise, next up.
        "action": "resume" if resume else ("next" if nxt else "done"),
    }


@app.get("/api/continue")
def api_continue(limit: int = 20):
    out = []

    series_ids = db.query(
        "SELECT series_id, MAX(watched_at) AS last FROM history "
        "WHERE kind='episode' AND series_id IS NOT NULL "
        "GROUP BY series_id ORDER BY last DESC LIMIT ?", (limit,))
    for row in series_ids:
        s = db.one("SELECT series_id, name, cover FROM series WHERE series_id=?",
                   (row["series_id"],))
        if not s:
            continue
        prog = _series_progress(row["series_id"])
        if not prog or prog["action"] == "done":
            continue
        out.append({"kind": "series", "id": s["series_id"], "name": s["name"],
                    "icon": s["cover"], "last": row["last"], "progress": prog})

    movies = db.query(
        "SELECT h.*, v.name, v.icon, v.ext FROM history h "
        "JOIN vod v ON v.stream_id=h.item_id "
        "WHERE h.kind='vod' AND h.completed=0 AND h.position_sec > ? "
        "ORDER BY h.watched_at DESC LIMIT ?",
        (player.MIN_TRACK_SECONDS, limit))
    for m in movies:
        out.append({"kind": "vod", "id": m["item_id"], "name": m["name"],
                    "icon": m["icon"], "last": m["watched_at"],
                    "progress": {"current": m, "next": None,
                                 "action": "resume"}})

    out.sort(key=lambda x: x["last"] or 0, reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- favorites

class FavIn(BaseModel):
    kind: str
    item_id: int


@app.post("/api/favorite")
def api_favorite(body: FavIn):
    existing = db.one("SELECT 1 FROM favorites WHERE kind=? AND item_id=?",
                      (body.kind, body.item_id))
    if existing:
        db.execute("DELETE FROM favorites WHERE kind=? AND item_id=?",
                   (body.kind, body.item_id))
        return {"favorite": False}
    db.execute("INSERT INTO favorites(kind,item_id,added_at) VALUES(?,?,?)",
               (body.kind, body.item_id, db.now()))
    return {"favorite": True}


@app.get("/api/favorites")
def api_favorites():
    out = []
    for f in db.query("SELECT * FROM favorites ORDER BY added_at DESC"):
        if f["kind"] == "series":
            r = db.one("SELECT series_id AS id, name, cover AS icon "
                       "FROM series WHERE series_id=?", (f["item_id"],))
        else:
            r = db.one(f"SELECT stream_id AS id, name, icon FROM {f['kind']} "
                       f"WHERE stream_id=?", (f["item_id"],))
        if r:
            out.append({**r, "kind": f["kind"], "favorite": True})
    return out


# ---------------------------------------------------------------- playback

class PlayIn(BaseModel):
    kind: str          # live | vod | episode
    item_id: int
    restart: bool = False


def _resolve(kind: str, item_id: int) -> dict:
    """Build the stream URL and gather resume state for an item."""
    if kind == "live":
        row = db.one("SELECT stream_id, name FROM live WHERE stream_id=?",
                     (item_id,))
        if not row:
            raise HTTPException(404, "unknown channel")
        return {"url": config.live_url(item_id), "title": row["name"],
                "series_id": None, "duration": 0, "ext": "ts"}

    if kind == "vod":
        row = db.one("SELECT stream_id, name, ext FROM vod WHERE stream_id=?",
                     (item_id,))
        if not row:
            raise HTTPException(404, "unknown movie")
        return {"url": config.vod_url(item_id, row["ext"]),
                "title": row["name"], "series_id": None, "duration": 0,
                "ext": row["ext"]}

    if kind == "episode":
        row = db.one(
            "SELECT e.*, s.name AS series_name FROM episodes e "
            "JOIN series s ON s.series_id=e.series_id WHERE e.episode_id=?",
            (item_id,))
        if not row:
            raise HTTPException(404, "unknown episode")
        label = (f"{row['series_name']} - S{row['season']:02d}"
                 f"E{row['ep_num']:02d}")
        return {"url": config.episode_url(item_id, row["ext"]),
                "title": label, "series_id": row["series_id"],
                "duration": row["duration_secs"] or 0, "ext": row["ext"]}

    raise HTTPException(400, "bad kind")


@app.post("/api/play")
def api_play(body: PlayIn):
    if not config.find_mpv():
        raise HTTPException(500, "mpv not found - expected bin/mpv.exe")

    meta = _resolve(body.kind, body.item_id)

    resume = 0.0
    h = db.one("SELECT position_sec, duration_sec, completed FROM history "
               "WHERE kind=? AND item_id=?", (body.kind, body.item_id))
    if h and not body.restart and not h["completed"]:
        resume = h["position_sec"] or 0.0
    duration = meta["duration"] or (h["duration_sec"] if h else 0) or 0

    # Prefer an offline copy when we have one.
    d = db.one("SELECT local_path FROM downloads WHERE kind=? AND item_id=? "
               "AND status='done'", (body.kind, body.item_id))

    return player.play(
        kind=body.kind, item_id=body.item_id, url=meta["url"],
        title=meta["title"], series_id=meta["series_id"],
        duration=duration, resume=resume,
        local_path=d["local_path"] if d else None)


@app.post("/api/stop")
def api_stop():
    player.stop()
    return {"ok": True}


@app.get("/api/player")
def api_player():
    return {"current": player.current()}


class MarkIn(BaseModel):
    kind: str
    item_id: int
    completed: bool = True
    series_id: int | None = None


@app.post("/api/mark")
def api_mark(body: MarkIn):
    """Manual override for the odd case where auto-tracking got it wrong."""
    series_id = body.series_id
    if body.kind == "episode" and series_id is None:
        row = db.one("SELECT series_id FROM episodes WHERE episode_id=?",
                     (body.item_id,))
        series_id = row["series_id"] if row else None

    db.execute(
        "INSERT INTO history(kind,item_id,series_id,position_sec,duration_sec,"
        "completed,watched_at) VALUES(?,?,?,0,0,?,?) "
        "ON CONFLICT(kind,item_id) DO UPDATE SET completed=excluded.completed, "
        "position_sec=CASE WHEN excluded.completed=1 THEN 0 "
        "  ELSE history.position_sec END, watched_at=excluded.watched_at",
        (body.kind, body.item_id, series_id, int(body.completed), db.now()))
    return {"ok": True}


# ---------------------------------------------------------------- downloads

class DownloadIn(BaseModel):
    kind: str          # vod | episode
    item_id: int


@app.post("/api/download")
def api_download(body: DownloadIn):
    if body.kind not in ("vod", "episode"):
        raise HTTPException(400, "live streams cannot be downloaded")
    meta = _resolve(body.kind, body.item_id)
    return downloader.enqueue(body.kind, body.item_id, meta["url"],
                              meta["title"], meta["ext"], meta["series_id"])


@app.get("/api/downloads")
def api_downloads():
    return {"items": downloader.listing(),
            "disk_bytes": downloader.disk_usage(),
            "paused_for_playback": player.is_playing()}


@app.delete("/api/download/{kind}/{item_id}")
async def api_download_delete(kind: str, item_id: int, keep_file: bool = False):
    await downloader.cancel(kind, item_id, delete_file=not keep_file)
    return {"ok": True}


# ---------------------------------------------------------------- images

@app.get("/img")
async def api_img(u: str):
    """Cache artwork on disk. Remote posters are what make these UIs crawl."""
    if not u.startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    key = hashlib.sha1(u.encode()).hexdigest()
    path = config.CACHE_DIR / key

    if path.exists():
        return FileResponse(path, headers={"Cache-Control": "max-age=604800"})

    try:
        async with httpx.AsyncClient(timeout=15.0,
                                     follow_redirects=True) as c:
            r = await c.get(u)
            r.raise_for_status()
            path.write_bytes(r.content)
        return Response(r.content, media_type=r.headers.get(
            "content-type", "image/jpeg"),
            headers={"Cache-Control": "max-age=604800"})
    except Exception:
        return Response(status_code=404)


# ---------------------------------------------------------------- static

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    if not config.HOST or not config.USER:
        print("Missing Xtream credentials in .env")
        sys.exit(1)
    print(f"IPTV  ->  http://{config.WEB_HOST}:{config.WEB_PORT}")
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT,
                log_level="warning")
