"""Pretend to play games: one small process per game tells the running Steam client "I'm game X"
through Steam's own API library, the way a real game does. No Wine, no Proton. Works the same on
Linux, Windows and macOS; only where things live differs."""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time

WINDOWS, MAC = sys.platform == "win32", sys.platform == "darwin"
LIBRARY = "steam_api64.dll" if WINDOWS else "libsteam_api.dylib" if MAC else "libsteam_api.so"
QUIET = {"creationflags": 0x08000000} if WINDOWS else {"start_new_session": True}  # no console window / no Ctrl-C
LINUX_STEAM = ["~/.local/share/Steam", "~/.steam/steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"]


# --- one idler: the whole child process ---------------------------------------------------------
def child(library):
    """Body of one idler process (Card Farm runs itself with --idle-child LIB, SteamAppId set in
    its environment). It stays until Card Farm closes its stdin, on purpose or by going away."""
    appid = os.environ.get("SteamAppId", "unknown")
    try:
        steam = ctypes.CDLL(library)
    except OSError as e:
        print(f"AppId {appid}: couldn't load {library}: {e}", file=sys.stderr, flush=True)
        sys.exit(2)
    steam.SteamAPI_Init.restype = ctypes.c_bool  # a C++ bool: only the low byte means anything
    try:
        initialized = steam.SteamAPI_Init()
    except Exception as e:
        print(f"AppId {appid}: SteamAPI_Init raised {e!r}", file=sys.stderr, flush=True)
        sys.exit(3)
    if not initialized:
        print(f"AppId {appid}: SteamAPI_Init returned false", file=sys.stderr, flush=True)
        sys.exit(3)
    gone = threading.Event()
    stdin = sys.stdin.buffer if sys.stdin else open(0, "rb", closefd=False)
    threading.Thread(target=lambda: (stdin.read(), gone.set()), daemon=True).start()
    while not gone.wait(1):
        steam.SteamAPI_RunCallbacks()
    steam.SteamAPI_Shutdown()


def child_command(library):
    if getattr(sys, "frozen", False):  # the packaged app: it is its own idler
        return [sys.executable, "--idle-child", library]
    code_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # also works inside a .pyz
    return [sys.executable, "-c", f"import sys; sys.path.insert(0, {code_root!r}); "
            "from cardfarm.idler import child; child(sys.argv[1])", library]


# --- finding Steam --------------------------------------------------------------------------
def steam_root():
    if WINDOWS:
        path = _registry("SteamPath")
        return os.path.normpath(path) if path else r"C:\Program Files (x86)\Steam"
    if MAC:
        return os.path.expanduser("~/Library/Application Support/Steam")
    return next((p for p in map(os.path.expanduser, LINUX_STEAM) if os.path.isdir(p)), os.path.expanduser(LINUX_STEAM[0]))


