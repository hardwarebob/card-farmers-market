"""The Market, Gems and Finish-sets parts of the farm: your listings against today's prices
(reprice, cancel), your sales, gems into sacks, booster packs worth more than their gems, and card
sets you hold most of (what the rest costs; craft once complete). Anything that changes the account only happens when the person
asks for it on the page (or switched on a rule for it), through the farm's saved queue."""
import time

from . import rules, steam

SALES_EVERY = 30 * 60
LISTINGS_EVERY = 6 * 3600   # only when "cancel unsold after" is on
BOOSTER_TTL = 24 * 3600


def market_state():
    return {"listings": {"items": [], "checked": 0},
            "sales": [], "sales_until": 0,
            "gems": {"gems": 0, "sacks": 0, "checked": 0},
            "boosters": {"options": [], "checked": 0, "done": 0, "of": 0},
            "sets": {"offers": [], "checked": 0},
            "idled": {}, "farmed": {},
            "finishing": {},  # appid -> {"game", "since"}: sets being finished; their cards are kept
            "levelup": {"options": [], "checked": 0}}


class MarketParts:
    """Mixed into Farm (uses its state, lock, session, prices, settings, log and queue)."""

    # --- time spent per drop ------------------------------------------------------------------
    def _count_idle_time(self):
        now = time.time()
        last, self._last_idle_count = getattr(self, "_last_idle_count", now), now
        step = min(now - last, 120)
        with self.lock:
            for appid in self.state["idling"]:
                self.state["idled"][appid] = self.state["idled"].get(appid, 0) + step

    def _farmed_one(self, appid):
        with self.lock:
            self.state["farmed"][str(appid)] = self.state["farmed"].get(str(appid), 0) + 1

    # --- sales ------------------------------------------------------------------------------------
    def _sync_sales(self):
        first = not self.state["sales_until"]
        new = steam.sales_since(self.session, self.state["sales_until"], most=500 if first else 1000)
        if not new:
            return
        with self.lock:
            self.state["sales"] = (new + self.state["sales"])[:5000]
            self.state["sales_until"] = max(s["at"] for s in new)
        if not first and self.settings["notify_sales"]:
            got = sum(s["received"] for s in new)
            names = ", ".join(s["name"] for s in new[:3]) + (" and more" if len(new) > 3 else "")
            self.notify(f"Sold {len(new)} for ${got / 100:.2f}", names)

    def sales_summary(self):
        """Totals for the page: today, 7 and 30 days, by day (14 days) and by game (30 days)."""
        now = time.time()
        today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        days, games = {}, {}
        names = {i["appid"]: i["name"].split(" · ")[0] for i in self.state["items"]["inventories"]}
        names.update({g["appid"]: g["name"] for g in self.state["games"]})
        totals = {"today": [0, 0], "week": [0, 0], "month": [0, 0], "all": [0, 0]}
        for s in self.state["sales"]:
            for key, since in (("today", today), ("week", now - 7 * 86400), ("month", now - 30 * 86400), ("all", 0)):
                if s["at"] >= since:
                    totals[key][0] += 1
                    totals[key][1] += s["received"]
            if s["at"] >= today - 13 * 86400:
                day = time.strftime("%Y-%m-%d", time.localtime(s["at"]))
                d = days.setdefault(day, [0, 0])
                d[0] += 1
                d[1] += s["received"]
            if s["at"] >= now - 30 * 86400:
                game = names.get(s["appid"]) or s["game"].removesuffix(" Trading Card").removesuffix(" Foil") or "?"
                g = games.setdefault(game, [0, 0])
                g[0] += 1
                g[1] += s["received"]
        return {"totals": totals, "days": sorted(days.items(), reverse=True),
                "games": sorted(games.items(), key=lambda kv: -kv[1][1])[:12],
                "recent": self.state["sales"][:20], "tracked_since": min((s["at"] for s in self.state["sales"]), default=0)}

    # --- your listings ------------------------------------------------------------------------------
    def check_listings(self):
        self._in_background("listings", self._check_listings)

    def _stats(self, appid, hash_name):
        """{"lowest", "median", "volume"} for something listed (cached a few hours)."""
        return self.prices.item_stats(appid, hash_name)

    def _check_listings(self):
        try:
            mine = steam.my_listings(self.session)
            now, s = time.time(), self.settings
            # only listings that could be repriced get a price lookup: old enough, and above the lowest
            # price they could go to (Steam's 3c minimum, or your own minimum for that kind of item)
            out, check = [], []
            for l in sorted(mine, key=lambda l: l["created"]):
                if now - l["created"] < s["reprice_after_hours"] * 3600:
                    out.append(dict(l, lowest=None, verdict="young"))
                elif l["buyer"] <= self._floor(l) + s["reprice_gap_cents"]:
                    out.append(dict(l, lowest=None, verdict="floor"))
                else:
                    check.append(l)
            for i, l in enumerate(check):
                self.progress["listings"] = f"Pricing listings that could be repriced: {i + 1} of {len(check)}"
                try:
                    stats = self._stats(l["appid"], l["hash"])
                except Exception:
                    stats = {"lowest": None, "median": None, "volume": 0}
                out.append(dict(l, **stats, verdict=self._verdict(l, rules.realistic(stats), now)))
            with self.lock:
                self.state["listings"] = {"items": out, "checked": now}
            beaten = sum(1 for l in out if l["verdict"] == "undercut")
            self.log(f"Listings checked: {len(out)} on the market, {len(check)} priced, {beaten} undercut")
            if s["cancel_after_days"]:
                old = [l for l in out if now - l["created"] > s["cancel_after_days"] * 86400]
                for l in old:
                    self._enqueue("cancel", {"id": l["listingid"], "kind": "listing", "listing": l})
                if old:
                    self.log(f"Cancelling {len(old)} listings unsold for {s['cancel_after_days']} days")
        except Exception as e:
            self.log(f"Couldn't check your listings: {e}", "warn")
        finally:
            self.progress.pop("listings", None)

    def _floor(self, l):
        """The lowest a listing could be repriced to."""
        is_card = l["appid"] == 753 and l["game"].endswith("Trading Card")
        return max(3, self.settings["min_sell_cents" if is_card else "items_min_cents"])

    def _verdict(self, l, going, now):
        """going: what it really sells for (lower of the cheapest listing and the day's median sale).
        undercut: that's more than the allowed gap below yours, and yours has waited long enough;
        young: the same but recent; ok: yours is in line with what sells."""
        if not going or going >= l["buyer"] - self.settings["reprice_gap_cents"]:
            return "ok"
        return "undercut" if now - l["created"] >= self.settings["reprice_after_hours"] * 3600 else "young"

    def decide_listings(self, ids, action):
        """reprice / cancel these listing ids ("undercut" for every undercut one)."""
        with self.lock:
            items = self.state["listings"]["items"]
            if ids == "undercut":
                picked = [l for l in items if l.get("verdict") == "undercut"]
            else:
                picked = [l for l in items if l["listingid"] in ids]
            self.state["listings"]["items"] = [l for l in items if l not in picked]
            for l in picked:
                self._enqueue(action, {"id": l["listingid"], "kind": "listing", "listing": l})
        self.save()

    def _returned(self, listing, before):
        """The item a cancelled listing put back in the inventory."""
        for _ in range(5):
            time.sleep(3)
            stock = steam._inventory(self.session, listing["appid"], listing["context"])
            back = [a for a, x in stock if a["assetid"] not in before and str(a["classid"]) == listing["classid"]
                    and str(a["instanceid"]) == listing["instanceid"]]
            if back:
                return back[0]["assetid"]
        raise RuntimeError("the cancelled item didn't come back to the inventory yet")

    def _do_listing(self, action, e):
        l = e["listing"]
        before = {a["assetid"] for a, _ in steam._inventory(self.session, l["appid"], l["context"])}
        steam.remove_listing(self.session, l["listingid"])
        assetid = self._returned(l, before)
        with self.lock:  # Card Farm put it back itself: not a new card
            self.state["seen"] = sorted(set(self.state["seen"]) | {assetid})
        if action == "cancel":
            self.log(f"Cancelled the listing for {l['name']}; it's back in your inventory")
            if (l["appid"], l["context"]) == steam.CARDS and l["game"].endswith("Trading Card"):
                card = next((c for c in steam.cards_in_inventory(self.session) if c["assetid"] == assetid), None)
                if card:
                    self.handle(card, "existing")  # back under your rules, in the review list
            return 0
        going = rules.realistic(self._stats(l["appid"], l["hash"]))
        buyer = max(rules.asking_price(going, self.settings) or l["buyer"], self._floor(l))
        receive = rules.received(buyer)
        steam.sell(self.session, assetid, receive, l["appid"], l["context"])
        self.log(f"Relisted {l['name']} for ${rules.buyer_pays(receive) / 100:.2f} (was ${l['buyer'] / 100:.2f})", "sell")
        return 1

    # --- gems ---------------------------------------------------------------------------------------
    def check_gems(self):
        self._in_background("gems", self._check_gems)

    def _check_gems(self):
        try:
            stock = steam.gem_stock(self.session)
            with self.lock:
                self.state["gems"] = dict(stock, checked=time.time(), sack_price=self.prices.gem_sack())
        except Exception as e:
            self.log(f"Couldn't read your gems: {e}", "warn")

    def pack_gems(self):
        g = self.state["gems"]
        if g["gems"] < 1000:
            raise ValueError("you need at least 1,000 loose gems for a sack")
        self._enqueue("pack", {"id": "gems", "kind": "gems", "sacks": g["gems"] // 1000, "assetid": g["gems_id"]})

    def sell_sacks(self):
        g = self.state["gems"]
        if not g.get("sack_ids"):
            raise ValueError("no sacks of gems to sell")
        for assetid in g["sack_ids"]:
            item = {"assetid": assetid, "appid": 753, "context": 6, "amount": g["sacks"] if len(g["sack_ids"]) == 1 else 1,
                    "name": "Sack of Gems", "hash": "753-Sack of Gems", "game": "Steam", "icon": "", "kind": "Gems"}
            self._enqueue("sell", {"id": assetid, "kind": "item", "item": item, "detail": {}})

    def _do_pack(self, e):
        steam.pack_gems(self.session, e["assetid"], e["sacks"])
        self.log(f"Packed {e['sacks'] * 1000:,} gems into {e['sacks']} sack{'s' * (e['sacks'] > 1)}", "gems")
        self._check_gems()
        return 0

    # --- boosters -----------------------------------------------------------------------------------
    def check_boosters(self):
        self._in_background("boosters", self._check_boosters)

    def _gem_value(self, gems):
        """What gems fetch once sold as sacks (after Steam's fees)."""
        return gems * self.prices.gem_sack() / 1000 / 1.15

    def _check_boosters(self):
        """Price every booster you can make (one a few seconds, so the first full pass takes a
        while); profitable ones show up on the page as they're found."""
        try:
            options, tradable = steam.booster_options(self.session)
            now = time.time()
            with self.lock:
                b = self.state["boosters"]
                known = {o["appid"]: o for o in b["options"] if now - o.get("priced", 0) < BOOSTER_TTL}
                b.update(tradable=tradable, of=len(options), done=0, checked=now)
            for i, o in enumerate(options):
                self.progress["boosters"] = f"Booster prices: {i + 1} of {len(options)}"
                if o["appid"] not in known or "volume" not in known[o["appid"]]:
                    try:
                        stats = self.prices.item_stats(753, f"{o['appid']}-{o['name']} Booster Pack", lowest=False)
                    except steam.HttpError as e:
                        if e.status == 429:  # keep what's priced; the rest next time
                            self.log(f"Booster prices paused at {i} of {len(options)}: Steam asked to slow down; "
                                     "check again later to go on", "warn")
                            break
                        continue
                    except Exception:
                        continue
                    known[o["appid"]] = dict(o, **stats, priced=time.time())
                else:
                    known[o["appid"]].update(unavailable=o["unavailable"], available_at=o["available_at"], gems=o["gems"])
                with self.lock:
                    b["options"] = sorted((self._booster_offer(x) for x in known.values()),
                                          key=lambda x: (not x["worth"], -(x["profit"] if x["profit"] is not None else -999)))
                    b["done"] = i + 1
        except Exception as e:
            self.log(f"Couldn't check booster prices: {e}", "warn")
        finally:
            self.progress.pop("boosters", None)

    def _booster_offer(self, o):
        """Valued on what the pack really sells for: the lower of its cheapest listing and its median
        sale over the last day (a stale, inflated ask doesn't count), and only if it sells at all."""
        lowest, median, volume = o.get("lowest"), o.get("median"), o.get("volume", 0)
        price = min(p for p in (lowest, median) if p) if (lowest or median) else None
        buyer = rules.asking_price(price, self.settings)
        receive = rules.received(buyer) if buyer else 0
        cost = round(self._gem_value(o["gems"]), 1)
        sells = volume >= self.settings["booster_min_sales"]
        profit = round(receive - cost, 1) if buyer else None
        return dict(o, buyer=buyer, receive=receive, cost=cost, sells=sells, profit=profit,
                    worth=bool(buyer and sells and profit > 0))

    def make_boosters(self, appids):
        with self.lock:
            options = self.state["boosters"]["options"]
            picked = [o for o in options if (appids == "profitable" and o.get("worth") and not o["unavailable"])
                      or o["appid"] in (appids if appids != "profitable" else ())]
        for o in picked:
            o["unavailable"] = True
            self._enqueue("booster", {"id": f"booster-{o['appid']}", "kind": "booster", "booster": o})
        self.save()

    def _do_booster(self, e):
        o = e["booster"]
        hash_name = f"{o['appid']}-{o['name']} Booster Pack"
        before = {a["assetid"] for a, _ in steam._inventory(self.session, *steam.CARDS)}
        steam.create_booster(self.session, o["appid"], o["series"])
        self.log(f"Made a booster pack for {o['name']} from {o['gems']} gems", "gems")
        for _ in range(5):
            time.sleep(3)
            new = [a for a, x in steam._inventory(self.session, *steam.CARDS)
                   if a["assetid"] not in before and x.get("market_hash_name") == hash_name]
            if new:
                break
        else:
            raise RuntimeError("the new booster pack didn't show up in the inventory yet; sell it from the Items tab")
        stats = self.prices.item_stats(753, hash_name)
        price = min(p for p in (stats["lowest"], stats["median"]) if p) if (stats["lowest"] or stats["median"]) else None
        buyer = max(rules.asking_price(price, self.settings) or 0, self.settings["items_min_cents"])
        receive = rules.received(buyer)
        steam.sell(self.session, new[0]["assetid"], receive, 753, 6)
        self.log(f"Listed the booster pack for {o['name']} at ${rules.buyer_pays(receive) / 100:.2f} (you get ${receive / 100:.2f})", "sell")
        return 1

    # --- finishing card sets ---------------------------------------------------------------------------
    def check_sets(self):
        self._in_background("sets", self._check_sets)

    def _check_sets(self):
        """Sets you hold part of: what the missing cards cost now, and what your cards of the set
        would sell for instead."""
        try:
            pending = steam.pending_listings(self.session)  # listed cards can't go into a badge
            cards = [c for c in steam.cards_in_inventory(self.session) if not c["foil"] and c["assetid"] not in pending]
            games = {}
            for c in cards:
                games.setdefault(c["appid"], {"game": c["game"], "owned": set()})["owned"].add(c["hash"])
            offers = []
            for i, (appid, g) in enumerate(games.items()):
                self.progress["sets"] = f"Card sets: {i + 1} of {len(games)}"
                prices = self.prices.game_cards(appid)
                full = {h: p for h, p in prices.items() if "(Foil" not in h}
                missing = {h: p for h, p in full.items() if h not in g["owned"]}
                if not full or len(missing) == len(full):  # nothing of it, or Steam lists no cards for it
                    continue
                cost = sum(p for p in missing.values())
                worth = sum(rules.received(p) for h, p in full.items() if h in g["owned"])
                offers.append({"appid": appid, "game": g["game"], "have": len(full) - len(missing), "of": len(full),
                               "missing": sorted(h.split("-", 1)[1] for h in missing), "missing_hashes": sorted(missing),
                               "cost": cost, "worth": worth, "unpriced": any(p is None for p in missing.values())})
            offers.sort(key=lambda o: (o["cost"], -o["have"]))
            with self.lock:
                self.state["sets"] = {"offers": offers, "checked": time.time()}
            ready = sum(1 for o in offers if not o["missing"])
            cheap = sum(1 for o in offers if o["missing"] and o["cost"] <= self.settings["set_budget_cents"])
            self.log(f"Card sets checked: {ready} ready to craft, {cheap} you could finish for under "
                     f"${self.settings['set_budget_cents'] / 100:.2f}")
        except Exception as e:
            self.log(f"Couldn't check your card sets: {e}", "warn")
        finally:
            self.progress.pop("sets", None)

    def finish_set(self, appid):
        """Buy what a set is missing (within the limit) and craft its badge."""
        with self.lock:
            offer = next((o for o in self.state["sets"]["offers"] if o["appid"] == appid), None)
            if not offer:
                raise ValueError("that set isn't on the list any more; check sets again")
            if offer["missing"] and offer["cost"] > self.settings["set_budget_cents"]:
                raise ValueError(f"the missing cards cost about ${offer['cost'] / 100:.2f}, over your limit")
            self.state["sets"]["offers"].remove(offer)
            # from now until the badge is crafted, this game's cards aren't sold or ground
            self.state["finishing"][str(appid)] = {"game": offer["game"], "since": time.time()}
        self._enqueue("set", {"id": f"set-{appid}", "kind": "set", "offer": offer})
        self.save()

    def finishing(self, appid):
        return str(appid) in self.state["finishing"]

    def stop_finishing(self, appid):
        """Release a set's cards to your selling rules again."""
        with self.lock:
            done = self.state["finishing"].pop(str(appid), None)
        if done:
            self.log(f"Stopped finishing the {done['game']} set; its cards follow your rules again")
        self.save()

    def _buy(self, hash_name, price):
        """Buy one at `price` through a buy order; True once Steam says it was bought. An order
        that doesn't fill within a few seconds (the listing sold) is cancelled again."""
        name = hash_name.split("-", 1)[1]
        order = steam.create_buy_order(self.session, hash_name, price)
        if "confirm" in order:
            # Steam wants each purchase OK'd in the mobile app; once it is, sending the order again
            # with the confirmation's id goes through
            self.notify("Confirm the purchase", f"Approve buying {name} in the Steam mobile app (Confirmations).")
            self.log(f"Waiting for you to confirm buying {name} in the Steam mobile app (up to 10 min)", "warn")
            self.progress["sets"] = f"Confirm buying {name} in the Steam app…"
            try:
                for _ in range(120):
                    time.sleep(5)
                    again = steam.create_buy_order(self.session, hash_name, price, confirmation=order["confirm"])
                    if "orderid" in again:
                        order = again
                        break
                else:
                    raise RuntimeError(f"buying {name} wasn't confirmed in the Steam app within 10 minutes")
            finally:
                self.progress.pop("sets", None)
        for _ in range(8):
            time.sleep(2)
            try:
                if steam.buy_order_status(self.session, order["orderid"]).get("purchased"):
                    return True
            except steam.HttpError:
                # Steam's status check turns away orders that filled at once; an order that's no
                # longer open has filled (nothing here cancels one before this point)
                if order["orderid"] not in steam.open_buy_orders(self.session):
                    return True
        steam.cancel_buy_order(self.session, order["orderid"])
        return False

    def _do_set(self, e):
        """Price every missing card (cheapest listing that isn't yours) and stop before buying
        anything if one can't be had or the total is over the limit; then buy them and craft."""
        o, spent = e["offer"], 0
        if steam.card_set(self.session, o["appid"])["level"] >= 5:
            raise RuntimeError(f"the {o['game']} badge is already at its top level")
        plan = []
        for hash_name in o["missing_hashes"]:
            price = steam.cheapest_listing(self.session, self.prices, hash_name)
            if not price:
                raise RuntimeError(f"nobody else is selling {hash_name.split('-', 1)[1]} right now; nothing bought")
            plan.append((hash_name, price))
        total, budget = sum(p for _, p in plan), o.get("limit") or self.settings["set_budget_cents"]
        if total > budget:
            raise RuntimeError(f"the {o['game']} cards cost ${total / 100:.2f} now, over your limit; nothing bought")
        for n, (hash_name, price) in enumerate(plan):
            name = hash_name.split("-", 1)[1]
            if not self._buy(hash_name, price):  # that listing sold: once more at the next price
                price = steam.cheapest_listing(self.session, self.prices, hash_name)
                rest = sum(p for _, p in plan[n + 1:])
                if not price or spent + price + rest > budget or not self._buy(hash_name, price):
                    raise RuntimeError(f"couldn't buy {name} within your limit; stopped after ${spent / 100:.2f}")
            spent += price
            self.log(f"Bought {name} for ${price / 100:.2f}")
        for attempt in range(6):
            try:
                steam.craft_badge(self.session, o["appid"])
                break
            except (RuntimeError, steam.HttpError):
                if attempt == 5:
                    raise
                time.sleep(5)  # bought cards take a moment to reach the inventory
        with self.lock:
            more = any(j["entry"].get("kind") == "set" and j["entry"]["offer"]["appid"] == o["appid"] and j["entry"] is not e
                       for j in self.state["queue"])
            if not more:
                self.state["finishing"].pop(str(o["appid"]), None)
        self.log(f"Crafted the {o['game']} badge" + (f" (bought cards for ${spent / 100:.2f})" if spent else ""), "gems")
        return 0

    # --- level up ----------------------------------------------------------------------------------
    def check_levelup(self):
        self._in_background("levelup", self._check_levelup)

    def _check_levelup(self):
        """Every way to earn badge XP Card Farmer's Market can price: sets you hold part of, and the
        market's cheapest full sets (from its cheapest cards), each up to its badge's top level."""
        try:
            self.progress["levelup"] = "Reading your level and badges…"
            me = steam.player_badges(self.session)
            self._check_sets()  # sets you hold part of (logs it if Steam turned it away)
            options, skipped = [], []
            for o in self.state["sets"]["offers"]:
                if me["badges"].get(o["appid"], 0) < 5 and not o.get("unpriced"):
                    options.append({"appid": o["appid"], "game": o["game"], "levels": 1, "cost": o["cost"],
                                    "forgone": o["worth"], "source": "yours", "missing_hashes": o["missing_hashes"],
                                    "missing": o["missing"]})
            yours = {o["appid"] for o in options}
            self.progress["levelup"] = "Looking for the market's cheapest sets…"
            games, self._names = [], {}
            try:
                cheap = steam.cheapest_cards(self.prices, LEVELUP_PAGES)
            except steam.HttpError as e:
                if e.status != 429:
                    raise
                cheap = []
                skipped.append("the market's cheapest sets")
            for appid, _, _, game in cheap:
                self._names.setdefault(appid, game)
                if appid not in games and appid not in yours and me["badges"].get(appid, 0) < 5:
                    games.append(appid)
            for i, appid in enumerate(games[:LEVELUP_GAMES]):
                self.progress["levelup"] = f"Pricing cheap sets: {i + 1} of {min(len(games), LEVELUP_GAMES)}"
                try:
                    prices = self.prices.game_cards(appid)
                except steam.HttpError as e:
                    if e.status != 429:
                        raise
                    skipped.append(f"{min(len(games), LEVELUP_GAMES) - i} of the cheap sets")
                    break
                full = {h: p for h, p in prices.items() if "(Foil" not in h}
                if len(full) < 5 or any(not p for p in full.values()):  # every set has 5 to 15 cards
                    continue
                options.append({"appid": appid, "game": self._game_name(appid), "levels": 5 - me["badges"].get(appid, 0),
                                "cost": sum(full.values()), "forgone": 0, "source": "market",
                                "missing_hashes": sorted(full), "missing": sorted(h.split("-", 1)[1] for h in full)})
            options.sort(key=lambda o: o["cost"] + o["forgone"])
            with self.lock:
                self.state["levelup"] = {"level": me["level"], "xp": me["xp"], "xp_to_next": me["xp_to_next"],
                                         "maxed": sum(1 for v in me["badges"].values() if v >= 5),
                                         "options": options, "checked": time.time()}
            self.log(f"Level up: {len(options)} ways to earn badge XP found"
                     + (f"; Steam asked to slow down, so {' and '.join(skipped)} weren't priced: check again later" if skipped else ""))
        except Exception as e:
            self.log(f"Couldn't work out how to level up: {e}", "warn")
        finally:
            self.progress.pop("levelup", None)

    def _game_name(self, appid):
        return next((g["name"] for g in self.state["games"] if g["appid"] == appid), None) or self._names.get(appid) or f"App {appid}"

    def levelup_plan(self):
        """The cheapest crafts that reach the target level within the budget (each craft is a badge
        level: 100 XP)."""
        lu, s = self.state["levelup"], self.settings
        if not lu.get("checked"):
            return {"crafts": [], "xp": 0, "cost": 0, "reaches": None, "short": 0}
        target = s["levelup_target"] or lu["level"] + 1
        need = xp_between(lu["level"], lu["xp_to_next"], target)
        crafts = [dict(o, n=n) for o in lu["options"] for n in range(o["levels"])]
        crafts.sort(key=lambda c: c["cost"] + c["forgone"])
        plan, xp, cost = [], 0, 0
        for c in crafts:
            if xp >= need or cost + c["cost"] > s["levelup_budget_cents"]:
                break
            plan.append(c)
            xp += 100
            cost += c["cost"]
        return {"crafts": plan, "xp": xp, "cost": cost, "need": need, "target": target,
                "reaches": level_after(lu["level"], lu["xp_to_next"], xp), "short": max(0, need - xp)}

    def level_up(self):
        """Queue the plan: each craft buys its set's cards and crafts the badge."""
        plan = self.levelup_plan()
        if not plan["crafts"]:
            raise ValueError("nothing to do: find the options first, or raise the budget")
        with self.lock:
            for c in plan["crafts"]:
                self.state["finishing"][str(c["appid"])] = {"game": c["game"], "since": time.time()}
                offer = {"appid": c["appid"], "game": c["game"], "missing_hashes": c["missing_hashes"],
                         "missing": c["missing"], "limit": round(c["cost"] * 1.25) + 2}
                self._enqueue("set", {"id": f"set-{c['appid']}-{c['n']}", "kind": "set", "offer": offer})
            self.state["levelup"]["options"] = []  # spent; check again afterwards
        self.log(f"Levelling up: {len(plan['crafts'])} badge levels queued, about ${plan['cost'] / 100:.2f}")
        self.save()


LEVELUP_PAGES = 15   # the market's 150 cheapest cards
LEVELUP_GAMES = 25   # sets priced from them, at most


def xp_for_level(level):
    """XP from `level` to the next: 100 for levels 0-9, 200 for 10-19, and so on."""
    return 100 * (1 + level // 10)


def xp_between(level, xp_to_next, target):
    """XP still needed to reach `target` from `level` with `xp_to_next` to go."""
    if target <= level:
        return 0
    return xp_to_next + sum(xp_for_level(l) for l in range(level + 1, target))


def level_after(level, xp_to_next, gained):
    while gained >= xp_to_next:
        gained -= xp_to_next
        level += 1
        xp_to_next = xp_for_level(level)
    return level
