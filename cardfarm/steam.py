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
    GAP = 3  # seconds between market requests: Steam answers faster ones with 429 "too many requests"

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self._last = 0.0
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
        """One market request, at least GAP seconds after the previous one (callers hold the lock)."""
        time.sleep(max(0.0, self._last + self.GAP - time.time()))
        try:
            return _patient(lambda: get_json(url))
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
        key = f"item:{appid}:{hash_name}"
        with self.lock:
            if self._fresh(key):
                return self.cache[key]["v"]
            q = urllib.parse.urlencode({"appid": appid, "currency": 1, "market_hash_name": hash_name})
            d = self._ask(f"{COMMUNITY}/market/priceoverview/?{q}")
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
            d = self._ask(f"{COMMUNITY}/market/priceoverview/?appid=753&currency=1"
                          "&market_hash_name=753-Sack%20of%20Gems")
            return self._remember("sack", _cents(d["lowest_price"]))


def _cents(text):
    """'$1,234.56' -> 123456; None stays None."""
    return round(float(text.strip("$ ").replace(",", "")) * 100) if text else None


on_wait = None  # called (no arguments) before each minute spent waiting out a 429, to tell the page


def _patient(call, tries=6):
    """Wait out "too many requests" (a minute at a time) and Steam's passing server errors (a few
    seconds, three tries)."""
    hiccups = 0
    for i in range(tries):
        try:
            return call()
        except HttpError as e:
            if e.status in (500, 502, 503) and hiccups < 2:
                hiccups += 1
                time.sleep(5)
                continue
            if e.status != 429 or i == tries - 1:
                raise
            if on_wait:
                on_wait()
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
        raise RuntimeError(r.get("message") or "Steam didn't make the booster pack")
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


def open_buy_orders(session):
    """Ids of your buy orders still waiting on the market."""
    d = get_json(f"{COMMUNITY}/market/mylistings/render/?query=&start=0&count=10&norender=1", cookies=session.cookies())
    return {str(o.get("buy_orderid")) for o in d.get("buy_orders") or []}


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