def parse_library_folders(text):
    """The library folders listed in Steam's libraryfolders.vdf."""
    return [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', text)]


def libraries():
    """Every Steam library folder (the main one first), each holding a steamapps folder."""
    root = steam_root()
    found = [root]
    for vdf in (os.path.join(root, "steamapps", "libraryfolders.vdf"), os.path.join(root, "config", "libraryfolders.vdf")):
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                found += parse_library_folders(f.read())
        except OSError:
            pass
    seen, out = set(), []
    for p in found:
        key = os.path.normcase(os.path.realpath(p))
        if key not in seen and os.path.isdir(os.path.join(p, "steamapps")):
            seen.add(key)
            out.append(p)
    return out


def _candidates(common, depth=5):
    """Steam API libraries shipped with installed games, shallow ones first. Proton's comes first
    on Linux: every Proton has one, and it's the right kind."""
    if not (WINDOWS or MAC):
        for tool in sorted(os.listdir(common)) if os.path.isdir(common) else []:
            path = os.path.join(common, tool, "files", "lib64", LIBRARY)
            if os.path.isfile(path):
                yield path
    level = [common]
    for _ in range(depth):
        deeper = []
        for folder in level:
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for e in entries:
                if e.is_file() and e.name == LIBRARY:
                    yield e.path
                elif e.is_dir(follow_symlinks=False):
                    deeper.append(e.path)
        level = deeper


def _loads(path):
    """Whether this copy can be used by this computer (64-bit, right processor type)."""
    try:
        ctypes.CDLL(path)
        return True
    except OSError:
        return False


def find_library(data_dir):
    """Steam's API library, copied next to our data so it survives game uninstalls."""
    mine = os.path.join(data_dir, LIBRARY)
    if os.path.exists(mine) and _loads(mine):
        return mine
    for lib in libraries():
        for path in _candidates(os.path.join(lib, "steamapps", "common")):
            if _loads(path):
                shutil.copy(path, mine)
                return mine
    hint = ("Install any Proton version from your Steam library (Library > Tools > Proton)"
            if not (WINDOWS or MAC) else "Install any game that uses Steam features (most do)")
    raise RuntimeError(f"Couldn't find Steam's {LIBRARY} in your Steam library. {hint}, then restart Card Farmer's Market.")


# --- what Steam is doing ----------------------------------------------------------------------
def _registry(name, key=r"Software\Valve\Steam"):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _alive(pid):
    if WINDOWS:
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _pgrep(*args):
    out = subprocess.run(["pgrep", *args], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def steam_pid():
    """The Steam client's process id, or None when Steam isn't running. A new id means Steam
    restarted, and games it knew about have to say hello again."""
    if WINDOWS:
        pid = _registry("pid", r"Software\Valve\Steam\ActiveProcess")
        return pid if pid and _alive(pid) else None
    if MAC:
        return _pgrep("-o", "-x", "steam_osx")
    return _pgrep("-o", "-x", "steam") or _pgrep("-o", "-f", "steamrt64/steam")


def _steam_environment():
    """Use the logged-in desktop session when a background service lacks its GUI environment."""
    env = os.environ.copy()
    if not WINDOWS and not MAC and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        try:
            result = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                                    text=True, timeout=3, check=True)
            inherited = {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE",
                         "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
            for line in result.stdout.splitlines():
                key, sep, value = line.partition("=")
                if sep and key in inherited and value:
                    env[key] = value
        except (OSError, subprocess.SubprocessError):
            pass
    return env


def start_steam():
    """Launch the installed Steam client in the logged-in user's desktop session."""
    if steam_pid():
        return True
    env = _steam_environment()
    if not WINDOWS and not MAC and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no graphical desktop session is available to open Steam")
    if WINDOWS:
        path = _registry("SteamExe") or os.path.join(_registry("SteamPath") or r"C:\Program Files (x86)\Steam", "steam.exe")
        command = [path]
    elif MAC:
        command = ["open", "-a", "Steam"]
    else:
        executable = shutil.which("steam")
        if executable:
            command = [executable]
        elif shutil.which("flatpak") and subprocess.run(["flatpak", "info", "com.valvesoftware.Steam"],
                                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            command = [shutil.which("flatpak"), "run", "com.valvesoftware.Steam"]
        else:
            fallback = os.path.join(steam_root(), "steam.sh")
            if not os.path.isfile(fallback):
                raise FileNotFoundError("Steam isn't installed or its launcher isn't on PATH")
            command = [fallback]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=env, **QUIET)
    return False


def installed_games():
    """{install folder name: appid} from the app manifests of every library."""
    out = {}
    for lib in libraries():
        apps = os.path.join(lib, "steamapps")
        for name in os.listdir(apps):
            if name.startswith("appmanifest_") and name.endswith(".acf"):
                try:
                    with open(os.path.join(apps, name), encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                appid, folder = re.search(r'"appid"\s+"(\d+)"', text), re.search(r'"installdir"\s+"([^"]+)"', text)
                if appid and folder:
                    out[folder[1]] = int(appid[1])
    return out


def game_you_are_playing(ours=()):
    """AppId of a game you're playing yourself (not one of Card Farm's idlers), or None."""
    if WINDOWS:  # Steam keeps the running game in the registry; our idlers count as running too
        appid = _registry("RunningAppID")
        return appid if appid and appid not in ours else None
    if MAC:  # a process running from a game's folder in a Steam library
        out = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
        folders = set(re.findall(r"/steamapps/common/([^/]+)/", out))
        games = installed_games() if folders else {}
        return next((games[f] for f in folders if f in games and games[f] not in ours), None)
    # Linux: Steam starts every game as `reaper SteamLaunch AppId=N`
    out = subprocess.run(["pgrep", "-a", "-f", "reaper SteamLaunch AppId="], capture_output=True, text=True).stdout
    m = re.search(r"AppId=(\d+)", out)
    return int(m[1]) if m else None


# --- the idlers -------------------------------------------------------------------------------
class Idler:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.library = None
        self.procs = {}  # appid -> Popen
        self.logs = {}  # appid -> temporary stderr capture for its child

    def running(self):
        for appid, p in list(self.procs.items()):
            if p.poll() is not None:
                del self.procs[appid]
                log = self.logs.pop(appid, None)
                if log:
                    log.close()
        return set(self.procs)

    def start(self, appids):
        """Start these games together; returns the ones Steam wouldn't take."""
        appids = [a for a in appids if a not in self.running()]
        if not appids:
            return {}
        self.library = self.library or find_library(self.data_dir)
        started = {}
        for appid in appids:
            env = _steam_environment()
            env.update(SteamAppId=str(appid), SteamGameId=str(appid))
            log = tempfile.TemporaryFile(mode="w+b")
            p = subprocess.Popen(child_command(self.library), env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=log, **QUIET)
            started[appid] = (p, log)
        time.sleep(3)  # SteamAPI_Init fails within a moment when Steam says no
        failed = {}
        for appid, (p, log) in started.items():
            if p.poll() is None:
                self.procs[appid] = p
                self.logs[appid] = log
            else:
                log.seek(0)
                detail = log.read().decode(errors="replace").strip()
                log.close()
                failed[appid] = {"code": p.returncode, "detail": detail}
        return failed

    def stop(self, appid):
        log = self.logs.pop(appid, None)
        if log:
            log.close()
        p = self.procs.pop(appid, None)
        if p and p.poll() is None:
            try:
                p.stdin.close()  # the idler's cue to sign off from Steam and exit
                p.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()

    def stop_all(self):
        for appid in list(self.procs):
            self.stop(appid)
