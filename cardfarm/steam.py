"""What Card Farm asks of Steam: badges (drops left), inventory, market prices, gem values, and the
two actions (list on the market, turn into gems)."""
import html
import json
import re
import time
import urllib.parse

from .http import HttpError, get_json, post_json, request

COMMUNITY = "https://steamcommunity.com"


# --- badges -------------------------------------------------------------------------------
def parse_badges(page):
    """Game rows of one badges page: [{appid, name, drops, hours}]. Only the owner sees drops."""
    games = []
    for row in page.split('class="badge_row is_link"')[1:]:
        app = re.search(r"/gamecards/(\d+)/", row)
        if not app:
            continue
        title = re.search(r'class="badge_title">\s*(.*?)\s*(?:&nbsp;|<)', row, re.S)
        drops = re.search(r"(\d+) card drops? remaining", row)
        hours = re.search(r"([\d,.]+) hrs on record", row)
        games.append({"appid": int(app[1]), "name": html.unescape(title[1].strip()) if title else app[1],
                      "drops": int(drops[1]) if drops else 0,
                      "hours": float(hours[1].replace(",", "")) if hours else 0.0})
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


# --- inventory ------------------------------------------------------------------------------
def tags(desc):
    return {t["category"]: t for t in desc.get("tags", [])}


def cards_in_inventory(session):
    """Every marketable trading card: [{assetid, appid, name, game, hash, foil, item_type?}]"""
    url = f"{COMMUNITY}/inventory/{session.steamid}/753/6?l=english&count=2000"
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
    out = []
    for a in assets:
        x = descs.get((a["classid"], a["instanceid"]))
        if not x:
            continue
        t = tags(x)
        if t.get("item_class", {}).get("internal_name") != "item_class_2":
            continue
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


# --- prices -------------------------------------------------------------------------------------
class Prices:
    """Lowest asking price per card (by market hash name) and gem values, cached for a few hours."""
    TTL = 6 * 3600

    def __init__(self, path):
        self.path = path
        try:
            self.cache = json.load(open(path))
        except (OSError, ValueError):
            self.cache = {}

    def _save(self):
        json.dump(self.cache, open(self.path, "w"))

    def _fresh(self, key):
        hit = self.cache.get(key)
        return hit if hit and time.time() - hit["at"] < self.TTL else None

    def game_cards(self, appid):
        """{hash name: lowest price in cents} for every card of a game (normal and foil)."""
        key = f"cards:{appid}"
        if not self._fresh(key):
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
            self.cache[key] = {"at": time.time(), "v": prices}
            self._save()
        return self.cache[key]["v"]

    def gems(self, appid, item_type=None, foil=False):
        """Gems for one card. Without the inventory's exact item type, the cheapest item of the
        game's set is its card (backgrounds and emoticons are worth more)."""
        key = f"gems:{appid}:{item_type}:{int(foil)}"
        if not self._fresh(key):
            types = [item_type] if item_type is not None else range(1, 25)
            vals = []
            for t in types:
                d = _patient(lambda: get_json(f"{COMMUNITY}/auction/ajaxgetgoovalueforitemtype/"
                                              f"?appid={appid}&item_type={t}&border_color={int(foil)}"))
                if int(d.get("goo_value") or 0):
                    vals.append(int(d["goo_value"]))
                time.sleep(0.25)
            self.cache[key] = {"at": time.time(), "v": min(vals) if vals else 0}
            self._save()
        return self.cache[key]["v"]

    def gem_sack(self):
        """Cents a 1,000-gem sack sells for."""
        if not self._fresh("sack"):
            d = _patient(lambda: get_json(f"{COMMUNITY}/market/priceoverview/?appid=753&currency=1"
                                          "&market_hash_name=753-Sack%20of%20Gems"))
            self.cache["sack"] = {"at": time.time(), "v": round(float(d["lowest_price"].strip("$").replace(",", "")) * 100)}
            self._save()
        return self.cache["sack"]["v"]


def _patient(call, tries=6):
    for i in range(tries):
        try:
            return call()
        except HttpError as e:
            if e.status != 429 or i == tries - 1:
                raise
            time.sleep(60)


# --- actions ---------------------------------------------------------------------------------------
def sell(session, assetid, receive_cents):
    """List one card; Steam then wants a confirmation in the mobile app."""
    r = post_json(f"{COMMUNITY}/market/sellitem/",
                  {"sessionid": session.sessionid, "appid": 753, "contextid": 6, "assetid": assetid,
                   "amount": 1, "price": receive_cents}, cookies=session.cookies(),
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
