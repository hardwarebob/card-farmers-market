"""The farm: keeps the right games idling, notices drops, and sells / gems / queues each new card."""
import json
import os
import queue
import subprocess
import threading
import time
import traceback

from . import rules, steam
from .auth import Session
from .idler import Idler, game_you_are_playing, steam_running

BADGES_EVERY = 10 * 60
INVENTORY_EVERY = 30 * 60
SKIP_FOR = 12 * 3600


class Farm:
    def __init__(self, data_dir):
        self.data = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.session = Session(os.path.join(data_dir, "session.json"))
        self.settings = rules.load_settings(self._path("settings.json"))
        self.prices = steam.Prices(self._path("prices.json"))
        self.idler = Idler(data_dir)
        self.state = {"seen": [], "review": [], "history": [], "games": [], "skipped": {}, "idling": {},
                      "stats": {"drops": 0, "listed": 0, "listed_cents": 0, "gems": 0}, "baseline": False}
        if os.path.exists(self._path("state.json")):
            self.state.update(json.load(open(self._path("state.json"))))
        self.status = "starting"
        self.error = None
        self.jobs = queue.Queue()  # (action, review entry) done one at a time, paced for Steam
        self.working = 0
        self._next_badges = self._next_inventory = 0
        self._wake = threading.Event()

    def _path(self, name):
        return os.path.join(self.data, name)

    def save(self):
        with self.lock:
            tmp = self._path("state.json.tmp")
            json.dump(self.state, open(tmp, "w"))
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
            json.dump(self.settings, open(self._path("settings.json"), "w"), indent=1)
        self.poke()

    def start_sign_in(self):
        qr = self.session.start_qr()
        threading.Thread(target=self._wait_for_phone, daemon=True).start()
        return qr

    def _wait_for_phone(self):
        while self.session.qr:
            time.sleep(self.session.qr["interval"])
            try:
                if self.session.poll_qr():
                    self.log(f"Signed in as {self.session.account}")
                    self._next_badges = self._next_inventory = 0
                    self.poke()
                    return
            except Exception as e:
                self.error = f"Sign-in: {e}"

    def sign_out(self):
        self.idler.stop_all()
        self.session.sign_out()
        with self.lock:
            self.state["idling"] = {}
        self.log("Signed out")

    def poke(self):
        self._wake.set()

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
        if not s["farming"]:
            return self._idle_none("Stopped")
        if not steam_running():
            return self._idle_none("Waiting for Steam to start")
        playing = game_you_are_playing() if s["pause_when_playing"] else None
        if playing:
            return self._idle_none(f"Paused while you play ({self._name(playing)})")
        now = time.time()
        if now >= self._next_badges:
            self._refresh_badges()
            self._next_badges = now + BADGES_EVERY
        if now >= self._next_inventory:
            self._check_inventory()
            self._next_inventory = now + INVENTORY_EVERY
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
        games = steam.badges(self.session)
        dropped = False
        with self.lock:
            old = {g["appid"]: g["drops"] for g in self.state["games"]}
            for g in games:
                if str(g["appid"]) in self.state["idling"] and g["drops"] < old.get(g["appid"], g["drops"]):
                    self.state["idling"][str(g["appid"])].update(last_drop=time.time(), restarts=0)
                    dropped = True
            self.state["games"] = games
        if dropped:  # fetch the new card now rather than in half an hour
            self._check_inventory()
            self._next_inventory = time.time() + INVENTORY_EVERY

    def _candidates(self):
        now, s = time.time(), self.settings
        skipped = {int(a) for a, until in self.state["skipped"].items() if until > now}
        games = [g for g in self.state["games"] if g["drops"] > 0 and g["appid"] not in skipped]
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
        want = [g["appid"] for g in self._candidates()[: self.settings["games_at_once"]]]
        with self.lock:
            idling = self.state["idling"]  # keyed by str(appid), like everything saved as JSON
            for key in list(idling):
                if int(key) not in want:
                    self.idler.stop(int(key))
                    idling.pop(key)
            for appid in want:
                if appid not in self.idler.running() and not self.idler.start(appid):
                    self.log(f"Steam wouldn't start {self._name(appid)}; skipping it for now", "warn")
                    self.state["skipped"][str(appid)] = time.time() + SKIP_FOR
                    idling.pop(str(appid), None)
                    continue
                idling.setdefault(str(appid), {"since": time.time(), "last_drop": time.time(), "restarts": 0})
        names = [self._name(int(a)) for a in self.state["idling"]]
        left = sum(g["drops"] for g in self.state["games"])
        self.status = f"Farming {', '.join(names)}" if names else (
            "All cards collected" if not left else "Waiting (games with drops are skipped for now)")

    def _check_stalls(self):
        limit = self.settings["stall_minutes"] * 60
        if not limit:
            return
        with self.lock:
            for appid, info in list(self.state["idling"].items()):
                if time.time() - info["last_drop"] < limit:
                    continue
                if info["restarts"] < 1:
                    self.log(f"No drop from {self._name(int(appid))} in {limit // 60} min; restarting it", "warn")
                    self.idler.stop(int(appid))
                    info.update(restarts=1, last_drop=time.time())
                else:
                    self.log(f"{self._name(int(appid))} still isn't dropping; trying other games for 12 h", "warn")
                    self.idler.stop(int(appid))
                    self.state["idling"].pop(appid)
                    self.state["skipped"][str(appid)] = time.time() + SKIP_FOR

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
        with self.lock:
            seen = set(self.state["seen"])
            first_look = not self.state["baseline"]
            new = [c for c in cards if c["assetid"] not in seen]
            self.state["seen"] = sorted(seen | {c["assetid"] for c in cards})
            self.state["baseline"] = True
        if first_look:
            self.log(f"Found {len(cards)} cards already in your inventory; left alone "
                     "(use 'Review cards I already have' to sort them)")
            return
        for card in new:
            self.state["stats"]["drops"] += 1
            self.handle(card, "drop")

    def evaluate(self, card):
        lowest = self.prices.game_cards(card["appid"]).get(card["hash"])
        item_type = card["goo"][1] if card.get("goo") else None
        gems = self.prices.gems(card["appid"], item_type, card["foil"])
        action, detail = rules.decide(card, lowest, gems, self.prices.gem_sack(), self.settings)
        detail.setdefault("gems", gems)
        detail.setdefault("buyer", lowest)
        return action, detail

    def handle(self, card, why):
        """why: 'drop' follows the settings; 'existing' always goes to review."""
        action, detail = self.evaluate(card)
        label = f"{card['name']} ({card['game']})"
        # one of the two switched off: the other is still better than leaving the card
        if action == "sell" and self.settings["sell"] == "off" and detail.get("gems"):
            action = "gems"
        elif action == "gems" and self.settings["gems"] == "off" and detail.get("buyer") \
                and detail["buyer"] >= self.settings["min_sell_cents"]:
            action = "sell"
        mode = self.settings.get(action, "off") if action in ("sell", "gems") else "off"
        if why == "existing" and mode != "off":
            mode = "review"
        if action == "keep" or mode == "off":
            if why == "drop":
                self.log(f"New card: {label}; kept ({detail.get('why') or f'{action} is off'})")
            return
        entry = {"id": card["assetid"], "card": card, "action": action, "detail": detail, "at": time.time()}
        if mode == "auto":
            self.log(f"New card: {label}")
            self.jobs.put((action, entry))
        else:
            with self.lock:
                if not any(e["id"] == entry["id"] for e in self.state["review"]):
                    self.state["review"].append(entry)
            if why == "drop":
                self.log(f"New card: {label}; waiting for your review")

    def review_existing(self):
        """Run every card already in the inventory through the rules, into the review list."""
        threading.Thread(target=self._review_existing, daemon=True).start()

    def _review_existing(self):
        self.working += 1
        try:
            queued = {e["id"] for e in self.state["review"]}
            cards = [c for c in steam.cards_in_inventory(self.session) if c["assetid"] not in queued]
            self.log(f"Pricing {len(cards)} cards you already have…")
            for c in cards:
                self.handle(c, "existing")
            self.log(f"Review list ready: {len(self.state['review'])} cards")
        except Exception as e:
            self.log(f"Couldn't review your cards: {e}", "warn")
        finally:
            self.working -= 1

    def decide_review(self, ids, action):
        """The person's call on queued cards: sell / gems / keep / suggested."""
        with self.lock:
            picked = [e for e in self.state["review"] if e["id"] in ids or ids == "all"]
            self.state["review"] = [e for e in self.state["review"] if e not in picked]
        for e in picked:
            act = e["action"] if action == "suggested" else action
            if act == "keep":
                with self.lock:
                    self.state["seen"].append(e["id"])
                continue
            self.jobs.put((act, e))

    # --- doing it ---------------------------------------------------------------------------------
    def _worker(self):
        listed = 0
        while True:
            try:
                action, e = self.jobs.get(timeout=20)
            except queue.Empty:
                if listed and self.settings["notify"]:
                    _notify(f"{listed} card listing{'s' * (listed > 1)} waiting",
                            "Confirm them in the Steam mobile app (Confirmations).")
                listed = 0
                continue
            self.working += 1
            try:
                listed += self._do(action, e)
            except Exception as err:
                self.log(f"Couldn't {action} {e['card']['name']}: {err}", "warn")
            finally:
                self.working -= 1
                time.sleep(1.5)

    def _do(self, action, e):
        card, d = e["card"], e["detail"]
        label = f"{card['name']} ({card['game']})"
        if action == "sell":
            buyer = d.get("buyer")
            if not buyer:
                raise RuntimeError("nobody is selling it, so there's no price to match")
            receive = rules.received(buyer)
            steam.sell(self.session, card["assetid"], receive)
            with self.lock:
                self.state["stats"]["listed"] += 1
                self.state["stats"]["listed_cents"] += receive
            self.log(f"Listed {label} for ${buyer / 100:.2f} (you get ${receive / 100:.2f})", "sell")
            return 1
        gems = steam.grind(self.session, card["appid"], card["assetid"], d["gems"])
        with self.lock:
            self.state["stats"]["gems"] += gems
        self.log(f"Turned {label} into {gems} gems", "gems")
        return 0

    # --- for the page --------------------------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            st = self.state
            now = time.time()
            return {
                "signed_in": self.session.signed_in, "account": self.session.account,
                "qr": self.session.qr and self.session.qr["url"],
                "status": self.status, "error": self.error, "working": self.working + self.jobs.qsize(),
                "settings": self.settings,
                "idling": [{"appid": int(a), "name": self._name(int(a)), "since": i["since"],
                            "last_drop": i["last_drop"]} for a, i in st["idling"].items()],
                "games": [dict(g, value=self._game_value(g["appid"]),
                               skipped=st["skipped"].get(str(g["appid"]), 0) > now)
                          for g in self._candidates()] if self.session.signed_in else [],
                "drops_left": sum(g["drops"] for g in st["games"]),
                "review": st["review"], "history": st["history"][:80], "stats": st["stats"],
                "ui": int(os.path.getmtime(os.path.join(os.path.dirname(__file__), "ui", "index.html"))),
            }

    def unskip(self, appid):
        with self.lock:
            self.state["skipped"].pop(str(appid), None)
        self.poke()

    def skip(self, appid):
        with self.lock:
            self.state["skipped"][str(appid)] = time.time() + SKIP_FOR
        self.poke()


def _notify(title, body):
    try:
        subprocess.run(["notify-send", "-a", "Card Farm", "-i", "steam", title, body], timeout=10)
    except Exception:
        pass
