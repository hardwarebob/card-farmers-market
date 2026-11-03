"""Pretend to play games: one small process per game tells the running Steam client "I'm game X"
through Steam's own API library, the way a real game does. No Wine, no Proton."""
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import time

CHILD = r"""
import ctypes, signal, sys, time
libc = ctypes.CDLL("libc.so.6")
libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG: go away with Card Farm
steam = ctypes.CDLL(sys.argv[1])
if not steam.SteamAPI_Init():
    sys.exit(3)
signal.signal(signal.SIGTERM, lambda *a: (steam.SteamAPI_Shutdown(), sys.exit(0)))
while True:
    steam.SteamAPI_RunCallbacks()
    time.sleep(1)
"""

STEAM_DIRS = ["~/.local/share/Steam", "~/.steam/steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"]
SEARCH = [d + tail for d in STEAM_DIRS for tail in (
    "/steamapps/common/*/files/lib64/libsteam_api.so",          # Proton
    "/steamapps/common/SteamVR/bin/linux64/libsteam_api.so",
    "/steamapps/common/*/libsteam_api.so",                      # native Linux games
    "/steamapps/common/*/*/libsteam_api.so",
    "/steamapps/common/*/*/*/libsteam_api.so")]


def find_library(data_dir):
    """Steam's API library, copied next to our data so it survives game uninstalls."""
    mine = os.path.join(data_dir, "libsteam_api.so")
    if os.path.exists(mine):
        return mine
    for pattern in SEARCH:
        for path in glob.glob(os.path.expanduser(pattern)):
            if _is_64bit(path):
                shutil.copy(path, mine)
                return mine
    raise RuntimeError("Couldn't find Steam's libsteam_api.so. Install any Proton version from your "
                       "Steam library (Library > Tools > Proton), then restart Card Farm.")


def _is_64bit(path):
    with open(path, "rb") as f:
        head = f.read(5)
    return head[:4] == b"\x7fELF" and head[4] == 2


def steam_running():
    return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0 or \
        subprocess.run(["pgrep", "-f", "steamrt64/steam"], capture_output=True).returncode == 0


def game_you_are_playing():
    """AppId of a game Steam launched for you (Steam starts every game as `reaper SteamLaunch AppId=N`)."""
    out = subprocess.run(["pgrep", "-a", "-f", "reaper SteamLaunch AppId="], capture_output=True, text=True).stdout
    m = re.search(r"AppId=(\d+)", out)
    return int(m[1]) if m else None


class Idler:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.library = None
        self.procs = {}  # appid -> Popen

    def running(self):
        for appid, p in list(self.procs.items()):
            if p.poll() is not None:
                del self.procs[appid]
        return set(self.procs)

    def start(self, appid):
        if appid in self.running():
            return True
        self.library = self.library or find_library(self.data_dir)
        env = dict(os.environ, SteamAppId=str(appid), SteamGameId=str(appid))
        p = subprocess.Popen([sys.executable, "-I", "-c", CHILD, self.library], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(3)
        if p.poll() is not None:
            return False
        self.procs[appid] = p
        return True

    def stop(self, appid):
        p = self.procs.pop(appid, None)
        if p and p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    def stop_all(self):
        for appid in list(self.procs):
            self.stop(appid)
