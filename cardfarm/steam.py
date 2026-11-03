"""What Card Farm asks of Steam: badges (drops left), inventories, market prices, gem values, and
the two actions (list on the market, turn into gems)."""
import html
import json
import os
import re
import threading
import time
import urllib.parse

from .http import HttpError, get_json, post_json, request

COMMUNITY = "https://steamcommunity.com"
CARDS = (753, 6)  # the Steam inventory's Community context, where cards live


# --- badges -------------------------------------------------------------------------------
def parse_badges(page):
    """Game rows of one badges page: [{appid, name, drops (left), earned (in all)}]. Only the owner
    sees drops; the playtime box there is usually blank, so hours aren't read."""
    games = []
    for row in page.split('class="badge_row is_link"')[1:]:
        app = re.search(r"/gamecards/(\d+)/", row)
        if not app:
            continue
        title = re.search(r'class="badge_title">\s*(.*?)\s*(?:&nbsp;|<)', row, re.S)
        drops = re.search(r"(\d+) card drops? remaining", row)
        earned = re.search(r"Card drops earned:\s*(\d+)", row)
        left = int(drops[1]) if drops else 0
        games.append({"appid": int(app[1]), "name": html.unescape(title[1].strip()) if title else app[1],
                      "drops": left, "earned": int(earned[1]) if earned else left})
    return games


def page_count(page):
    return max([int(p) for p in re.findall(r"badges/\?p=(\d+)", page)] or [1])


def badges(session):
    base = f"{COMMUNITY}/profiles/{session.steamid}/badges/?l=english"
    status, first = request(base, cookies=session.cookies())
    if status != 200:
        raise HttpError(status)
    if "g_steamID = false" in first:
        raise RuntimeError("Steam says we're signed out; sign in again")
    games = parse_badges(first)
    for p in range(2, page_count(first) + 1):
        time.sleep(1)
        status, page = request(f"{base}&p={p}", cookies=session.cookies())
        if status == 200:
            games += parse_badges(page)
    seen, out = set(), []
    for g in games:
        if g["appid"] not in seen:
            seen.add(g["appid"]); out.append(g)
    return out


# --- inventories ----------------------------------------------------------------------------
def tags(desc):
    return {t["category"]: t for t in desc.get("tags", [])}


def _inventory(session, appid, context):
    """[(asset, description)] for one inventory, every page of it."""
    url = f"{COMMUNITY}/inventory/{session.steamid}/{appid}/{context}?l=english&count=2000"
    assets, descs, start = [], {}, None
    while True:
        d = get_json(url + (f"&start_assetid={start}" if start else ""), cookies=session.cookies())
        assets += d.get("assets", [])
        for x in d.get("descriptions", []):
            descs[(x["classid"], x["instanceid"])] = x
        if not d.get("more_items"):
            break
        start = d.get("last_assetid")
        time.sleep(2)
    return [(a, descs[(a["classid"], a["instanceid"])]) for a in assets if (a["classid"], a["instanceid"]) in descs]


def _is_card(desc):
    return tags(desc).get("item_class", {}).get("internal_name") == "item_class_2"


def cards_in_inventory(session):
    """Every trading card: [{assetid, appid, name, game, hash, foil, marketable, icon, goo}]"""
    out = []
    for a, x in _inventory(session, *CARDS):
        if not _is_card(x):
            continue
        t = tags(x)
        goo = None  # the inventory tells its owner the exact gem lookup for each item
        for act in x.get("owner_actions", []):
            m = re.search(r"GetGooValue\(\s*'[^']*',\s*'[^']*',\s*(\d+),\s*(\d+),\s*(\d+)", act.get("link", ""))
            if m:
                goo = (int(m[1]), int(m[2]), int(m[3]))
        out.append({"assetid": a["assetid"], "appid": int(x["market_fee_app"]), "name": x["name"],
                    "game": t.get("Game", {}).get("localized_tag_name", ""),
                    "hash": x["market_hash_name"], "marketable": bool(x.get("marketable")),
                    "foil": t.get("cardborder", {}).get("internal_name") == "cardborder_1",
                    "icon": x.get("icon_url", ""), "goo": goo})
    return out


