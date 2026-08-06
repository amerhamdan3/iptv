"""One-shot setup: dependencies, a portable mpv, and a desktop shortcut.

    python setup.py

Safe to re-run; it skips whatever is already in place.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
BIN = ROOT / "bin"
UA = {"User-Agent": "iptv-setup"}
MPV_API = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"


def step(msg):
    print(f"\n>> {msg}")


def install_deps():
    step("Installing Python dependencies")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                           "-r", str(ROOT / "requirements.txt")])
    print("   done")


def make_env():
    step("Checking .env")
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        print("   .env already exists - leaving it alone")
        return
    shutil.copy(example, env)
    print(f"   created {env}")
    print("   >>> EDIT IT NOW and put your provider host / username / password in")


def _fetch(url, path, timeout=600):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return path.stat().st_size


def install_mpv():
    """mpv plays the streams; browsers cannot decode MPEG-TS or most .mkv."""
    step("Installing mpv")
    if (BIN / "mpv.exe").exists():
        print("   bin/mpv.exe already present")
        return
    if shutil.which("mpv"):
        print("   mpv already on PATH")
        return
    if sys.platform != "win32":
        print("   Not Windows - install mpv with your package manager "
              "(e.g. 'brew install mpv' or 'apt install mpv')")
        return

    BIN.mkdir(exist_ok=True)
    # The official Windows builds are .7z with a BCJ2 filter that Python's
    # archive libraries cannot read, so grab 7-Zip's standalone extractor.
    sevenzr = BIN / "7zr.exe"
    if not sevenzr.exists():
        _fetch("https://www.7-zip.org/a/7zr.exe", sevenzr, timeout=120)

    rel = json.load(urllib.request.urlopen(
        urllib.request.Request(MPV_API, headers=UA), timeout=60))
    asset = next((a for a in rel["assets"]
                  if a["name"].startswith("mpv-x86_64-")
                  and a["name"].endswith(".7z")
                  and "dev" not in a["name"]), None)
    if not asset:
        print("   Could not find an mpv build - install it yourself from mpv.io")
        return

    archive = BIN / "mpv.7z"
    print(f"   downloading {asset['name']} "
          f"({round(asset['size'] / 1048576)} MB)")
    _fetch(asset["browser_download_url"], archive)

    subprocess.check_call([str(sevenzr), "x", str(archive),
                           f"-o{BIN}", "-y"],
                          stdout=subprocess.DEVNULL)
    archive.unlink(missing_ok=True)
    print("   mpv.exe installed" if (BIN / "mpv.exe").exists()
          else "   extraction failed")


def make_shortcut():
    step("Creating desktop shortcut")
    if sys.platform != "win32":
        print("   Not Windows - run 'python app.py' instead")
        return
    ps = f"""
$d = [Environment]::GetFolderPath('Desktop')
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut((Join-Path $d 'IPTV.lnk'))
$s.TargetPath       = '{ROOT / "IPTV.bat"}'
$s.WorkingDirectory = '{ROOT}'
$s.IconLocation     = '{ROOT / "static" / "iptv.ico"}'
$s.Description      = 'Open your IPTV library'
$s.WindowStyle      = 1
$s.Save()
Write-Output ('   created ' + (Join-Path $d 'IPTV.lnk'))
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


if __name__ == "__main__":
    install_deps()
    make_env()
    install_mpv()
    make_shortcut()
    print("\nSetup complete.")
    print("Make sure .env has your provider details, then double-click the")
    print("IPTV icon on your desktop (or run: python app.py)")
