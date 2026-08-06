"""Resumable offline downloads.

Xtream serves VOD and episodes as ordinary HTTP files, so downloads are just
ranged GETs. State lives in SQLite, which means a half-finished download
survives closing the app and picks up exactly where it stopped.

The account allows a single concurrent connection, so downloads automatically
yield to playback: start a video and the queue pauses itself, quit the player
and it resumes. Nothing for you to remember.
"""
import asyncio
import os
import re
import time
from pathlib import Path

import httpx

import config
import db
import player

CHUNK = 1 << 18  # 256 KiB
_tasks: dict[tuple[str, int], asyncio.Task] = {}
_supervisor: asyncio.Task | None = None


def _safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return (s or "video")[:120]


def concurrency() -> int:
    """Never exceed what the provider allows, or downloads fight each other."""
    allowed = int(db.get_meta("max_connections", "1") or 1)
    return max(1, min(config.MAX_CONCURRENT_DOWNLOADS, allowed))


def enqueue(kind: str, item_id: int, url: str, title: str, ext: str,
            series_id: int | None = None) -> dict:
    existing = db.one("SELECT * FROM downloads WHERE kind=? AND item_id=?",
                      (kind, item_id))
    if existing and existing["status"] in ("done", "downloading", "queued"):
        return existing

    folder = config.DOWNLOAD_DIR / ("series" if kind == "episode" else "movies")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe_name(title)}.{ext or 'mkv'}"

    db.execute(
        "INSERT INTO downloads(kind,item_id,series_id,title,url,local_path,"
        "bytes_done,total_bytes,status,error,added_at) "
        "VALUES(?,?,?,?,?,?,0,0,'queued',NULL,?) "
        "ON CONFLICT(kind,item_id) DO UPDATE SET status='queued', error=NULL",
        (kind, item_id, series_id, title, url, str(path), db.now()))
    return db.one("SELECT * FROM downloads WHERE kind=? AND item_id=?",
                  (kind, item_id))


async def cancel(kind: str, item_id: int, delete_file: bool = True) -> None:
    task = _tasks.get((kind, item_id))
    if task and not task.done():
        task.cancel()
        # Wait for it to actually stop, otherwise the .part file is still
        # open and the unlink below silently fails on Windows.
        await asyncio.gather(task, return_exceptions=True)
    row = db.one("SELECT local_path FROM downloads WHERE kind=? AND item_id=?",
                 (kind, item_id))
    if delete_file and row and row["local_path"]:
        for p in (Path(row["local_path"]), Path(row["local_path"] + ".part")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    db.execute("DELETE FROM downloads WHERE kind=? AND item_id=?",
               (kind, item_id))


def listing() -> list[dict]:
    return db.query("SELECT * FROM downloads ORDER BY "
                    "CASE status WHEN 'downloading' THEN 0 WHEN 'queued' "
                    "THEN 1 WHEN 'paused' THEN 2 WHEN 'error' THEN 3 ELSE 4 "
                    "END, added_at DESC")


def disk_usage() -> int:
    total = 0
    for root, _, files in os.walk(config.DOWNLOAD_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


async def _download(kind: str, item_id: int) -> None:
    row = db.one("SELECT * FROM downloads WHERE kind=? AND item_id=?",
                 (kind, item_id))
    if not row:
        return

    final = Path(row["local_path"])
    part = Path(str(final) + ".part")
    start = part.stat().st_size if part.exists() else 0

    db.execute("UPDATE downloads SET status='downloading', bytes_done=?, "
               "error=NULL WHERE kind=? AND item_id=?", (start, kind, item_id))

    headers = {"Range": f"bytes={start}-"} if start else {}
    timeout = httpx.Timeout(connect=20.0, read=60.0, write=30.0, pool=30.0)

    try:
        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=True) as client:
            async with client.stream("GET", row["url"], headers=headers) as r:
                # If the server ignored our Range, start over rather than
                # silently appending to a partial file and corrupting it.
                if start and r.status_code == 200:
                    start = 0
                    part.unlink(missing_ok=True)
                elif r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")

                total = int(r.headers.get("content-length") or 0) + start
                db.execute("UPDATE downloads SET total_bytes=? "
                           "WHERE kind=? AND item_id=?", (total, kind, item_id))

                done = start
                last_write = 0.0
                with open(part, "ab" if start else "wb") as f:
                    async for chunk in r.aiter_bytes(CHUNK):
                        f.write(chunk)
                        done += len(chunk)
                        # Throttle DB writes; progress updates once a second.
                        if time.monotonic() - last_write > 1.0:
                            last_write = time.monotonic()
                            db.execute("UPDATE downloads SET bytes_done=? "
                                       "WHERE kind=? AND item_id=?",
                                       (done, kind, item_id))

        part.replace(final)
        db.execute("UPDATE downloads SET status='done', bytes_done=? "
                   "WHERE kind=? AND item_id=?", (final.stat().st_size,
                                                  kind, item_id))
    except asyncio.CancelledError:
        # Paused, not failed - the .part file keeps our place.
        size = part.stat().st_size if part.exists() else 0
        db.execute("UPDATE downloads SET status='paused', bytes_done=? "
                   "WHERE kind=? AND item_id=?", (size, kind, item_id))
        raise
    except Exception as e:
        size = part.stat().st_size if part.exists() else 0
        db.execute("UPDATE downloads SET status='error', error=?, bytes_done=? "
                   "WHERE kind=? AND item_id=?",
                   (f"{type(e).__name__}: {e}", size, kind, item_id))
    finally:
        _tasks.pop((kind, item_id), None)


async def _supervise() -> None:
    """Start queued downloads, and yield the connection to the player."""
    while True:
        try:
            playing = player.is_playing()

            if playing and _tasks:
                for task in list(_tasks.values()):
                    task.cancel()

            if not playing:
                free = concurrency() - len(_tasks)
                if free > 0:
                    pending = db.query(
                        "SELECT kind,item_id FROM downloads "
                        "WHERE status IN ('queued','paused') "
                        "ORDER BY added_at LIMIT ?", (free,))
                    for row in pending:
                        key = (row["kind"], row["item_id"])
                        if key not in _tasks:
                            _tasks[key] = asyncio.create_task(
                                _download(*key))
        except Exception:
            pass
        await asyncio.sleep(1.5)


def start_supervisor() -> None:
    global _supervisor
    if _supervisor is None or _supervisor.done():
        # Anything left mid-flight from a previous run goes back in the queue.
        db.execute("UPDATE downloads SET status='queued' "
                   "WHERE status='downloading'")
        _supervisor = asyncio.create_task(_supervise())
