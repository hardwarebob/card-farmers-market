"""What Card Farm asks of Steam: badges (drops left), inventories, market prices, gem values, and
the two actions (list on the market, turn into gems)."""
import calendar
import html
import json
import os
import re
import threading
import time
import urllib.parse

from .http import HttpError, get_json, post_json, request, retry_after

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
    if status != 200:  # a 429 here makes the farm's loop back off
        raise HttpError(status, first[:300], base)
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


def playtime(session):
    """Minutes played per owned game ({appid: minutes}), from Steam's own record."""
    q = urllib.parse.urlencode({"access_token": session.access_token, "steamid": session.steamid,
                                "include_played_free_games": 1, "include_appinfo": 0})
    games = get_json(f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?{q}").get("response", {}).get("games", [])
    return {g["appid"]: g.get("playtime_forever", 0) for g in games}


def free_to_play(appid):
    """Whether the store sells the game as free to play (a game the store no longer lists: False)."""
    d = get_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic").get(str(appid)) or {}
    return bool(d.get("success") and (d.get("data") or {}).get("is_free"))


# --- inventories ----------------------------------------------------------------------------
def tags(desc):
    return {t["category"]: t for t in desc.get("tags", [])}


def _inventory(session, appid, context):
    """[(asset, description)] for one inventory, every page of it."""
    url = f"{COMMUNITY}/inventory/{session.steamid}/{appid}/{context}?l=english&count=2000"
    assets, descs, start = [], {}, None
    while True:
        d = _patient(lambda: get_json(url + (f"&start_assetid={start}" if start else ""), cookies=session.cookies()))
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
    if status == 429:
        raise HttpError(429)
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
    d = _patient(lambda: get_json(f"{COMMUNITY}/market/mylistings/render/?query=&start=0&count=10&norender=1",
                                  cookies=session.cookies()))
    if not d.get("success"):
        raise RuntimeError("Steam didn't show your market listings")
    return {str(listing["asset"]["id"]) for listing in d.get("listings_to_confirm") or [] if listing.get("asset")}


# --- prices -------------------------------------------------------------------------------------
class Prices:
    """Lowest asking prices (by market hash name) and gem values, cached for a few hours. One fetch
    at a time, whichever part of Card Farm asks, to stay gentle with the market."""
    TTL = 6 * 3600
    GAP = 4  # seconds between market requests: Steam answers faster runs of them with 429 "too many requests"

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self._last = 0.0
        self._resume, self._backoff = 0.0, 0  # market pause after a 429: until when, and how long it was
        self.session = None  # the farm's sign-in, set by the farm
        try:
            self.cache = json.load(open(path))
        except (OSError, ValueError):
            self.cache = {}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(self.cache), f)
        os.replace(tmp, self.path)

    def _ask(self, url):
        """One market request, at least GAP seconds after the previous one (callers hold the lock).
        After Steam says "too many requests", every market request is turned away here for a while
        (5 minutes, then 10, up to 30) instead of each one asking again."""
        if time.time() < self._resume:
            raise HttpError(429, "", url)
        time.sleep(max(0.0, self._last + self.GAP - time.time()))
        signed = self.session and self.session.signed_in  # signed in, Steam lets the market be asked far more
        try:
            d = _patient(lambda: get_json(url, cookies=self.session.cookies()) if signed else get_json(url), tries=1)
            self._backoff = 0
            return d
        except HttpError as e:
            if e.status == 429:
                self._backoff = min(self._backoff * 2 or 5, 30)
                self._resume = time.time() + self._backoff * 60
                if on_wait:
                    on_wait(self._backoff, url)
            raise
        finally:
            self._last = time.time()

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
                d = self._ask(f"{COMMUNITY}/market/search/render/?{q}")
                for it in d.get("results", []):
                    prices[it["hash_name"]] = it["sell_price"]
                total, start = d.get("total_count", 0), start + 10
            return self._remember(key, prices)

    def item(self, appid, hash_name):
        """Lowest asking price in cents for any market item, or None when nobody sells it."""
        return self.item_stats(appid, hash_name)["lowest"]

    def item_stats(self, appid, hash_name, lowest=True):
        """{"lowest": cheapest asking price, "median": median sale price over the last 24 h, "volume":
        sales in the last 24 h} in cents. The sales come from the signed-in price history, the asking
        price from a search for the exact name (Steam's one-item price lookup is the first thing it
        turns away). lowest=False skips the search (its "lowest" is then None)."""
        key = f"stats:{appid}:{hash_name}:{int(lowest)}"
        with self.lock:
            if self._fresh(key):
                return self.cache[key]["v"]
            q = urllib.parse.urlencode({"appid": appid, "currency": 1, "market_hash_name": hash_name})
            stats = recent_sales(self._ask(f"{COMMUNITY}/market/pricehistory/?{q}").get("prices") or [])
            stats["lowest"] = None
            if lowest:
                q = urllib.parse.urlencode({"appid": appid, "norender": 1, "start": 0, "count": 10, "query": hash_name,
                                            "search_descriptions": 0})
                found = self._ask(f"{COMMUNITY}/market/search/render/?{q}").get("results") or []
                stats["lowest"] = next((r["sell_price"] for r in found if r.get("hash_name") == hash_name), None)
            return self._remember(key, stats)

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
                d = self._ask(f"{COMMUNITY}/auction/ajaxgetgoovalueforitemtype/"
                              f"?appid={appid}&item_type={t}&border_color={int(foil)}")
                if int(d.get("goo_value") or 0):
                    vals.append(int(d["goo_value"]))
            return self._remember(key, min(vals) if vals else 0)

    def gem_sack(self):
        """Cents a 1,000-gem sack sells for."""
        with self.lock:
            if self._fresh("sack"):
                return self.cache["sack"]["v"]
            stats = self.item_stats(753, "753-Sack of Gems")
            return self._remember("sack", stats["lowest"] or stats["median"])


