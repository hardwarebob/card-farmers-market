"""The control page, on this PC only (http://127.0.0.1:8797 by default).

Changes need an X-Card-Farm header, which other websites open in the same browser can't send
without a CORS preflight we never answer; the Host check stops DNS-rebinding tricks."""
import json
import pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .qr import svg

UI = pathlib.Path(__file__).parent / "ui" / "index.html"
PORT = 8797


def serve(farm, port=PORT):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return self.headers.get("Host") in hosts

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "wrong host"})
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/qr.svg":
                qr = farm.session.qr
                if not qr:
                    return self._send(404, {"error": "no sign-in in progress"})
                return self._send(200, svg(qr["url"]).encode(), "image/svg+xml")
            if path == "/api/state":
                return self._send(200, farm.snapshot())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Card-Farm") != "1":
                return self._send(403, {"error": "forbidden"})
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                result = route(farm, self.path, body)
            except Exception as e:
                return self._send(400, {"error": str(e)})
            self._send(200, result if result is not None else {"ok": True})

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def route(farm, path, body):
    if path == "/api/sign-in":
        return {"url": farm.start_sign_in()["url"]}
    if path == "/api/sign-out":
        return farm.sign_out()
    if path == "/api/settings":
        return farm.update_settings(body)
    if path == "/api/review":  # {"ids": [...] | "all", "action": sell|gems|keep|suggested}
        if body.get("action") not in ("sell", "gems", "keep", "suggested"):
            raise ValueError("unknown action")
        ids = body.get("ids")
        return farm.decide_review(ids if ids == "all" else set(map(str, ids or [])), body["action"])
    if path == "/api/review-existing":
        return farm.review_existing()
    if path == "/api/skip":
        return farm.skip(int(body["appid"]))
    if path == "/api/unskip":
        return farm.unskip(int(body["appid"]))
    if path == "/api/refresh":
        farm._next_badges = farm._next_inventory = 0
        return farm.poke()
    raise ValueError("unknown action")
