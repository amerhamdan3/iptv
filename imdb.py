"""On-demand IMDb lookup: rating, votes, plot, credits for one title.

IMDb has no public API and its title pages sit behind a bot challenge, so
this uses the two endpoints its own site calls: the search-suggestion feed to
find the tt id, and the GraphQL API for the details. Results are cached in
SQLite, so each title is fetched only when someone asks for it.
"""
import json
import re
from urllib.parse import quote

import httpx

import db
from sync import year_from

SUGGEST = "https://v3.sg.media-imdb.com/suggestion/x/{q}.json"
GRAPHQL = "https://caching.graphql.imdb.com/"
TIMEOUT = httpx.Timeout(15.0)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# IMDb title types that count as a match for each of our kinds.
TYPES = {
    "vod": {"movie", "tvMovie", "video", "short", "tvSpecial"},
    "series": {"tvSeries", "tvMiniSeries"},
}

QUERY = """
query ($id: ID!) {
  title(id: $id) {
    titleText { text }
    titleType { id }
    releaseYear { year endYear }
    ratingsSummary { aggregateRating voteCount }
    metacritic { metascore { score } }
    plot { plotText { plainText } }
    runtime { seconds }
    certificate { rating }
    genres { genres { text } }
    countriesOfOrigin { countries { text } }
    spokenLanguages { spokenLanguages { text } }
    primaryImage { url }
    principalCredits {
      category { text }
      credits(limit: 6) { name { nameText { text } } }
    }
  }
}"""

_TT = re.compile(r"tt\d{5,}")
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN = re.compile(r"[A-Za-z]")


def clean_title(name: str) -> str:
    """The searchable title out of a provider name.

    "Vacation Friends (2021) أصدقاء العُطلة" -> "Vacation Friends"
    "Hereditary - 2018"                        -> "Hereditary"
    "The Last of Us - الأخير منا - مترجم"       -> "The Last of Us"
    """
    # Language/country tags some providers prefix: "EN - ", "|AR| ", "[FR] ".
    s = re.sub(r"^\s*(?:[\[|(][A-Z]{2,3}[\]|)]|[A-Z]{2,3}\s*[-:|])\s*", "",
               str(name or ""))
    # Drop the release year and everything after it (usually a translation).
    m = re.search(r"[\(\[]?\s*(?:19|20)\d{2}\s*[\)\]]?", s)
    if m and m.start() > 0:
        s = s[:m.start()]
    # "English / Arabic" or "English - Arabic": keep the Latin-script part.
    parts = [p.strip(" -|/:") for p in re.split(r"\s+[/|-]\s+|\|", s)]
    parts = [p for p in parts if p]
    latin = [p for p in parts if _LATIN.search(p) and not _ARABIC.search(p)]
    s = (latin or parts or [s])[0]
    return re.sub(r"\s+", " ", s).strip(" -|/:")


async def _search(client: httpx.AsyncClient, title: str) -> list[dict]:
    q = re.sub(r"[^\w\s']", " ", title).strip().lower()
    if not q:
        return []
    r = await client.get(SUGGEST.format(q=quote(q)))
    r.raise_for_status()
    return [d for d in r.json().get("d", []) if _TT.fullmatch(d.get("id", ""))]


def _pick(results: list[dict], kind: str, year: int) -> dict | None:
    """Best candidate: right kind of title, then closest release year."""
    typed = [d for d in results if d.get("qid") in TYPES[kind]] or results
    if not typed:
        return None
    if year:
        near = [d for d in typed if d.get("y") and abs(d["y"] - year) <= 1]
        if near:
            return near[0]
    return typed[0]


async def _details(client: httpx.AsyncClient, tt: str) -> dict:
    r = await client.post(GRAPHQL, json={"query": QUERY, "variables": {"id": tt}},
                          headers={"x-imdb-client-name": "imdb-web-next"})
    r.raise_for_status()
    t = (r.json().get("data") or {}).get("title")
    if not t:
        raise LookupError(f"IMDb has no title {tt}")

    def texts(items, key="text"):
        return [i[key] for i in items or [] if i.get(key)]

    credits = {}
    for c in t.get("principalCredits") or []:
        names = [x["name"]["nameText"]["text"] for x in c.get("credits") or []]
        if names:
            credits[c["category"]["text"]] = names

    rating = t.get("ratingsSummary") or {}
    years = t.get("releaseYear") or {}
    return {
        "imdb_id": tt,
        "url": f"https://www.imdb.com/title/{tt}/",
        "title": (t.get("titleText") or {}).get("text", ""),
        "type": (t.get("titleType") or {}).get("id", ""),
        "year": years.get("year"),
        "end_year": years.get("endYear"),
        "rating": rating.get("aggregateRating"),
        "votes": rating.get("voteCount"),
        "metascore": ((t.get("metacritic") or {}).get("metascore") or {}).get("score"),
        "plot": ((t.get("plot") or {}).get("plotText") or {}).get("plainText", ""),
        "runtime_sec": (t.get("runtime") or {}).get("seconds"),
        "certificate": (t.get("certificate") or {}).get("rating"),
        "genres": texts((t.get("genres") or {}).get("genres")),
        "countries": texts((t.get("countriesOfOrigin") or {}).get("countries")),
        "languages": texts((t.get("spokenLanguages") or {}).get("spokenLanguages")),
        "poster": (t.get("primaryImage") or {}).get("url"),
        "credits": credits,
    }


def cached(kind: str, item_id: int) -> dict | None:
    row = db.one("SELECT data FROM imdb WHERE kind=? AND item_id=?",
                 (kind, item_id))
    return json.loads(row["data"]) if row else None


async def lookup(kind: str, item_id: int, name: str, year: int = 0,
                 imdb_id: str = "") -> dict:
    """Fetch and cache IMDb data for one item.

    `imdb_id` (a tt id or any IMDb URL) skips the search, for when the
    automatic match picked the wrong title.
    """
    forced = _TT.search(imdb_id or "")
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS,
                                 follow_redirects=True) as client:
        if forced:
            tt = forced.group(0)
        else:
            title = clean_title(name)
            hit = _pick(await _search(client, title), kind,
                        year or year_from(name))
            if not hit:
                raise LookupError(f'No IMDb match for "{title}"')
            tt = hit["id"]
        data = await _details(client, tt)

    db.execute(
        "INSERT INTO imdb(kind,item_id,imdb_id,data,fetched_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(kind,item_id) DO UPDATE SET "
        "imdb_id=excluded.imdb_id, data=excluded.data, "
        "fetched_at=excluded.fetched_at",
        (kind, item_id, tt, json.dumps(data), db.now()))
    return data