def recent_sales(points, hours=24, now=None):
    """{"median", "volume"} of the last day from Steam's price history points
    [["Nov 04 2026 12: +0", 0.68, "277"], ...] (hourly lately; times in UTC)."""
    now = now or time.time()
    recent = []
    for when, price, count in points:
        stamp = calendar.timegm(time.strptime(when.split(":")[0], "%b %d %Y %H"))
        if now - stamp <= hours * 3600:
            recent.append((round(float(price) * 100), int(str(count).replace(",", ""))))
    volume = sum(n for _, n in recent)
    median, seen = None, 0
    for price, n in sorted(recent):  # the price at the middle sale
        seen += n
        if seen * 2 >= volume:
            median = price
            break
    return {"median": median if volume else None, "volume": volume}


def _cents(text):
    """'$1,234.56' -> 123456; None stays None."""
    return round(float(text.strip("$ ").replace(",", "")) * 100) if text else None


on_wait = None  # called with the retry delay in minutes and URL, so the page can report throttling


def _patient(call, tries=6):
    """Wait out "too many requests" (a minute at a time, `tries` in all) and Steam's passing server
    errors (a few seconds, three tries)."""
    hiccups = waits = 0
    while True:
        try:
            return call()
        except HttpError as e:
            if e.status in (500, 502, 503) and hiccups < 2:
                hiccups += 1
                time.sleep(5)
                continue
            if e.status != 429 or waits >= tries - 1:
                raise
            waits += 1
            delay = max(60.0, retry_after())
            if on_wait:
                on_wait(max(1, int((delay + 59) // 60)), e.url)
            time.sleep(delay)


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


# --- your market: listings and sales ----------------------------------------------------------
def my_listings(session):
    """Every active listing: [{listingid, assetid, appid, context, hash, name, game, icon, receive,
    buyer, created, classid, instanceid}]. Ones still waiting for the phone are left out."""
    out, start, total = [], 0, 1
    while start < total:
        d = _patient(lambda: get_json(f"{COMMUNITY}/market/mylistings/render/?query=&start={start}&count=100&norender=1",
                                      cookies=session.cookies()))
        if not d.get("success"):
            raise RuntimeError("Steam didn't show your market listings")
        for listing in d.get("listings") or []:
            a = listing.get("asset") or {}
            out.append({"listingid": str(listing["listingid"]), "assetid": str(a.get("id")), "appid": int(a.get("appid", 0)),
                        "context": int(a.get("contextid", 0)), "hash": a.get("market_hash_name", ""),
                        "name": a.get("market_name") or a.get("name", ""), "game": a.get("type", ""),
                        "icon": a.get("icon_url", ""), "classid": str(a.get("classid", "")),
                        "instanceid": str(a.get("instanceid", "")), "receive": int(listing.get("price") or 0),
                        "buyer": int(listing.get("price") or 0) + int(listing.get("fee") or 0),
                        "created": int(listing.get("time_created") or 0)})
        total, start = int(d.get("total_count") or 0), start + 100
        if start < total:
            time.sleep(2)
    return out


def remove_listing(session, listingid):
    """Cancel one listing; the item goes back to the inventory."""
    url = f"{COMMUNITY}/market/removelisting/{listingid}"
    status, body = request(url, data={"sessionid": session.sessionid}, cookies=session.cookies(),
                           headers={"Referer": f"{COMMUNITY}/market/", "Origin": COMMUNITY})
    if status != 200:
        raise HttpError(status, body[:300], url)


def parse_sales(d):
    """Sold items in one page of market history: [{at, listingid, name, hash, appid, received}]."""
    names = {(str(appid), str(ctx), aid): x for appid, ctxs in (d.get("assets") or {}).items()
             for ctx, items in ctxs.items() for aid, x in items.items()}
    out = []
    for e in d.get("events") or []:
        if e.get("event_type") != 3:  # 3: one of yours sold
            continue
        sale = (d.get("purchases") or {}).get(f"{e['listingid']}_{e['purchaseid']}") or {}
        a = ((d.get("listings") or {}).get(e["listingid"]) or {}).get("asset") or {}
        x = names.get((str(a.get("appid")), str(a.get("contextid")), str(a.get("id")))) or {}
        out.append({"at": int(e["time_event"]), "listingid": str(e["listingid"]), "name": x.get("market_name") or x.get("name", "?"),
                    "hash": x.get("market_hash_name", ""), "game": x.get("type", ""),
                    "appid": int(x.get("market_fee_app") or a.get("appid") or 0), "received": int(sale.get("received_amount") or 0)})
    return out


def sales_since(session, since, most=500):
    """Your sales newer than `since` (newest first), at most `most` events back."""
    out, start = [], 0
    while start < most:
        d = _patient(lambda: get_json(f"{COMMUNITY}/market/myhistory/render/?query=&start={start}&count=100&norender=1",
                                      cookies=session.cookies()))
        events = d.get("events") or []
        out += [s for s in parse_sales(d) if s["at"] > since]
        if not events or min(e["time_event"] for e in events) <= since or len(events) < 100:
            break
        start += 100
        time.sleep(2)
    return out


# --- gems and boosters ----------------------------------------------------------------------
def gem_stock(session):
    """{'gems': loose gems, 'gems_id': their asset id, 'sacks': sacks, 'sack_ids': [asset ids]}"""
    out = {"gems": 0, "gems_id": None, "sacks": 0, "sack_ids": []}
    for a, x in _inventory(session, *CARDS):
        if x.get("market_hash_name") == "753-Sack of Gems":
            out["sacks"] += int(a.get("amount") or 1)
            out["sack_ids"].append(a["assetid"])
        elif x.get("market_hash_name") == "753-Gems" or x.get("name") == "Gems":
            out["gems"] += int(a.get("amount") or 0)
            out["gems_id"] = out["gems_id"] or a["assetid"]
    return out


def pack_gems(session, gems_assetid, sacks, unpack=False):
    """Turn sacks x 1,000 loose gems into sacks of gems, or (unpack, with a sack's asset id) back."""
    size_in, size_out = (1000, 1) if unpack else (1, 1000)
    r = post_json(f"{COMMUNITY}/profiles/{session.steamid}/ajaxexchangegoo/",
                  {"sessionid": session.sessionid, "appid": 753, "assetid": gems_assetid, "goo_denomination_in": size_in,
                   "goo_amount_in": sacks * 1000 // size_in, "goo_denomination_out": size_out,
                   "goo_amount_out_expected": sacks * 1000 // size_out},
                  cookies=session.cookies(), headers={"Referer": f"{COMMUNITY}/profiles/{session.steamid}/inventory/",
                                                      "Origin": COMMUNITY})
    if r.get("success") != 1:
        raise RuntimeError(f"Steam didn't {'unpack' if unpack else 'pack'} the gems"
                           + (" (not enough of them)" if r.get("success") == 78 else ""))


def parse_booster_page(page):
    """(boosters you can make [{appid, name, series, gems, unavailable, available_at}], tradable gems)"""
    m = re.search(r"CBoosterCreatorPage\.Init\(\s*(\[.*?\])\s*,\s*parseFloat\(\s*\"([\d.]+)\"\s*\)\s*,\s*"
                  r"parseFloat\(\s*\"([\d.]+)\"\s*\)", page, re.S)
    if not m:
        raise RuntimeError("Steam didn't show the booster pack creator")
    boosters = [{"appid": int(b["appid"]), "name": b["name"], "series": int(b.get("series") or 1), "gems": int(b["price"]),
                 "unavailable": bool(b.get("unavailable")), "available_at": b.get("available_at_time") or ""}
                for b in json.loads(m[1])]
    return boosters, int(float(m[3]))


def booster_options(session):
    url = f"{COMMUNITY}/tradingcards/boostercreator/?l=english"
    status, page = request(url, cookies=session.cookies())
    if status != 200:
        raise HttpError(status, page[:300], url)
    return parse_booster_page(page)


def create_booster(session, appid, series=1):
    """Spend gems on a booster pack, from tradable gems only so the pack can be sold."""
    r = post_json(f"{COMMUNITY}/tradingcards/ajaxcreatebooster/",
                  {"sessionid": session.sessionid, "appid": appid, "series": series, "tradability_preference": 1},
                  cookies=session.cookies(), headers={"Referer": f"{COMMUNITY}/tradingcards/boostercreator/",
                                                      "Origin": COMMUNITY})
    if not r.get("purchase_result"):
        raise RuntimeError(r.get("message") or f"Steam didn't make the booster pack (its answer: {json.dumps(r)[:120]})")
    return r


def booster_prices(prices, stop_below):
    """{appid: lowest price} for booster packs costing at least stop_below, from the market's own list
    sorted by price (the cheap ones can't beat their gem cost anyway)."""
    found, start = {}, 0
    while start < 2000:
        q = urllib.parse.urlencode({"appid": 753, "norender": 1, "start": start, "count": 10, "sort_column": "price",
                                    "sort_dir": "desc", "category_753_item_class[]": "tag_item_class_5"})
        with prices.lock:
            d = prices._ask(f"{COMMUNITY}/market/search/render/?{q}")
        results = d.get("results") or []
        for it in results:
            m = re.match(r"(\d+)-", it["hash_name"])
            if m:
                found[int(m[1])] = it["sell_price"]
        if not results or results[-1]["sell_price"] < stop_below:
            break
        start += 10
    return found


# --- finishing badges -----------------------------------------------------------------------------
def parse_set_page(page):
    """{'level', 'cards': [{'name', 'owned'}]} from a game's card set page."""
    level = re.search(r"Level (\d+)[^<]*?- \d+ of \d+ cards collected", page)
    cards = []
    for chunk in page.split('class="badge_card_set_card ')[1:]:
        qty = re.search(r'badge_card_set_text_qty">\((\d+)\)', chunk)
        title = re.search(r'badge_card_set_title[^"]*">\s*(?:<span[^>]*>[^<]*</span>)?\s*([^<]+?)\s*<', chunk, re.S)
        owned = 0 if chunk.startswith("unowned") else int(qty[1]) if qty else 1
        cards.append({"name": html.unescape(title[1]) if title else "?", "owned": owned})
    return {"level": int(level[1]) if level else 0, "cards": cards}


def card_set(session, appid):
    url = f"{COMMUNITY}/profiles/{session.steamid}/gamecards/{appid}/?l=english"
    status, page = request(url, cookies=session.cookies())
    if status != 200:
        raise HttpError(status, page[:300], url)
    return parse_set_page(page)


def parse_listing_page(page):
    """[(listingid, buyer price)] for sale on a market item's page, cheapest first, leaving out
    your own (Steam's page carries its listings as JSON in a valve-ssr-data script)."""
    m = re.search(r'<script[^>]*id="valve-ssr-data"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        raise RuntimeError("Steam's market page didn't have its listings in it")
    queries = json.loads(json.loads(m[1])["renderContext"]["queryData"]).get("queries", [])
    out = []
    for q in queries:
        data = (q.get("state") or {}).get("data")
        if (q.get("queryKey") or [""])[0] != "market_item_search" or not isinstance(data, dict):
            continue
        for p in data.get("pages", []):
            out += [(str(l["listingid"]), int(l["unPrice"]) + int(l["unFee"])) for l in p.get("listings", [])
                    if not l.get("bMine") and l.get("eCurrency") == 1]
    return sorted(out, key=lambda x: x[1])


def cheapest_listing(session, prices, hash_name, appid=753):
    """What the cheapest listing someone else has up costs a buyer (cents), or None."""
    url = f"{COMMUNITY}/market/listings/{appid}/{urllib.parse.quote(hash_name)}"

    def fetch():
        time.sleep(max(0.0, prices._last + prices.GAP - time.time()))
        try:
            status, page = request(url, cookies=session.cookies())
        finally:
            prices._last = time.time()
        if status == 400:  # the market page turns some requests away for a moment
            raise HttpError(502, page[:300], url)
        if status != 200:
            raise HttpError(status, page[:300], url)
        return page
    with prices.lock:
        page = _patient(fetch)
    found = parse_listing_page(page)
    return found[0][1] if found else None


def create_buy_order(session, hash_name, price_each, quantity=1, appid=753, confirmation="0"):
    """Steam's market buys through buy orders: one at (or above) the cheapest listing's price fills
    straight away. Returns {"orderid"} or, when Steam wants it confirmed in the app, {"confirm"}."""
    r = post_json(f"{COMMUNITY}/market/createbuyorder/",
                  {"sessionid": session.sessionid, "currency": 1, "appid": appid, "market_hash_name": hash_name,
                   "price_total": price_each * quantity, "tradefee_tax": 0, "quantity": quantity, "first_name": "",
                   "last_name": "", "billing_address": "", "billing_address_two": "", "billing_country": "",
                   "billing_city": "", "billing_state": "", "billing_postal_code": "", "confirmation": confirmation,
                   "save_my_address": 0},
                  cookies=session.cookies(), headers={"Referer": f"{COMMUNITY}/market/listings/{appid}/{urllib.parse.quote(hash_name)}",
                                                      "Origin": COMMUNITY})
    if r.get("success") == 1 and r.get("buy_orderid"):
        return {"orderid": str(r["buy_orderid"])}
    if r.get("need_confirmation"):
        return {"confirm": str(r["confirmation"]["confirmation_id"])}
    raise RuntimeError(r.get("message") or "Steam didn't take the buy order")


def buy_order_status(session, orderid):
    return get_json(f"{COMMUNITY}/market/getbuyorderstatus/?sessionid={session.sessionid}&buy_orderid={orderid}",
                    cookies=session.cookies())


def buy_orders(session):
    """Your buy orders still waiting on the market: {orderid: quantity still wanted}."""
    d = _patient(lambda: get_json(f"{COMMUNITY}/market/mylistings/render/?query=&start=0&count=10&norender=1",
                                  cookies=session.cookies()))
    return {str(o.get("buy_orderid")): int(o.get("quantity_remaining") or 0) for o in d.get("buy_orders") or []}


def open_buy_orders(session):
    """Ids of your buy orders still waiting on the market."""
    return set(buy_orders(session))


def cancel_buy_order(session, orderid):
    r = post_json(f"{COMMUNITY}/market/cancelbuyorder/", {"sessionid": session.sessionid, "buy_orderid": orderid},
                  cookies=session.cookies(), headers={"Referer": f"{COMMUNITY}/market/", "Origin": COMMUNITY})
    if not r.get("success"):
        raise RuntimeError(r.get("error") or "Steam didn't cancel the buy order")


def craft_badge(session, appid, series=1):
    r = post_json(f"{COMMUNITY}/profiles/{session.steamid}/ajaxcraftbadge/",
                  {"appid": appid, "series": series, "border_color": 0, "levels": 1, "sessionid": session.sessionid},
                  cookies=session.cookies(), headers={"Referer": f"{COMMUNITY}/profiles/{session.steamid}/gamecards/{appid}/",
                                                     "Origin": COMMUNITY})
    if not r.get("success"):
        raise RuntimeError(r.get("message") or "Steam didn't craft the badge")
    return r


# --- your Steam level ---------------------------------------------------------------------------
def player_badges(session):
    """{"level", "xp_to_next", "badges": {appid: level of its normal (not foil) game badge}}"""
    session.cookies()  # renews the access token when it's due
    q = urllib.parse.urlencode({"access_token": session.access_token, "steamid": session.steamid})
    r = get_json(f"https://api.steampowered.com/IPlayerService/GetBadges/v1/?{q}").get("response", {})
    badges = {int(b["appid"]): int(b.get("level", 0)) for b in r.get("badges", [])
              if b.get("appid") and not b.get("border_color")}
    return {"level": int(r.get("player_level", 0)), "xp": int(r.get("player_xp", 0)),
            "xp_to_next": int(r.get("player_xp_needed_to_level_up", 0)), "badges": badges}


def cheapest_cards(prices, pages):
    """The market's cheapest trading cards (normal, not foil), cheapest first: [(appid, hash, cents, game)]."""
    out = []
    for page in range(pages):
        q = urllib.parse.urlencode({"appid": 753, "norender": 1, "start": page * 10, "count": 10, "sort_column": "price",
                                    "sort_dir": "asc", "category_753_item_class[]": "tag_item_class_2",
                                    "category_753_cardborder[]": "tag_cardborder_0"})
        with prices.lock:
            d = prices._ask(f"{COMMUNITY}/market/search/render/?{q}")
        for it in d.get("results") or []:
            m = re.match(r"(\d+)-", it["hash_name"])
            if m and "(Foil" not in it["hash_name"]:
                game = (it.get("asset_description") or {}).get("type", "").removesuffix(" Trading Card")
                out.append((int(m[1]), it["hash_name"], it["sell_price"], game))
        if not d.get("results"):
            break
    return out


# --- booster packs you have -----------------------------------------------------------------------
def boosters_held(session):
    """Booster packs in the inventory: [{assetid, appid (the game), name, game, hash, icon, marketable}]"""
    out = []
    for a, x in _inventory(session, *CARDS):
        if tags(x).get("item_class", {}).get("internal_name") == "item_class_5":
            out.append({"assetid": a["assetid"], "appid": int(x.get("market_fee_app") or 0), "name": x.get("name", ""),
                        "game": tags(x).get("Game", {}).get("localized_tag_name", ""), "hash": x.get("market_hash_name", ""),
                        "icon": x.get("icon_url", ""), "marketable": bool(x.get("marketable"))})
    return out


def unpack_booster(session, appid, assetid):
    """Open a booster pack; returns the cards' names (3, sometimes one of them foil)."""
    r = post_json(f"{COMMUNITY}/profiles/{session.steamid}/ajaxunpackbooster/",
                  {"appid": appid, "communityitemid": assetid, "sessionid": session.sessionid}, cookies=session.cookies(),
                  headers={"Referer": f"{COMMUNITY}/profiles/{session.steamid}/inventory/", "Origin": COMMUNITY})
    items = r.get("rgItems") or []
    if not items:
        raise RuntimeError(r.get("message") or f"Steam didn't open the pack (its answer: {json.dumps(r)[:120]})")
    return [i.get("name", "?") + (" (foil)" if i.get("foil") else "") for i in items]
