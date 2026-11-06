"""HTTP service hosting the farm and its browser interface."""
import os
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import api

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def serve(make_farm, host='127.0.0.1', port=8765):
    farm = make_farm()
    stopping = os.path.join(farm.data, "service-stopping")
    try:
        os.unlink(stopping)
    except FileNotFoundError:
        pass
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self): self.route()
        def do_POST(self): self.route()
        def route(self):
            length = min(int(self.headers.get('Content-Length', '0')), 2_000_000)
            body = self.rfile.read(length) if length else b''
            status, kind, data = api.answer(farm, self.command, self.path.split('?', 1)[0], body)
            self.send_response(status); self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data))); self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers(); self.wfile.write(data)
    server = Server((host, port), Handler)
    def stop_service():
        open(stopping, "w").close()
        threading.Thread(target=server.shutdown, daemon=True).start()
    farm.on_quit = stop_service
    print(f'Card Farm service listening on http://{host}:{server.server_port}', flush=True)
    threading.Thread(target=farm.run, daemon=True).start()
    try: server.serve_forever()
    finally:
        farm.idler.stop_all(); farm.save(); server.server_close()

def ensure_running(data_dir):
    """Start the local service when needed and return its base URL."""
    import os, subprocess, sys, time, urllib.error, urllib.request
    local = f"http://127.0.0.1:{int(os.environ.get('CARD_FARM_PORT', '8765'))}"
    base = os.environ.get('CARD_FARM_URL', local).rstrip('/')
    def ready():
        try:
            urllib.request.urlopen(base + '/api/state', timeout=.4).close()
            return True
        except Exception:
            return False
    if ready():
        _register_frontend(data_dir)
        return base
    if os.environ.get('CARD_FARM_URL'):
        raise RuntimeError(f'Card Farm service is not reachable at {base}')
    os.makedirs(data_dir, exist_ok=True)
    _register_frontend(data_dir)
    log = open(os.path.join(data_dir, 'service.log'), 'ab', buffering=0)
    command = [sys.executable, '--service'] if getattr(sys, "frozen", False) else [sys.executable, '-m', 'cardfarm', '--service']
    subprocess.Popen(command, stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True, close_fds=True)
    for _ in range(60):
        if ready(): return base
        time.sleep(.15)
    raise RuntimeError('Card Farm service did not start; see service.log')

def _register_frontend(data_dir):
    """Record this UI process so a service update can wait for every local frontend."""
    folder = os.path.join(data_dir, "frontends")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{os.getpid()}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"pid": os.getpid(), "identity": _process_identity(os.getpid())}, f)
    os.replace(tmp, path)


def clear_frontend(data_dir):
    """Remove our frontend marker on normal exit so later updates cannot wait on a stale PID."""
    identity = _process_identity(os.getpid())
    paths = [os.path.join(data_dir, "frontends", f"{os.getpid()}.json"),
             os.path.join(data_dir, "frontend.pid")]  # legacy single-frontend marker
    for path in paths:
        try:
            with open(path) as f:
                current = json.load(f)
            if current.get("pid") == os.getpid() and current.get("identity") == identity:
                os.unlink(path)
        except (OSError, ValueError, AttributeError):
            pass


def frontend_pids(data_dir):
    """Return live frontend PIDs, ignoring stale markers and entries for this service process."""
    folder = os.path.join(data_dir, "frontends")
    found = set()
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name)) as f:
                entry = json.load(f)
            pid, identity = int(entry["pid"]), entry["identity"]
            if pid != os.getpid() and identity and identity == _process_identity(pid):
                found.add(pid)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Read the exact-identity marker written by the previous release during upgrades.
    try:
        with open(os.path.join(data_dir, "frontend.pid")) as f:
            entry = json.load(f)
        pid, identity = int(entry["pid"]), entry["identity"]
        if pid != os.getpid() and identity and identity == _process_identity(pid):
            found.add(pid)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return sorted(found)


def _process_identity(pid):
    """An OS process-start token, used to distinguish an old PID from a later PID reuse."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                             ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        try:
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                            ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return f"{created.high:08x}{created.low:08x}"
        finally:
            kernel32.CloseHandle(handle)
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                                text=True, timeout=2, check=True)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None

def request(base, method, path, body=b'', timeout=30):
    import urllib.request
    req = urllib.request.Request(base + path, data=body if method == 'POST' else None,
                                 method=method, headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, r.headers.get_content_type(), r.read()
    except urllib.error.HTTPError as e: return e.code, e.headers.get_content_type(), e.read()

def service_is_farming(base):
    """Whether closing the desktop window should preserve the old hide-while-farming behavior."""
    import json
    try:
        status, _, data = request(base, "GET", "/api/state", timeout=.5)
        if status != 200:
            return False
        state = json.loads(data)
        return bool(state.get("signed_in") and
                    (state.get("settings", {}).get("farming") or state.get("working")))
    except Exception:
        return False
