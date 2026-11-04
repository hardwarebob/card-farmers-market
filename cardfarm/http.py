"""HTTP through curl: Steam's market answers curl but rate-limits Python's own HTTP client."""
import json
import os
import subprocess
import sys
import urllib.parse

QUIET = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # no console window flashing up
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


class HttpError(Exception):
    def __init__(self, status, body=""):
        super().__init__("Steam says too many requests (HTTP 429); Card Farm waits and tries again"
                         if status == 429 else f"HTTP {status}")
        self.status, self.body = status, body


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
    out = subprocess.run(cmd, input="\n".join(config).encode() + b"\n",
                         capture_output=True, timeout=timeout + 10, **QUIET).stdout.decode("utf-8", "replace")
    body, _, status = out.rpartition("\n")
    return int(status or 0), body


def download(url, path, timeout=600):
    """Save a file (following redirects); True when it arrived whole."""
    cmd = ["curl", "-s", "-L", "-f", "--max-time", str(timeout), "-A", UA, "-o", path, "-K", "-"]
    done = subprocess.run(cmd, input=f"url = {_quoted(url)}\n".encode(), capture_output=True,
                          timeout=timeout + 10, **QUIET)
    return done.returncode == 0 and os.path.exists(path)


def get_json(url, **kw):
    status, body = request(url, **kw)
    if status != 200:
        raise HttpError(status, body[:300])
    return json.loads(body)


def post_json(url, data, **kw):
    status, body = request(url, data=data, **kw)
    try:
        parsed = json.loads(body)
    except ValueError:
        raise HttpError(status, body[:300])
    if status != 200 and not isinstance(parsed, dict):
        raise HttpError(status, body[:300])
    return parsed
