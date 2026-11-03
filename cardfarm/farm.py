"""The farm: keeps the right games idling, notices drops, and sells / gems / queues each new card.
On request it also sorts the other things in your inventories (skins, cases, ...) for selling."""
import json
import os
import subprocess
import threading
import time
import traceback

from . import qr, rules, steam
from .auth import Session
from .idler import Idler, game_you_are_playing, steam_pid

BADGES_EVERY = 10 * 60
INVENTORY_EVERY = 30 * 60
ITEMS_EVERY = 60 * 60
SKIP_FOR = 12 * 3600


def fresh_state():
    return {"seen": [], "review": [], "history": [], "games": [], "skipped": {}, "idling": {},
            "stats": {"drops": 0, "listed": 0, "listed_cents": 0, "gems": 0, "items_listed": 0, "items_cents": 0},
            "baseline": False,
            "queue": [],     # [{"action", "entry"}] still to do, in order; kept across restarts
            "kept": [],      # asset ids you chose to keep, so they aren't offered again
            "owed": {},      # appid -> card drops the badges page showed that the inventory hasn't yet
            "steamid": None,
            "items": {"inventories": [], "review": [], "seen": {}, "checked": 0}}


class Farm:
    def __init__(self, data_dir):
        self.data = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.session = Session(os.path.join(data_dir, "session.json"))
        self.settings = rules.load_settings(self._path("settings.json"))
        self.prices = steam.Prices(self._path("prices.json"))
        self.idler = Idler(data_dir)
        self.state = fresh_state()
        if os.path.exists(self._path("state.json")):
            with open(self._path("state.json")) as f:
                saved = json.load(f)
            self.state.update(saved)
            self.state["stats"] = dict(fresh_state()["stats"], **saved.get("stats", {}))
            self.state["items"] = dict(fresh_state()["items"], **saved.get("items", {}))
        self.state["idling"] = {}  # the idlers went away with the last run
        if self.session.signed_in:
            self._for_account()
        self.status = "starting"
        self.error = None
        self.signin_error = None
        self.busy = set()  # long jobs running for the page: "cards", "items"
        self.notify = _notify
        self._next_badges = self._next_inventory = 0
        self._next_items = time.time() + ITEMS_EVERY
        self._steam = None
        self._poller = None
        self._qr_svg = (None, None)
        self._wake = threading.Event()
        self._jobs = threading.Event()

    def _path(self, name):
        return os.path.join(self.data, name)

    def save(self):
        with self.lock:
            tmp = self._path("state.json.tmp")
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self._path("state.json"))

    def log(self, text, kind="info"):
        with self.lock:
            self.state["history"].insert(0, {"at": time.time(), "kind": kind, "text": text})
            del self.state["history"][300:]
        print(text, flush=True)

    # --- settings / sign-in ------------------------------------------------------------------
    def update_settings(self, patch):
        with self.lock:
            self.settings.update(rules.clean(patch))
            with open(self._path("settings.json"), "w") as f:
                json.dump(self.settings, f, indent=1)
        self.poke()

    def start_sign_in(self):
        self.signin_error = None
        qr_code = self.session.start_qr()
        with self.lock:  # one poller follows whichever code is showing
            if self._poller is None:
                self._poller = threading.Thread(target=self._wait_for_phone, daemon=True)
                self._poller.start()
        return qr_code

    def _wait_for_phone(self):
        while True:
            with self.lock:
                pending = self.session.qr
                if not pending:
                    self._poller = None
                    return
            time.sleep(pending["interval"])
            try:
                if self.session.poll_qr():
                    self.log(f"Signed in as {self.session.account}")
                    self._for_account()
                    self._next_badges = self._next_inventory = 0
                    self.poke()
                elif not self.session.qr:
                    self.signin_error = "That code expired. Show a new one to try again."
            except Exception as e:
                self.signin_error = f"Steam didn't answer the sign-in check ({e}); still trying."

    def _for_account(self):
        """What Card Farm remembers belongs to one Steam account: signing in as another starts afresh,
        so that account's cards aren't mistaken for new drops."""
        with self.lock:
            if self.state.get("steamid") not in (None, self.session.steamid):
                history = self.state["history"]
                self.state = fresh_state()
                self.state["history"] = history
            self.state["steamid"] = self.session.steamid

    def sign_out(self):
        self.idler.stop_all()
        self.session.sign_out()
        with self.lock:
            self.state["idling"] = {}
        self.log("Signed out")

    def poke(self):
        self._wake.set()

    def refresh(self):
        self._next_badges = self._next_inventory = 0
        self.poke()

    # --- main loop ---------------------------------------------------------------------------
    def run(self):
        threading.Thread(target=self._worker, daemon=True).start()
        while True:
            try:
                self.tick()
                self.error = None
            except Exception as e:
                self.error = str(e)
                traceback.print_exc()
            self.save()
            self._wake.wait(30)
            self._wake.clear()

    def tick(self):
        s = self.settings
        if not self.session.signed_in:
            return self._idle_none("Sign in to start")
        if s["items_new"] != "off" and time.time() >= self._next_items:
            self._next_items = time.time() + ITEMS_EVERY
            self._check_items(manual=False)
        if not s["farming"]:
            return self._idle_none("Stopped")
        pid = steam_pid()
        if not pid:
            return self._idle_none("Waiting for Steam to start")
        if self._steam and pid != self._steam and self.idler.running():
            self.log("Steam restarted; starting the games again")
            self._idle_none("Steam restarted")
        self._steam = pid
        playing = game_you_are_playing() if s["pause_when_playing"] else None
        if playing:
            return self._idle_none(f"Paused while you play ({self._name(playing)})")
        now = time.time()
        if now >= self._next_badges or now >= self._next_inventory:
            dropped = self._refresh_badges()
            self._next_badges = now + BADGES_EVERY
            if dropped or now >= self._next_inventory:  # the inventory is always read after fresh badges
                self._check_inventory()
                self._next_inventory = time.time() + INVENTORY_EVERY
        self._check_stalls()
        self._choose_games()
        self._price_one_game()

    def _idle_none(self, why):
        if self.idler.running():
            self.idler.stop_all()
        with self.lock:
            self.state["idling"] = {}
        self.status = why

    def _name(self, appid):
        return next((g["name"] for g in self.state["games"] if g["appid"] == appid), str(appid))

    def _refresh_badges(self):
        """Reads the badges page; returns True when cards dropped since the last read."""
        games = steam.badges(self.session)
        dropped = False
        with self.lock:
            old = {g["appid"]: g for g in self.state["games"]}
            owed = self.state["owed"]
            for g in games:
                before = old.get(g["appid"])
                if not before:
                    continue
                got = max(before["drops"] - g["drops"],
                          (g["earned"] - g["drops"]) - (before["earned"] - before["drops"]))
                if got > 0:
                    owed[str(g["appid"])] = owed.get(str(g["appid"]), 0) + got
                    dropped = True
                    info = self.state["idling"].get(str(g["appid"]))
                    if info:
                        info.update(last_drop=time.time(), clock=time.time(), restarts=0)
            self.state["games"] = games
        return dropped

    def _candidates(self, with_skipped=False):
        now, s = time.time(), self.settings
        with self.lock:
            skipped = {int(a) for a, until in self.state["skipped"].items() if until > now}
            games = [g for g in self.state["games"] if g["drops"] > 0 and (with_skipped or g["appid"] not in skipped)]
        if s["order"] == "most":
            games.sort(key=lambda g: -g["drops"])
        elif s["order"] == "fewest":
            games.sort(key=lambda g: g["drops"])
        else:
            games.sort(key=lambda g: -(self._game_value(g["appid"]) or 0))
        return games

    def _game_value(self, appid):
        hit = self.prices.cache.get(f"cards:{appid}")
        normal = [c for n, c in (hit or {}).get("v", {}).items() if "(Foil)" not in n]
        return sum(normal) / len(normal) if normal else None

    def _choose_games(self):
        """Idle the games that should be idling. Starting takes a few seconds, so it happens without
        holding the lock (the window keeps reading the state meanwhile)."""
        want = [g["appid"] for g in self._candidates()[: self.settings["games_at_once"]]]
        with self.lock:
            stop = [int(k) for k in self.state["idling"] if int(k) not in want]
            for appid in stop:
                self.state["idling"].pop(str(appid))
        for appid in stop:
            self.idler.stop(appid)
        failed = self.idler.start(want)
        now = time.time()
        with self.lock:
            idling = self.state["idling"]  # keyed by str(appid), like everything saved as JSON
            for appid in want:
                if appid in failed:
                    self.log(f"Steam wouldn't start {self._name(appid)}; skipping it for now", "warn")
                    self.state["skipped"][str(appid)] = now + SKIP_FOR
                    idling.pop(str(appid), None)
                else:
                    idling.setdefault(str(appid), {"since": now, "clock": now, "last_drop": None, "restarts": 0})
            names = [self._name(int(a)) for a in idling]
        left = sum(g["drops"] for g in self.state["games"])
        if names:
            self.status = f"Farming {', '.join(names)}" if len(names) <= 2 else f"Farming {len(names)} games"
        else:
            self.status = "All cards collected" if not left else "Waiting (games with drops are skipped for now)"

    def _check_stalls(self):
        limit = self.settings["stall_minutes"] * 60
        if not limit:
            return
        restart, give_up = [], []
        with self.lock:
            for appid, info in list(self.state["idling"].items()):
                if time.time() - info["clock"] < limit:
                    continue
                if info["restarts"] < 1:
                    restart.append(appid)
                    info.update(restarts=1, clock=time.time())
                else:
                    give_up.append(appid)
                    self.state["idling"].pop(appid)
                    self.state["skipped"][appid] = time.time() + SKIP_FOR
        for appid in restart:
            self.log(f"No drop from {self._name(int(appid))} in {limit // 60} min; restarting it", "warn")
            self.idler.stop(int(appid))  # _choose_games starts it again
        for appid in give_up:
            self.log(f"{self._name(int(appid))} still isn't dropping; trying other games for 12 h", "warn")
            self.idler.stop(int(appid))

    def _price_one_game(self):
        """Fill in card prices for one game per tick, so 'priciest first' learns the order gently."""
        if self.settings["order"] != "value":
            return
        for g in self._candidates():
            if self._game_value(g["appid"]) is None and f"cards:{g['appid']}" not in self.prices.cache:
                self.prices.game_cards(g["appid"])
                return

    # --- drops ---------------------------------------------------------------------------------
    def _check_inventory(self):
        cards = steam.cards_in_inventory(self.session)
        ids = {c["assetid"] for c in cards}
        with self.lock:
            seen = set(self.state["seen"])
            first_look = not self.state["baseline"]
            new = [c for c in cards if c["assetid"] not in seen]
            # cards that left (sold, ground, traded) drop out of the lists
            self.state["review"] = [e for e in self.state["review"] if e["id"] in ids]
            self.state["kept"] = [a for a in self.state["kept"] if a in ids]
            if first_look:
                self.state["seen"], self.state["baseline"] = sorted(ids), True
        if first_look:
            self.log(f"Found {len(cards)} cards already in your inventory; left alone "
                     "(use 'Sort cards I have' to sort them)")
            return
        if self.state["review"]:
            self._drop_pending_from_review()
        handled = set()
        for card in new:
            with self.lock:
                owed = self.state["owed"].get(str(card["appid"]), 0)
            try:
                self.handle(card, "drop" if owed else "new")
            except Exception as e:  # stays unseen, so the next check tries again
                self.log(f"Couldn't price {card['name']} ({card['game']}): {e}", "warn")
                continue
            handled.add(card["assetid"])
            with self.lock:
                if owed:
                    self.state["owed"][str(card["appid"])] = owed - 1
                    self.state["stats"]["drops"] += 1
        with self.lock:
            self.state["seen"] = sorted((seen & ids) | handled)

    def _drop_pending_from_review(self):
        """Cards listed elsewhere and waiting for the phone can't be listed again."""
        pending = steam.pending_listings(self.session)
        with self.lock:
            self.state["review"] = [e for e in self.state["review"] if e["id"] not in pending]
            items = self.state["items"]
            items["review"] = [e for e in items["review"] if e["id"] not in pending]
        return pending

    def evaluate(self, card):
        lowest = self.prices.game_cards(card["appid"]).get(card["hash"])
        item_type = card["goo"][1] if card.get("goo") else None
        gems = self.prices.gems(card["appid"], item_type, card["foil"])
        action, detail = rules.decide(card, lowest, gems, self.prices.gem_sack(), self.settings)
        detail.setdefault("gems", gems)
        detail.setdefault("buyer", lowest)
        return action, detail

    def handle(self, card, why):
        """why: 'drop' follows the settings; 'new' (a card that isn't from a drop: a trade, a booster
        pack, a purchase) and 'existing' go to review at most."""
        action, detail = self.evaluate(card)
        label = f"{card['name']} ({card['game']})"
        # one of the two switched off: the other is still better than leaving the card
        if action == "sell" and self.settings["sell"] == "off" and detail.get("gems"):
            action = "gems"
        elif action == "gems" and self.settings["gems"] == "off" and detail.get("buyer") \
                and detail["buyer"] >= self.settings["min_sell_cents"]:
            action = "sell"
        mode = self.settings.get(action, "off") if action in ("sell", "gems") else "off"
        if why != "drop" and mode != "off":
            mode = "review"
        if action == "keep" or mode == "off":
            if why != "existing":
                self.log(f"New card: {label}; kept ({detail.get('why') or f'{action} is off'})")
            return
        entry = {"id": card["assetid"], "kind": "card", "card": card, "action": action, "detail": detail,
                 "at": time.time()}
        if mode == "auto":
            self.log(f"New card: {label}")
            self._enqueue(action, entry)
            return
        with self.lock:
            if not any(e["id"] == entry["id"] for e in self.state["review"]):
                self.state["review"].append(entry)
        if why == "drop":
            self.log(f"New card: {label}; waiting for your review")
        elif why == "new":
            self.log(f"New card: {label}, not from a card drop (a trade, booster pack or purchase?); "
                     "waiting for your review")

    def review_existing(self):
        """Run every card already in the inventory through the rules, into the review list."""
        self._in_background("cards", self._review_existing)

    def _in_background(self, job, fn):
        with self.lock:
            if job in self.busy:
                return
            self.busy.add(job)

        def go():
            try:
                fn()
            finally:
                with self.lock:
                    self.busy.discard(job)
        threading.Thread(target=go, daemon=True).start()

    def _queued_ids(self):
        with self.lock:
            return ({e["id"] for e in self.state["review"]} | {e["id"] for e in self.state["items"]["review"]}
                    | {j["entry"]["id"] for j in self.state["queue"]} | set(self.state["kept"]))

    def _review_existing(self):
        try:
            pending = self._drop_pending_from_review()
            skip = self._queued_ids() | pending
            cards = [c for c in steam.cards_in_inventory(self.session) if c["assetid"] not in skip]
            self.log(f"Pricing {len(cards)} cards you already have…")
            for c in cards:
                self.handle(c, "existing")
            self.log(f"Review list ready: {len(self.state['review'])} cards")
        except Exception as e:
            self.log(f"Couldn't review your cards: {e}", "warn")

    def decide_review(self, ids, action, items=False):
        """The person's call on queued cards (sell / gems / keep / suggested) or items (sell / keep /
        always: keep this and every other one of its kind)."""
        with self.lock:
            box = self.state["items"] if items else self.state
            picked = [e for e in box["review"] if ids == "all" or e["id"] in ids]
            box["review"] = [e for e in box["review"] if e not in picked]
            for e in picked:
                act = e["action"] if action == "suggested" else action
                if act == "always":
                    names = set(self.settings["items_keep"]) | {e["item"]["hash"]}
                    self.settings["items_keep"] = sorted(names)
                    box["review"] = [x for x in box["review"] if x["item"]["hash"] not in names]
                if act in ("keep", "always"):
                    self.state["kept"].append(e["id"])
                elif act in ("sell", "gems"):
                    self._enqueue(act, e)
        if action == "always":
            self.update_settings({"items_keep": self.settings["items_keep"]})
        self.save()

    # --- other items ----------------------------------------------------------------------------
    def check_items(self):
        """The page's 'Check items': find the inventories and price what's in the chosen ones."""
        self._in_background("items", lambda: self._check_items(manual=True))

    def _check_items(self, manual):
        """manual: every sellable item in the chosen inventories goes to review. On the timer
        (items_new on), only items that weren't there last time count, and 'auto' lists them."""
        try:
            found = steam.inventories(self.session)
            chosen = [i for i in found if i["key"] in self.settings["items_inventories"]]
            with self.lock:
                self.state["items"]["inventories"] = found
            if not chosen:
                if manual:
                    self.log("Found your inventories; pick which ones Card Farm may sell from")
                return
            pending = self._drop_pending_from_review()
            skip = self._queued_ids() | pending
            offered = held = 0
            for inv in chosen:
                stock = steam.items_in_inventory(self.session, inv["appid"], inv["context"], inv["name"])
                ids = {i["assetid"] for i in stock}
                with self.lock:
                    seen = self.state["items"]["seen"]
                    before = set(seen[inv["key"]]) if inv["key"] in seen else None
                    seen[inv["key"]] = sorted(ids)
                    box = self.state["items"]
                    box["review"] = [e for e in box["review"]
                                     if e["item"]["context"] != inv["context"] or e["item"]["appid"] != inv["appid"]
                                     or e["id"] in ids]
                for item in stock:
                    new = before is not None and item["assetid"] not in before
                    if item["assetid"] in skip or not (manual or new):
                        continue
                    try:
                        action, detail = rules.decide_item(item, self.prices.item(item["appid"], item["hash"]),
                                                           self.settings)
                    except Exception as e:
                        self.log(f"Couldn't price {item['name']}: {e}", "warn")
                        continue
                    if action == "keep":
                        held += 1
                        continue
                    entry = {"id": item["assetid"], "kind": "item", "item": item, "action": "sell",
                             "detail": detail, "at": time.time()}
                    if new and not manual and self.settings["items_new"] == "auto":
                        self.log(f"New item: {item['name']} ({inv['name']})")
                        self._enqueue("sell", entry)
                    else:
                        with self.lock:
                            if not any(e["id"] == entry["id"] for e in box["review"]):
                                box["review"].append(entry)
                        offered += 1
            with self.lock:
                self.state["items"]["checked"] = time.time()
            if manual or offered:
                self.log(f"Items checked: {offered} to review"
                         + (f", {held} left alone (nobody selling, under your minimum or kept)" if held else ""))
        except Exception as e:
            self.log(f"Couldn't check your items: {e}", "warn")

    # --- doing it ---------------------------------------------------------------------------------
    def _enqueue(self, action, entry):
        with self.lock:
            self.state["queue"].append({"action": action, "entry": entry})
        self._jobs.set()

    def _worker(self):
        """Works through the queue one job at a time, paced for Steam. A job leaves the queue once
        done (or failed), so a restart picks up where it stopped."""
        listed = 0
        while True:
            with self.lock:
                job = self.state["queue"][0] if self.state["queue"] and self.session.signed_in else None
            if not job:
                if listed and self.settings["notify"]:
                    self.notify(f"{listed} listing{'s' * (listed > 1)} to confirm",
                                "Confirm them in the Steam mobile app (Confirmations).")
                listed = 0
                self._jobs.wait(20)
                self._jobs.clear()
                continue
            e = job["entry"]
            what = e["card"]["name"] if e["kind"] == "card" else e["item"]["name"]
            try:
                listed += self._do(job["action"], e)
            except Exception as err:
                if steam.ALREADY_LISTED in str(err):
                    self.log(f"{what} is already listed and waits for your OK in the Steam app")
                    listed += 1
                else:
                    self.log(f"Couldn't {job['action']} {what}: {err}", "warn")
            with self.lock:
                if job in self.state["queue"]:
                    self.state["queue"].remove(job)
            self.save()
            time.sleep(1.5)

    def _do(self, action, e):
        """Prices are looked up again here (from the few-hours cache), so a card that waited days
        for review isn't listed at an old price."""
        if e["kind"] == "item":
            it = e["item"]
            buyer = rules.asking_price(self.prices.item(it["appid"], it["hash"]), self.settings)
            if not buyer:
                raise RuntimeError("nobody is selling one now, so there's no price to match")
            receive = rules.received(buyer)
            steam.sell(self.session, it["assetid"], receive, it["appid"], it["context"], it["amount"])
            with self.lock:
                self.state["stats"]["items_listed"] += 1
                self.state["stats"]["items_cents"] += receive * it["amount"]
            count, each = (f" ×{it['amount']}", " each") if it["amount"] > 1 else ("", "")
            self.log(f"Listed {it['name']}{count} for ${rules.buyer_pays(receive) / 100:.2f}{each} "
                     f"(you get ${receive / 100:.2f}{each})", "sell")
            return 1
        card = e["card"]
        label = f"{card['name']} ({card['game']})"
        if action == "sell":
            buyer = rules.asking_price(self.prices.game_cards(card["appid"]).get(card["hash"]), self.settings)
            if not buyer:
                raise RuntimeError("nobody is selling it now, so there's no price to match")
            receive = rules.received(buyer)
            steam.sell(self.session, card["assetid"], receive)
            with self.lock:
                self.state["stats"]["listed"] += 1
                self.state["stats"]["listed_cents"] += receive
            self.log(f"Listed {label} for ${rules.buyer_pays(receive) / 100:.2f} (you get ${receive / 100:.2f})", "sell")
            return 1
        item_type = card["goo"][1] if card.get("goo") else None
        expected = self.prices.gems(card["appid"], item_type, card["foil"]) or e["detail"]["gems"]
        gems = steam.grind(self.session, card["appid"], card["assetid"], expected)
        with self.lock:
            self.state["stats"]["gems"] += gems
        self.log(f"Turned {label} into {gems} gems", "gems")
        return 0

    # --- for the page --------------------------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            st = self.state
            now = time.time()
            keep = set(self.settings["never_touch"])
            names = {g["appid"]: g["name"] for g in st["games"]}
            return {
                "signed_in": self.session.signed_in, "account": self.session.account,
                "qr_svg": self._qr(), "signin_error": self.signin_error,
                "status": self.status, "error": self.error, "working": len(st["queue"]),
                "busy": sorted(self.busy),
                "settings": self.settings,
                "idling": [{"appid": int(a), "name": self._name(int(a)), "since": i["since"],
                            "last_drop": i.get("last_drop")} for a, i in st["idling"].items()],
                "games": [dict(g, value=self._game_value(g["appid"]), keep=g["appid"] in keep,
                               skipped=st["skipped"].get(str(g["appid"]), 0) > now)
                          for g in self._candidates(with_skipped=True)] if self.session.signed_in else [],
                "kept_games": [{"appid": a, "name": names.get(a, str(a))} for a in sorted(keep)],
                "drops_left": sum(g["drops"] for g in st["games"]),
                "review": st["review"], "history": st["history"][:80], "stats": st["stats"],
                "items": {"inventories": [dict(i, on=i["key"] in self.settings["items_inventories"])
                                          for i in st["items"]["inventories"]],
                          "review": st["items"]["review"], "checked": st["items"]["checked"]},
                "ui": int(os.path.getmtime(os.path.join(os.path.dirname(__file__), "ui", "index.html"))),
            }

    def _qr(self):
        url = self.session.qr and self.session.qr["url"]
        if url and self._qr_svg[0] != url:
            self._qr_svg = (url, qr.svg(url))
        return self._qr_svg[1] if url else None

    def unskip(self, appid):
        with self.lock:
            self.state["skipped"].pop(str(appid), None)
        self.poke()

    def skip(self, appid):
        with self.lock:
            self.state["skipped"][str(appid)] = time.time() + SKIP_FOR
        self.poke()


def _notify(title, body):
    """Fallback when there's no window app to send notifications through."""
    try:
        subprocess.run(["notify-send", "-a", "Card Farm", title, body], timeout=10)
    except Exception:
        pass
