"""Configuration loaded from .env, plus resolved paths to local binaries."""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

HOST = os.getenv("XTREAM_HOST", "").rstrip("/")
USER = os.getenv("XTREAM_USER", "")
PASS = os.getenv("XTREAM_PASS", "")

WEB_HOST = os.getenv("HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("PORT", "8000"))

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "24"))

DB_PATH = ROOT / "iptv.db"
CACHE_DIR = ROOT / "cache" / "img"
DOWNLOAD_DIR = ROOT / os.getenv("DOWNLOAD_DIR", "downloads")
BIN_DIR = ROOT / "bin"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def find_mpv() -> str | None:
    """Prefer the bundled portable build, fall back to whatever is on PATH."""
    local = BIN_DIR / "mpv.exe"
    if local.exists():
        return str(local)
    return shutil.which("mpv")


# Stream URL builders. Xtream serves VOD and series episodes as plain HTTP
# files, which is what makes resumable offline downloads possible.
def live_url(stream_id: int, ext: str = "ts") -> str:
    return f"{HOST}/live/{USER}/{PASS}/{stream_id}.{ext}"


def vod_url(stream_id: int, ext: str = "mp4") -> str:
    return f"{HOST}/movie/{USER}/{PASS}/{stream_id}.{ext}"


def episode_url(episode_id: int, ext: str = "mkv") -> str:
    return f"{HOST}/series/{USER}/{PASS}/{episode_id}.{ext}"