def inventories(session):
    """Every inventory on the account that has something in it:
    [{"key": "730/2", "appid", "context", "name", "count"}], biggest first."""
    status, page = request(f"{COMMUNITY}/profiles/{session.steamid}/inventory/?l=english", cookies=session.cookies())
    m = re.search(r"g_rgAppContextData\s*=\s*(\{.*?\});\s*$", page, re.M)
    if status != 200 or not m:
        raise RuntimeError("Steam didn't show the list of your inventories")
    out = []
    for app in json.loads(m[1]).values():
        contexts = app.get("rgContexts") or {}
        contexts = list(contexts.values()) if isinstance(contexts, dict) else contexts
        for ctx in contexts:
            if ctx.get("asset_count"):
                name = app["name"] if len(contexts) == 1 else f"{app['name']} · {ctx['name']}"
                out.append({"key": f"{app['appid']}/{ctx['id']}", "appid": int(app["appid"]),
                            "context": int(ctx["id"]), "name": name, "count": ctx["asset_count"]})
    return sorted(out, key=lambda i: -i["count"])


def items_in_inventory(session, appid, context, name=""):
    """Everything in one inventory that can go on the market now, cards aside (Card Farm handles
    those). Stacks (e.g. gems) come as one entry with their amount."""
    out = []
    for a, x in _inventory(session, appid, context):
        if not x.get("marketable") or ((appid, context) == CARDS and _is_card(x)):
            continue
        out.append({"assetid": a["assetid"], "appid": appid, "context": context, "amount": int(a.get("amount") or 1),
                    "name": x.get("market_name") or x["name"], "hash": x["market_hash_name"],
                    "game": name, "icon": x.get("icon_url", ""),
                    "kind": tags(x).get("Type", {}).get("localized_tag_name", "")})
    return out


def pending_listings(session):
    """Asset ids listed on the market but still waiting for confirmation in the Steam app. They stay
    in the inventory until confirmed, and Steam refuses to list them a second time."""
    d = get_json(f"{COMMUNITY}/market/mylistings/render/?query=&start=0&count=10&norender=1", cookies=session.cookies())
    if not d.get("success"):
        raise RuntimeError("Steam didn't show your market listings")
    return {str(listing["asset"]["id"]) for listing in d.get("listings_to_confirm") or [] if listing.get("asset")}


