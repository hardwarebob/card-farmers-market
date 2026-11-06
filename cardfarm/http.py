"""HTTP through curl: Steam's market answers curl but rate-limits Python's own HTTP client."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse

QUIET = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # no console window flashing up
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
_steam_lock = threading.Lock()
_steam_last = 0.0
_steam_resume = 0.0
_steam_backoff = 0
_STEAM_GAP = 0.5


def retry_after():
    """Seconds until the shared Steam Community cooldown expires."""
    return max(0.0, _steam_resume - time.monotonic())


def _steam_call(url, call, status_of):
    """Serialize Steam requests and make a 429 pause apply to every Steam caller."""
    global _steam_last, _steam_resume, _steam_backoff
    host = urllib.parse.urlsplit(url).hostname or ""
    if host != "steamcommunity.com" and not host.endswith((".steamcommunity.com", ".steampowered.com")):
        return call()
    with _steam_lock:
        wait = max(_steam_resume, _steam_last + _STEAM_GAP) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        result = call()
        status = status_of(result)
        _steam_last = time.monotonic()
        if status == 429:
            _steam_backoff = min(_steam_backoff * 2 or 5, 30)
            _steam_resume = _steam_last + _steam_backoff * 60
        elif 0 < status < 500:
            _steam_backoff = 0
            _steam_resume = 0
        return result


def _response_status(out):
    try:
        return int(out.rpartition("\n")[2])
    except ValueError:
        return 0


def _header_status(out):
    try:
        return int(out.replace("\r\n", "\n").partition("\n")[0].split()[1])
    except (IndexError, ValueError):
        return 0


class HttpError(Exception):
    def __init__(self, status, body="", url=""):
        where = f" from {urllib.parse.urlsplit(url).path}" if url else ""
        super().__init__("Steam says too many requests (HTTP 429); waiting before trying again" + where
                         if status == 429 else f"HTTP {status}{where}")
        self.status, self.body, self.url = status, body, url


def _quoted(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def request(url, data=None, cookies=None, headers=None, timeout=30):
    """Returns (status, body text). data (a dict) makes it a form POST.

    Cookies, headers and the form go to curl as a config on stdin, never on its command line,
    which any program on the PC can read."""
    config = [f"url = {_quoted(url)}"]
    if cookies:
        config.append(f"cookie = {_quoted('; '.join(f'{k}={v}' for k, v in cookies.items()))}")
    for k, v in (headers or {}).items():
        config.append(f"header = {_quoted(f'{k}: {v}')}")
    if data is not None:
        config.append(f"data-binary = {_quoted(urllib.parse.urlencode(data))}")
    # -c: keep cookies Steam sets on the way (the inventory page redirects through a cookie check)
    cmd = ["curl", "-s", "-L", "--compressed", "-c", os.devnull, "--max-time", str(timeout), "-A", UA, "-w", "\n%{http_code}", "-K", "-"]
    def run():
        return subprocess.run(cmd, input="\n".join(config).encode() + b"\n",
                              capture_output=True, timeout=timeout + 10, **QUIET).stdout.decode("utf-8", "replace")
    out = _steam_call(url, run, _response_status)
    body, _, status = out.rpartition("\n")
    return int(status or 0), body


def post_for_cookies(url, data, timeout=30):
    """POST a form and return (status, body, {name: value} of the cookies the reply sets)."""
    config = f"url = {_quoted(url)}\ndata-binary = {_quoted(urllib.parse.urlencode(data))}\n"
    cmd = ["curl", "-s", "-D", "-", "--compressed", "--max-time", str(timeout), "-A", UA, "-K", "-"]
    def run():
        return subprocess.run(cmd, input=config.encode(), capture_output=True, timeout=timeout + 10,
                              **QUIET).stdout.decode("utf-8", "replace")
    out = _steam_call(url, run, _header_status)
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
    cookies = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "set-cookie":
            k, _, v = value.strip().split(";", 1)[0].partition("=")
            cookies[k] = v
    return status, body, cookies


def download(url, path, timeout=600):
    """Save a file (following redirects); True when it arrived whole."""
    cmd = ["curl", "-s", "-L", "-f", "--max-time", str(timeout), "-A", UA, "-o", path, "-K", "-"]
    done = subprocess.run(cmd, input=f"url = {_quoted(url)}\n".encode(), capture_output=True,
                          timeout=timeout + 10, **QUIET)
    return done.returncode == 0 and os.path.exists(path)


def get_json(url, **kw):
    status, body = request(url, **kw)
    if status != 200:
        raise HttpError(status, body[:300], url)
    return json.loads(body)


def post_json(url, data, **kw):
    status, body = request(url, data=data, **kw)
    try:
        parsed = json.loads(body)
    except ValueError:
        raise HttpError(status, body[:300], url)
    if status != 200 and not isinstance(parsed, dict):
        raise HttpError(status, body[:300], url)
    return parsed
