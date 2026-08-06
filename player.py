"""Launch mpv and track exactly where you got to.

mpv exposes a JSON IPC channel over a Windows named pipe. We poll it for
time-pos every couple of seconds and persist that to SQLite, so resume
survives closing the player, closing the app, or a hard crash.
"""
import json
import os
import subprocess
import threading
import time

import config
import db

PIPE = r"\\.\pipe\iptv-mpv"
POLL_SECONDS = 2.0
# Below this, a stop is treated as "changed my mind" rather than progress.
MIN_TRACK_SECONDS = 15
# At or past this fraction, the item counts as watched.
COMPLETE_AT = 0.90

_current: dict | None = None
_lock = threading.Lock()


class MpvIPC:
    """Minimal JSON-IPC client over the named pipe."""

    def __init__(self, path: str = PIPE):
        self.path = path
        self.f = None
        self._rid = 0
        self._buf = b""

    def connect(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.f = open(self.path, "r+b", buffering=0)
                return True
            except OSError:
                time.sleep(0.2)
        return False

    def get(self, prop: str):
        """Send get_property and return the value, or None."""
        if not self.f:
            return None
        self._rid += 1
        rid = self._rid
        try:
            self.f.write(json.dumps(
                {"command": ["get_property", prop], "request_id": rid}
            ).encode() + b"\n")
        except OSError:
            return None

        # mpv interleaves async events with replies; match on request_id.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            line = self._readline()
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("request_id") == rid:
                return msg.get("data") if msg.get("error") == "success" else None
        return None

    def _readline(self):
        while b"\n" not in self._buf:
            try:
                chunk = self.f.read(1)
            except OSError:
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("utf-8", "replace")

    def close(self):
        if self.f:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None


def is_playing() -> bool:
    with _lock:
        return _current is not None and _current["proc"].poll() is None


def current() -> dict | None:
    with _lock:
        if not _current or _current["proc"].poll() is not None:
            return None
        return {k: _current[k] for k in
                ("kind", "item_id", "title", "position", "duration")}


def stop() -> None:
    with _lock:
        proc = _current["proc"] if _current else None
    if proc and proc.poll() is None:
        proc.terminate()


def play(kind: str, item_id: int, url: str, title: str,
         series_id: int | None = None, duration: float = 0.0,
         resume: float = 0.0, local_path: str | None = None) -> dict:
    """Start playback. A downloaded copy is always preferred over the stream."""
    mpv = config.find_mpv()
    if not mpv:
        raise RuntimeError("mpv not found - expected bin/mpv.exe")

    # Only one stream at a time; the account allows a single connection.
    stop()
    time.sleep(0.3)

    source = url
    offline = False
    if local_path and os.path.exists(local_path):
        source = local_path
        offline = True

    args = [
        mpv, source,
        f"--input-ipc-server={PIPE}",
        f"--title={title}",
        "--force-window=immediate",
        "--save-position-on-quit=no",
        "--keep-open=no",
    ]
    if kind != "live" and resume > MIN_TRACK_SECONDS:
        args.append(f"--start={int(resume)}")
    if kind == "live":
        # Live streams need a little slack on a flaky connection.
        args += ["--cache=yes", "--demuxer-max-bytes=64MiB"]

    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    session = {
        "proc": proc, "kind": kind, "item_id": item_id, "series_id": series_id,
        "title": title, "duration": duration, "position": resume,
        "offline": offline, "started_at": time.time(),
    }
    with _lock:
        globals()["_current"] = session

    threading.Thread(target=_monitor, args=(session,), daemon=True).start()
    return {"ok": True, "offline": offline, "title": title, "source": source}


def _monitor(session: dict) -> None:
    """Poll mpv for position until it exits, persisting as we go."""
    proc: subprocess.Popen = session["proc"]
    ipc = MpvIPC()

    if not ipc.connect():
        proc.wait()
        _finalize(session)
        return

    try:
        while proc.poll() is None:
            pos = ipc.get("time-pos")
            if isinstance(pos, (int, float)):
                session["position"] = float(pos)
            dur = ipc.get("duration")
            if isinstance(dur, (int, float)) and dur > 0:
                session["duration"] = float(dur)
            _persist(session)
            time.sleep(POLL_SECONDS)
    except Exception:
        pass
    finally:
        ipc.close()
        proc.wait()
        _finalize(session)


def _persist(session: dict) -> None:
    """Live TV has no meaningful position, so only record that it was watched."""
    if session["kind"] == "live":
        db.execute(
            "INSERT INTO history(kind,item_id,series_id,position_sec,"
            "duration_sec,completed,watched_at) VALUES(?,?,?,0,0,0,?) "
            "ON CONFLICT(kind,item_id) DO UPDATE SET watched_at=excluded.watched_at",
            (session["kind"], session["item_id"], db.now()))
        return

    pos = session["position"]
    if pos < MIN_TRACK_SECONDS:
        return
    dur = session["duration"] or 0
    done = 1 if dur > 0 and pos / dur >= COMPLETE_AT else 0

    db.execute(
        "INSERT INTO history(kind,item_id,series_id,position_sec,duration_sec,"
        "completed,watched_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(kind,item_id) DO UPDATE SET "
        "position_sec=excluded.position_sec, duration_sec=excluded.duration_sec, "
        "completed=MAX(history.completed, excluded.completed), "
        "watched_at=excluded.watched_at",
        (session["kind"], session["item_id"], session["series_id"],
         pos, dur, done, db.now()))


def _finalize(session: dict) -> None:
    _persist(session)
    with _lock:
        if globals().get("_current") is session:
            globals()["_current"] = None