# --- prices -------------------------------------------------------------------------------------
class Prices:
    """Lowest asking prices (by market hash name) and gem values, cached for a few hours. One fetch
    at a time, whichever part of Card Farm asks, to stay gentle with the market."""
    TTL = 6 * 3600

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        try:
            self.cache = json.load(open(path))
        except (OSError, ValueError):
            self.cache = {}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(self.cache), f)
        os.replace(tmp, self.path)

    def _fresh(self, key):
        hit = self.cache.get(key)
        return hit if hit and time.time() - hit["at"] < self.TTL else None

    def _remember(self, key, value):
        self.cache[key] = {"at": time.time(), "v": value}
        self._save()
        return value

    def game_cards(self, appid):
        """{hash name: lowest price in cents} for every card of a game (normal and foil)."""
        key = f"cards:{appid}"
        with self.lock:
            if self._fresh(key):
                return self.cache[key]["v"]
            prices, start, total = {}, 0, 1
            while start < total:  # pages are 10 long whatever count asks for
                q = urllib.parse.urlencode({"appid": 753, "norender": 1, "start": start, "count": 10,
                                            "category_753_Game[]": f"tag_app_{appid}",
                                            "category_753_item_class[]": "tag_item_class_2"})
                d = _patient(lambda: get_json(f"{COMMUNITY}/market/search/render/?{q}"))
                for it in d.get("results", []):
                    prices[it["hash_name"]] = it["sell_price"]
                total, start = d.get("total_count", 0), start + 10
                time.sleep(4)
            return self._remember(key, prices)

    def item(self, appid, hash_name):
        """Lowest asking price in cents for any market item, or None when nobody sells it."""
        key = f"item:{appid}:{hash_name}"
        with self.lock:
            if self._fresh(key):
                return self.cache[key]["v"]
            q = urllib.parse.urlencode({"appid": appid, "currency": 1, "market_hash_name": hash_name})
            d = _patient(lambda: get_json(f"{COMMUNITY}/market/priceoverview/?{q}"))
            time.sleep(3)
            return self._remember(key, _cents(d.get("lowest_price")))

    def gems(self, appid, item_type=None, foil=False):
        """Gems for one card. Without the inventory's exact item type, the cheapest item of the
        game's set is its card (backgrounds and emoticons are worth more)."""
        key = f"gems:{appid}:{item_type}:{int(foil)}"
        with self.lock:
            if self._fresh(key):
                return self.cache[key]["v"]
            types = [item_type] if item_type is not None else range(1, 25)
            vals = []
            for t in types:
                d = _patient(lambda: get_json(f"{COMMUNITY}/auction/ajaxgetgoovalueforitemtype/"
                                              f"?appid={appid}&item_type={t}&border_color={int(foil)}"))
                if int(d.get("goo_value") or 0):
                    vals.append(int(d["goo_value"]))
                time.sleep(0.25)
            return self._remember(key, min(vals) if vals else 0)

    def gem_sack(self):
        """Cents a 1,000-gem sack sells for."""
        with self.lock:
            if self._fresh("sack"):
                return self.cache["sack"]["v"]
            d = _patient(lambda: get_json(f"{COMMUNITY}/market/priceoverview/?appid=753&currency=1"
                                          "&market_hash_name=753-Sack%20of%20Gems"))
            return self._remember("sack", _cents(d["lowest_price"]))


def _cents(text):
    """'$1,234.56' -> 123456; None stays None."""
    return round(float(text.strip("$ ").replace(",", "")) * 100) if text else None


def _patient(call, tries=6):
    for i in range(tries):
        try:
            return call()
        except HttpError as e:
            if e.status != 429 or i == tries - 1:
                raise
            time.sleep(60)


# --- actions ---------------------------------------------------------------------------------------
ALREADY_LISTED = "pending confirmation"  # in Steam's refusal when a listing already waits for the app


def sell(session, assetid, receive_cents, appid=753, context=6, amount=1):
    """List an item (receive_cents each); Steam then wants a confirmation in the mobile app."""
    r = post_json(f"{COMMUNITY}/market/sellitem/",
                  {"sessionid": session.sessionid, "appid": appid, "contextid": context, "assetid": assetid,
                   "amount": amount, "price": receive_cents}, cookies=session.cookies(),
                  headers={"Referer": f"{COMMUNITY}/profiles/{session.steamid}/inventory/",
                           "Origin": COMMUNITY})
    if not r.get("success"):
        raise RuntimeError(r.get("message") or "Steam refused the listing")
    return r


def grind(session, appid, assetid, gems):
    r = post_json(f"{COMMUNITY}/profiles/{session.steamid}/ajaxgrindintogoo/",
                  {"sessionid": session.sessionid, "appid": appid, "contextid": 6, "assetid": assetid,
                   "goo_value_expected": gems}, cookies=session.cookies(),
                  headers={"Referer": f"{COMMUNITY}/profiles/{session.steamid}/inventory/",
                           "Origin": COMMUNITY})
    if r.get("success") != 1:
        raise RuntimeError(r.get("message") or f"Steam refused (code {r.get('success')})")
    return int(r.get("goo_value_received") or gems)
