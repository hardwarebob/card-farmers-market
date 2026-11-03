"""HTTP through curl: Steam's market answers curl but rate-limits Python's own HTTP client."""
import json
import subprocess
import urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


class HttpError(Exception):
    def __init__(self, status, body=""):
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body


def request(url, data=None, cookies=None, headers=None, timeout=30):
    """Returns (status, body text). data (a dict) makes it a form POST."""
    cmd = ["curl", "-s", "-L", "--max-time", str(timeout), "-A", UA, "-w", "\n%{http_code}"]
    if cookies:
        cmd += ["-b", "; ".join(f"{k}={v}" for k, v in cookies.items())]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    out = subprocess.run(cmd + [url], input=urllib.parse.urlencode(data or {}).encode(),
                         capture_output=True, timeout=timeout + 10).stdout.decode("utf-8", "replace")
    body, _, status = out.rpartition("\n")
    return int(status or 0), body


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
