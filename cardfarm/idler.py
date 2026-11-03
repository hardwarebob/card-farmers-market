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


def steam_pid():
    """The Steam client's process id (its oldest process), or None when Steam isn't running. A new
    id means Steam restarted, and games it knew about have to say hello again."""
    for cmd in (["pgrep", "-o", "-x", "steam"], ["pgrep", "-o", "-f", "steamrt64/steam"]):
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.split()
        if out:
            return int(out[0])
    return None


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

    def start(self, appids):
        """Start these games together; returns the ones Steam wouldn't take.

        Call it from the farm's own long-lived thread: each child's PR_SET_PDEATHSIG fires when the
        thread that started it ends, not just the whole program."""
        appids = [a for a in appids if a not in self.running()]
        if not appids:
            return set()
        self.library = self.library or find_library(self.data_dir)
        started = {}
        for appid in appids:
            env = dict(os.environ, SteamAppId=str(appid), SteamGameId=str(appid))
            started[appid] = subprocess.Popen([sys.executable, "-I", "-c", CHILD, self.library], env=env,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              start_new_session=True)
        time.sleep(3)  # SteamAPI_Init fails within a moment when Steam says no
        failed = set()
        for appid, p in started.items():
            if p.poll() is None:
                self.procs[appid] = p
            else:
                failed.add(appid)
        return failed

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
