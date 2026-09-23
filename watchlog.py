"""Two-way link with the personal watchlog service (a separate Cloudflare app).

The watchlog is the shared list of what I've watched, how I rated it, and
what's been suggested to me. Every machine running this app reads and writes
the same list, so a rating given here shows up on the laptop, the phone page,
or anything else that talks to the watchlog API.

Reads come from an in-memory copy refreshed in the background, so browsing
never waits on the network. Writes go straight out; if the service can't be
reached they wait in a local outbox and are retried on the next refresh.
"""
import asyncio
import json
import re
import threading
import time
import unicodedata

import httpx

import config
import db
import imdb
from sync import year_from

TIMEOUT = httpx.Timeout(10.0)
REFRESH_SECONDS = 60
MATCH_TTL = 3600            # how long a list item -> catalog match is trusted
FIELDS = {"rating", "status", "watched_at", "note"}

_lock = threading.Lock()
_items: dict[str, dict] = {}                 # watchlog id -> item
_by_title: dict[tuple[str, str], list] = {}  # (type, normalised title) -> items
_loaded_at = 0.0
_refreshing = False
_last_error: str | None = None
_matches: dict[str, tuple[float, dict | None]] = {}


def enabled() -> bool:
    return bool(config.WATCHLOG_URL and config.WATCHLOG_KEY)


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.WATCHLOG_URL, timeout=TIMEOUT,
                        headers={"Authorization": f"Bearer {config.WATCHLOG_KEY}"})


