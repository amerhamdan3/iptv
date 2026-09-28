"""Share playback positions through the watchlog, so a movie or episode stopped
here resumes at the same spot on the TVs (Lumina TV), and the other way round.

- Push: while mpv plays at most every 30 s, and at once on stop / finish.
  Saves wait in the progress_outbox table when offline; each carries its age,
  so the watchlog can tell which is newest on its own clock ("newest wins").
- Pull: every minute in the background, and right before playing something.
  Positions saved by other devices overwrite this machine's history.

Titles are keyed by the provider's id ("vod:123", "episode:456"), the same on
every device signed in to the same subscription. Off when the watchlog is off.
"""
import asyncio
import json
import socket
import threading
import time

import httpx

import config
import db

PUSH_EVERY = 30.0
PULL_EVERY = 60.0
DEVICE = f"pc-{socket.gethostname()}"[:80]

_last_push: dict[str, float] = {}
_flush_lock = threading.Lock()
_pull_lock = threading.Lock()
_started = False


def enabled() -> bool:
    return bool(config.WATCHLOG_URL and config.WATCHLOG_KEY)


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.WATCHLOG_URL, timeout=httpx.Timeout(10.0),
                        headers={"Authorization": f"Bearer {config.WATCHLOG_KEY}"})


def push(kind: str, item_id: int, series_id: int | None, position: float,
         duration: float, completed: bool, final: bool = False) -> None:
    """Queue a position for the watchlog. Never raises; `final` skips the throttle."""
    if kind not in ("vod", "episode") or not enabled():
        return
    key = f"{kind}:{item_id}"
    now = time.time()
    if not final and now - _last_push.get(key, 0) < PUSH_EVERY:
        return
    _last_push[key] = now
    body: dict = {"position": 0 if completed else round(position),
                  "duration": round(duration or 0), "completed": bool(completed),
                  "series_id": series_id, "device": DEVICE}
    try:
        if kind == "episode":
            row = db.one("SELECT e.season, e.ep_num, s.name FROM episodes e "
                         "LEFT JOIN series s ON s.series_id=e.series_id "
                         "WHERE e.episode_id=?", (item_id,))
            if row:
                body.update(season=row["season"], episode=row["ep_num"], title=row["name"])
        else:
            row = db.one("SELECT name FROM vod WHERE stream_id=?", (item_id,))
            if row:
                body["title"] = row["name"]
        db.execute("INSERT INTO progress_outbox(id,body,recorded_at) VALUES(?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET body=excluded.body, "
                   "recorded_at=excluded.recorded_at", (key, json.dumps(body), now))
    except Exception:
        return
    threading.Thread(target=flush, daemon=True).start()


def flush() -> None:
    """Send queued positions. Stops at the first outage; the rest wait."""
    if not enabled() or not _flush_lock.acquire(blocking=False):
        return
    try:
        with _client() as c:
            for row in db.query("SELECT * FROM progress_outbox ORDER BY recorded_at"):
                body = json.loads(row["body"])
                body["age_ms"] = max(0, int((time.time() - row["recorded_at"]) * 1000))
                try:
                    r = c.put(f"/api/progress/{row['id']}", json=body)
                    if r.status_code >= 500:
                        return
                except httpx.HTTPError:
                    return
                # Keep it if a newer save for the same title arrived meanwhile.
                db.execute("DELETE FROM progress_outbox WHERE id=? AND recorded_at=?",
                           (row["id"], row["recorded_at"]))
    finally:
        _flush_lock.release()


def pull() -> int:
    """Apply positions saved by other devices. Returns how many were applied."""
    if not enabled():
        return 0
    with _pull_lock:
        flush()
        since = int(db.get_meta("progress_since", "0") or 0)
        applied, new_series = 0, set()
        with _client() as c:
            while True:
                r = c.get("/api/progress", params={"since": since, "limit": 500})
                r.raise_for_status()
                page = r.json()
                for p in page["items"]:
                    since = max(since, p["updated_at"])
                    if p.get("device") == DEVICE:
                        continue
                    if db.one("SELECT 1 FROM progress_outbox WHERE id=?", (p["id"],)):
                        continue  # our own unsent save is newer
                    kind, item_id = p["id"].split(":")
                    item_id = int(item_id)
                    # When it happened, on this machine's clock.
                    at = int(time.time() - max(0, page["now"] - p["updated_at"]) / 1000)
                    local = db.one("SELECT watched_at FROM history WHERE kind=? AND item_id=?",
                                   (kind, item_id))
                    if local and (local["watched_at"] or 0) > at:
                        continue
                    done = 1 if p.get("completed") else 0
                    db.execute(
                        "INSERT INTO history(kind,item_id,series_id,position_sec,duration_sec,"
                        "completed,watched_at) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(kind,item_id) DO UPDATE SET "
                        "series_id=COALESCE(excluded.series_id, history.series_id), "
                        "position_sec=excluded.position_sec, duration_sec=excluded.duration_sec, "
                        "completed=excluded.completed, watched_at=excluded.watched_at",
                        (kind, item_id, p.get("series_id"), 0 if done else p["position"],
                         p.get("duration") or 0, done, at))
                    applied += 1
                    if kind == "episode" and p.get("series_id"):
                        new_series.add(int(p["series_id"]))
                if len(page["items"]) < 500:
                    break
        db.set_meta("progress_since", since)

    # Continue watching needs the show's episode list; fetch a few missing ones.
    missing = [s for s in new_series
               if not db.one("SELECT 1 FROM episodes WHERE series_id=? LIMIT 1", (s,))][:5]
    if missing:
        import sync  # late: sync imports the provider client
        for sid in missing:
            try:
                asyncio.run(sync.sync_episodes(sid))
            except Exception:
                pass
    return applied


def pull_quietly(timeout: float = 2.5) -> None:
    """Before playing: get the latest position, but don't hold playback up."""
    if not enabled():
        return
    t = threading.Thread(target=lambda: _safe(pull), daemon=True)
    t.start()
    t.join(timeout)


def _safe(fn) -> None:
    try:
        fn()
    except Exception:
        pass


def start() -> None:
    """Background loop: push what's queued and pull other devices' positions."""
    global _started
    if _started or not enabled():
        return
    _started = True

    def loop():
        while True:
            _safe(pull)
            time.sleep(PULL_EVERY)

    threading.Thread(target=loop, daemon=True).start()
