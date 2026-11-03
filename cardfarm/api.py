"""What the window's page can ask of the farm. The page reaches it through Card Farm's own
cardfarm:// address inside the window; there's no web server and no network port."""
import json
import pkgutil

PAGES = {"/": ("index.html", "text/html; charset=utf-8"), "/icon.svg": ("icon.svg", "image/svg+xml")}


def answer(farm, method, path, body):
    """(status, content type, bytes) for one request from the page."""
    if method == "GET":
        if path in PAGES:
            name, kind = PAGES[path]
            return 200, kind, pkgutil.get_data("cardfarm", f"ui/{name}")  # also from inside a .pyz
        if path == "/api/state":
            return _json(200, farm.snapshot())
        return _json(404, {"error": "not found"})
    try:
        result = route(farm, path, json.loads(body or b"{}"))
    except Exception as e:
        return _json(400, {"error": str(e) or e.__class__.__name__})
    return _json(200, result if result is not None else {"ok": True})


def _json(status, data):
    return status, "application/json", json.dumps(data).encode()


def route(farm, path, body):
    if path == "/api/sign-in":
        farm.start_sign_in()
        return None
    if path == "/api/sign-out":
        return farm.sign_out()
    if path == "/api/settings":
        return farm.update_settings(body)
    if path in ("/api/review", "/api/items/review"):  # {"ids": [...] | "all", "action": ...}
        items = path == "/api/items/review"
        allowed = ("sell", "keep", "always") if items else ("sell", "gems", "keep", "suggested")
        if body.get("action") not in allowed:
            raise ValueError("unknown action")
        ids = body.get("ids")
        return farm.decide_review(ids if ids == "all" else set(map(str, ids or [])), body["action"], items=items)
    if path == "/api/review-existing":
        return farm.review_existing()
    if path == "/api/items/check":
        return farm.check_items()
    if path == "/api/skip":
        return farm.skip(int(body["appid"]))
    if path == "/api/unskip":
        return farm.unskip(int(body["appid"]))
    if path == "/api/refresh":
        return farm.refresh()
    raise ValueError("unknown action")