def norm(title: str) -> str:
    """Comparable form of a title: no accents, case, spaces or punctuation."""
    s = unicodedata.normalize("NFKD", str(title or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[\W_]+", "", s.replace("&", "and"))


def _wl_type(kind: str) -> str:
    return "show" if kind == "series" else "movie"


def _today() -> str:
    return time.strftime("%Y-%m-%d")  # local date, not the server's UTC one


# ------------------------------------------------------------------ reading

def _index(items: list[dict]) -> None:
    global _items, _by_title, _loaded_at
    by_title: dict[tuple[str, str], list] = {}
    for it in items:
        by_title.setdefault((it["type"], norm(it["title"])), []).append(it)
    with _lock:
        _items = {it["id"]: it for it in items}
        _by_title = by_title
        _loaded_at = time.time()


def _remember(item: dict) -> None:
    with _lock:
        old = _items.get(item["id"])
        _items[item["id"]] = item
        key = (item["type"], norm(item["title"]))
        bucket = [i for i in _by_title.get(key, []) if i["id"] != item["id"]]
        _by_title[key] = bucket + [item]
        if old and (old["type"], norm(old["title"])) != key:
            k = (old["type"], norm(old["title"]))
            _by_title[k] = [i for i in _by_title.get(k, []) if i["id"] != item["id"]]


def refresh() -> None:
    """Reload the whole list (it's small), then retry anything in the outbox."""
    global _refreshing, _last_error
    if not enabled():
        return
    try:
        items, offset = [], 0
        with _client() as c:
            while True:
                r = c.get("/api/items", params={"limit": 1000, "offset": offset})
                r.raise_for_status()
                page = r.json()
                items += page["items"]
                offset += len(page["items"])
                if not page["items"] or offset >= page["total"]:
                    break
        _index(items)
        _last_error = None
        _flush_outbox()
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
    finally:
        _refreshing = False


def ensure_fresh(block: bool = False) -> None:
    """Refresh in the background when stale; `block` waits for a first load."""
    global _refreshing
    if not enabled() or time.time() - _loaded_at < REFRESH_SECONDS:
        return
    if block and not _loaded_at:
        refresh()
        return
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(target=refresh, daemon=True).start()


def _catalog_row(kind: str, item_id: int) -> dict | None:
    if kind == "series":
        return db.one("SELECT name, year, cover AS icon FROM series "
                      "WHERE series_id=?", (item_id,))
    return db.one("SELECT name, year, icon FROM vod WHERE stream_id=?",
                  (item_id,))


def entry_for(kind: str, item_id: int, name: str | None = None,
              year: int | None = None) -> dict | None:
    """The watchlog item for a catalog title, if there is one.

    Matches on the IMDb id when this machine has looked the title up, and
    otherwise on title + year, so entries made on the phone or another
    machine still line up with this catalog.
    """
    if kind not in ("vod", "series"):
        return None
    wl_type = _wl_type(kind)
    cached = db.one("SELECT imdb_id, data FROM imdb WHERE kind=? AND item_id=?",
                    (kind, item_id))
    if cached and cached["imdb_id"] in _items:
        return _items[cached["imdb_id"]]

    if name is None:
        row = _catalog_row(kind, item_id)
        if not row:
            return None
        name, year = row["name"], row["year"]
    y = year or year_from(name)
    titles = {norm(imdb.clean_title(name))}
    if cached:
        titles.add(norm(json.loads(cached["data"]).get("title", "")))

    cands = [i for t in titles if t for i in _by_title.get((wl_type, t), [])]
    if not cands:
        return None
    if y:
        near = [i for i in cands if i.get("year") and abs(i["year"] - y) <= 1]
        if near:
            return near[0]
        undated = [i for i in cands if not i.get("year")]
        return undated[0] if len(undated) == 1 else None
    return cands[0] if len(cands) == 1 else None


def summary(item: dict | None) -> dict | None:
    """What the UI needs about an entry: status and score."""
    if not item:
        return None
    return {k: item.get(k) for k in
            ("id", "status", "rating", "watched_at", "reason", "pending")}


def annotate(rows: list[dict]) -> list[dict]:
    """Attach `mine` (my status/rating) to catalog rows for the poster badges."""
    if not enabled():
        return rows
    ensure_fresh()
    for r in rows:
        if r.get("kind") in ("vod", "series"):
            e = entry_for(r["kind"], r["id"], r.get("name"), r.get("year"))
            if e:
                r["mine"] = summary(e)
    return rows


# ------------------------------------------------------------------ writing

def _slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _identify(kind: str, item_id: int) -> dict:
    """The watchlog id and title details for a catalog item."""
    row = _catalog_row(kind, item_id)
    if not row:
        raise LookupError("unknown item")
    existing = entry_for(kind, item_id, row["name"], row["year"])
    if existing:
        return {"id": existing["id"], "existing": True}

    data = imdb.cached(kind, item_id)
    if not data:
        try:
            data = asyncio.run(imdb.lookup(kind, item_id, row["name"],
                                           row["year"] or 0))
        except LookupError:
            data = None  # not on IMDb: keep it under a stable custom id
    if data:
        return {"id": data["imdb_id"], "title": data["title"],
                "type": _wl_type(kind), "year": data.get("year"),
                "poster": data.get("poster")}
    title = imdb.clean_title(row["name"])
    return {"id": f"custom:{_slug(title) or f'iptv-{kind}-{item_id}'}",
            "title": title, "type": _wl_type(kind),
            "year": row["year"] or year_from(row["name"]) or None,
            "poster": None}


def _send(kind: str, item_id: int, fields: dict) -> dict:
    ident = _identify(kind, item_id)
    body = dict(fields)
    if not ident.pop("existing", False):
        body = {k: v for k, v in ident.items() if k != "id"} | body
        if body.get("poster"):
            # IMDb's full-size posters are huge; ask the CDN for a thumbnail.
            body["poster"] = re.sub(r"\._V1_.*\.jpg$", "._V1_UX200_.jpg",
                                    body["poster"])
    with _client() as c:
        r = c.put(f"/api/items/{ident['id']}", json=body)
        r.raise_for_status()
        item = r.json()
    _remember(item)
    return item


def update(kind: str, item_id: int, fields: dict) -> dict:
    """Change my entry for a catalog item. Queues it if the service is down."""
    fields = {k: v for k, v in fields.items() if k in FIELDS}
    # A score means you've seen it, unless it's already marked watched, in
    # which case the original watch date stays.
    if "status" not in fields and fields.get("rating"):
        current = entry_for(kind, item_id)
        if not current or current["status"] != "watched":
            fields["status"] = "watched"
    if fields.get("status") == "watched" and "watched_at" not in fields:
        fields["watched_at"] = _today()
    try:
        return summary(_send(kind, item_id, fields))
    except (httpx.HTTPError, OSError) as e:
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
            raise  # a real rejection, not an outage: retrying won't help
        db.execute("INSERT INTO watchlog_outbox(kind,item_id,fields,created_at) "
                   "VALUES(?,?,?,?)", (kind, item_id, json.dumps(fields), db.now()))
        return {**fields, "pending": True}


def remove(kind: str, item_id: int) -> bool:
    """Take a title off the list entirely. False if it wasn't on it."""
    entry = entry_for(kind, item_id)
    if not entry:
        return False
    with _client() as c:
        r = c.delete(f"/api/items/{entry['id']}")
        if r.status_code != 404:
            r.raise_for_status()
    with _lock:
        _items.pop(entry["id"], None)
        key = (entry["type"], norm(entry["title"]))
        _by_title[key] = [i for i in _by_title.get(key, []) if i["id"] != entry["id"]]
    return True


def _flush_outbox() -> None:
    for row in db.query("SELECT * FROM watchlog_outbox ORDER BY id"):
        try:
            _send(row["kind"], row["item_id"], json.loads(row["fields"]))
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                return  # still down; keep the rest queued
        except Exception:
            return
        db.execute("DELETE FROM watchlog_outbox WHERE id=?", (row["id"],))


def mark_watched(kind: str, item_id: int) -> None:
    """Called when playback crosses the "watched" line. Never raises.

    An episode marks its show as watched: the list tracks titles, and the
    app itself keeps the per-episode progress.
    """
    if not enabled():
        return
    try:
        if kind == "episode":
            row = db.one("SELECT series_id FROM episodes WHERE episode_id=?",
                         (item_id,))
            if not row:
                return
            kind, item_id = "series", row["series_id"]
        if kind in ("vod", "series"):
            ensure_fresh(block=True)
            update(kind, item_id, {"status": "watched"})
    except Exception:
        pass


def mark_watched_async(kind: str, item_id: int) -> None:
    threading.Thread(target=mark_watched, args=(kind, item_id),
                     daemon=True).start()


# ------------------------------------------------------------------ my list

def _find_in_catalog(item: dict) -> dict | None:
    """The library title a list item refers to, if this catalog has it."""
    now = time.time()
    hit = _matches.get(item["id"])
    if hit and now - hit[0] < MATCH_TTL:
        return hit[1]

    kind = "series" if item["type"] == "show" else "vod"
    found = None
    row = db.one("SELECT item_id FROM imdb WHERE imdb_id=? AND kind=?",
                 (item["id"], kind))
    if row:
        found = row["item_id"]
    else:
        want, y = norm(item["title"]), item.get("year")
        words = [w for w in re.split(r"\W+", item["title"]) if len(w) > 1]
        if want and words:
            longest = max(words, key=len)
            table, key = (("series", "series_id") if kind == "series"
                          else ("vod", "stream_id"))
            for r in db.query(f"SELECT {key} AS id, name, year FROM {table} "
                              f"WHERE name LIKE ? LIMIT 200", (f"%{longest}%",)):
                if norm(imdb.clean_title(r["name"])) != want:
                    continue
                ry = r["year"] or year_from(r["name"])
                if y and ry and abs(ry - y) > 1:
                    continue
                found = r["id"]
                break

    match = None
    if found is not None:
        row = _catalog_row(kind, found)
        match = {"kind": kind, "id": found, "name": row["name"],
                 "icon": row["icon"]} if row else None
    _matches[item["id"]] = (now, match)
    return match


def my_list() -> dict:
    if not enabled():
        return {"enabled": False}
    ensure_fresh(block=True)
    groups: dict[str, list] = {"suggested": [], "watchlist": [], "watched": []}
    for it in _items.values():
        groups.setdefault(it["status"], []).append(
            {**it, "match": _find_in_catalog(it)})
    groups["suggested"].sort(key=lambda i: i["created_at"], reverse=True)
    groups["watchlist"].sort(key=lambda i: i["created_at"], reverse=True)
    groups["watched"].sort(key=lambda i: (i.get("watched_at") or "", i["updated_at"]),
                           reverse=True)
    pending = db.one("SELECT COUNT(*) AS n FROM watchlog_outbox")["n"]
    return {"enabled": True, "error": _last_error, "pending": pending, **groups}


def status() -> dict:
    if not enabled():
        return {"enabled": False}
    return {"enabled": True, "items": len(_items), "error": _last_error,
            "pending": db.one("SELECT COUNT(*) AS n FROM watchlog_outbox")["n"]}
