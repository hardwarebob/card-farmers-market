import hashlib
import json
import pathlib
import threading
import time

import pytest

from cardfarm import api, farm as farm_mod, idler, qr, rules, steam

HERE = pathlib.Path(__file__).parent


# --- rules ---------------------------------------------------------------------------------
def card(**kw):
    return dict({"assetid": "1", "appid": 10, "name": "Hero", "game": "Game", "hash": "10-Hero",
                 "foil": False, "marketable": True, "icon": "", "goo": None}, **kw)


S = dict(rules.DEFAULTS)


def test_received_matches_steam_fees():
    # what a buyer pays -> what the seller gets
    assert [rules.received(c) for c in (3, 10, 18, 92, 115)] == [1, 8, 16, 80, 100]
    # 22c can't be hit exactly: the seller gets 19c and buyers see 21c
    assert rules.received(22) == 19 and rules.buyer_pays(19) == 21


def test_sells_when_the_market_beats_gems():
    action, d = rules.decide(card(), 18, 80, 70, S)
    assert action == "sell" and d["buyer"] == 18 and d["receive"] == 16


def test_gems_when_nobody_sells_or_too_cheap():
    assert rules.decide(card(), None, 20, 70, S)[0] == "gems"
    assert rules.decide(card(), 2, 20, 70, S)[0] == "gems"       # under min_sell_cents
    rich_gems = rules.decide(card(), 4, 2000, 70, S)              # 2000 gems are worth more than 2c
    assert rich_gems[0] == "gems" and rich_gems[1]["receive"] == 2  # the page can still show a sale


def test_keep_rules():
    assert rules.decide(card(appid=7), 50, 20, 70, dict(S, never_touch=[7]))[0] == "keep"
    assert rules.decide(card(foil=True), 90, 20, 70, dict(S, foils="keep"))[0] == "keep"
    assert rules.decide(card(), None, 0, 70, S)[0] == "keep"


def test_undercut_by_a_cent():
    assert rules.decide(card(), 18, 0, 70, dict(S, pricing="undercut"))[1]["buyer"] == 17


def test_settings_are_cleaned():
    assert rules.clean({"sell": "auto", "gems": "maybe", "games_at_once": 99, "bogus": 1,
                        "never_touch": ["5", 5, 6]}) == {"sell": "auto", "games_at_once": 32, "never_touch": [5, 6]}
    assert rules.clean({"items_inventories": ["730/2", "nope", "440/2"], "items_new": "auto"}) == \
        {"items_inventories": ["440/2", "730/2"], "items_new": "auto"}


def test_items_are_left_alone_by_default():
    assert rules.DEFAULTS["items_new"] == "off" and rules.DEFAULTS["items_inventories"] == []


def test_item_rules():
    item = {"hash": "Case", "name": "Case"}
    assert rules.decide_item(item, 119, S) == ("sell", {"buyer": 119, "receive": 104})
    assert rules.decide_item(item, 4, S)[0] == "keep"            # under items_min_cents
    assert rules.decide_item(item, None, S)[0] == "keep"         # nobody sells one
    assert rules.decide_item(item, 119, dict(S, items_keep=["Case"]))[0] == "keep"


# --- badges page ---------------------------------------------------------------------------------
def test_badges_page():
    page = (HERE / "badges_page.html").read_text()
    assert steam.parse_badges(page) == [
        {"appid": 440, "name": "Team & Fortress", "drops": 3, "earned": 4},
        {"appid": 620, "name": "Portal Two", "drops": 1, "earned": 1},
        {"appid": 730, "name": "Strike", "drops": 0, "earned": 0},
    ]
    assert steam.page_count(page) == 2


# --- QR -----------------------------------------------------------------------------------------
def test_qr_shape():
    m = qr.matrix("https://s.team/q/1/2625894210510258091")
    assert len(m) == 29 and all(len(r) == 29 for r in m)  # version 3
    finder = [r[:7] for r in m[:7]]
    assert finder[0] == [True] * 7 and finder[1] == [True] + [False] * 5 + [True]
    assert qr.svg("hi").startswith("<svg")


def test_qr_unchanged():
    # these exact codes were read back correctly by zbar; any change to the encoder must be re-checked
    def digest(text):
        return hashlib.sha256("".join("1" if d else "0" for r in qr.matrix(text) for d in r).encode()).hexdigest()[:16]
    assert digest("hi") == HI and digest("y" * 100) == LONG


HI = "69f439bee9c38226"
LONG = "058cebbfc8fda720"


# --- the window's requests ------------------------------------------------------------------------
class PageFarm:
    def __init__(self):
        self.settings = {}
        self.decided = None

    def snapshot(self):
        return {"ok": True}

    def update_settings(self, patch):
        self.settings.update(patch)

    def decide_review(self, ids, action, items=False):
        self.decided = (ids, action, items)


def test_page_and_state():
    status, kind, body = api.answer(PageFarm(), "GET", "/", b"")
    assert status == 200 and kind.startswith("text/html") and b"Card Farm" in body
    assert api.answer(PageFarm(), "GET", "/api/state", b"")[2] == b'{"ok": true}'
    assert api.answer(PageFarm(), "GET", "/nope", b"")[0] == 404


def test_actions():
    f = PageFarm()
    assert api.answer(f, "POST", "/api/settings", b'{"sell": "off"}')[0] == 200 and f.settings == {"sell": "off"}
    assert api.answer(f, "POST", "/api/items/review", b'{"ids": ["9"], "action": "always"}')[0] == 200
    assert f.decided == ({"9"}, "always", True)
    assert api.answer(f, "POST", "/api/review", b'{"ids": "all", "action": "always"}')[0] == 400  # items only
    assert api.answer(f, "POST", "/api/bogus", b"{}")[0] == 400


# --- the farm ----------------------------------------------------------------------------------
class FakeIdler:
    def __init__(self, farm=None):
        self.procs, self.farm, self.lock_free, self.library = set(), farm, [], "lib"

    def running(self):
        return set(self.procs)

    def start(self, appids):
        if self.farm:  # starting games must not hold the lock the window reads through
            def probe():
                ok = self.farm.lock.acquire(timeout=0.2)
                if ok:
                    self.farm.lock.release()
                self.lock_free.append(ok)
            t = threading.Thread(target=probe)
            t.start(); t.join()
        self.procs |= set(appids)
        return set()

    def stop(self, appid):
        self.procs.discard(appid)

    def stop_all(self):
        self.procs.clear()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A signed-in farm whose Steam is a dict we control."""
    (tmp_path / "session.json").write_text(json.dumps({"steamid": "7656", "refresh_token": "x", "access_token": "y"}))
    w = {"cards": [], "pending": set(), "badges": [], "inventories": [], "items": {}, "sold": []}
    monkeypatch.setattr(steam, "cards_in_inventory", lambda s: list(w["cards"]))
    monkeypatch.setattr(steam, "pending_listings", lambda s: set(w["pending"]))
    monkeypatch.setattr(steam, "badges", lambda s: [dict(g) for g in w["badges"]])
    monkeypatch.setattr(steam, "inventories", lambda s: list(w["inventories"]))
    monkeypatch.setattr(steam, "items_in_inventory", lambda s, a, c, n="": list(w["items"].get(f"{a}/{c}", [])))
    monkeypatch.setattr(steam, "sell", lambda s, asset, receive, *a: w["sold"].append((asset, receive)))

    def make():
        f = farm_mod.Farm(str(tmp_path))
        f.idler = FakeIdler(f)
        f.prices.game_cards = lambda appid: {"10-Hero": 50}
        f.prices.gems = lambda *a: 20
        f.prices.gem_sack = lambda: 70
        f.prices.item = lambda appid, name: 119
        f.update_settings({"sell": "auto"})
        return f
    w["make"] = make
    return w


def test_only_real_drops_are_sold_automatically(world):
    f = world["make"]()
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f._refresh_badges()
    f._check_inventory()                       # first look: whatever is there is left alone
    world["cards"] = [card(assetid="1")]
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 2, "earned": 3}]
    f._refresh_badges(); f._check_inventory()  # a drop: sold by the rules
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["1"] and f.state["stats"]["drops"] == 1
    world["cards"].append(card(assetid="2"))   # turned up without a drop (bought for a badge?)
    f._refresh_badges(); f._check_inventory()
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["1"]
    assert [e["id"] for e in f.state["review"]] == ["2"] and f.state["stats"]["drops"] == 1


def test_queue_survives_a_restart(world):
    f = world["make"]()
    f._enqueue("sell", {"id": "1", "kind": "card", "card": card(), "detail": {}})
    f.save()
    g = world["make"]()
    assert [j["entry"]["id"] for j in g.state["queue"]] == ["1"]


def test_sorting_skips_listed_kept_and_gone_cards(world):
    f = world["make"]()
    f.state["baseline"] = True
    world["cards"] = [card(assetid=a) for a in "123"]
    world["pending"] = {"2"}                       # listed, waiting for the phone
    f.decide_review(set(), "keep")
    f.state["kept"] = ["3"]
    f._review_existing()
    assert [e["id"] for e in f.state["review"]] == ["1"]
    world["cards"] = []                            # sold elsewhere
    f._check_inventory()
    assert f.state["review"] == [] and f.state["kept"] == []


def test_another_account_starts_fresh(world):
    f = world["make"]()
    f.state.update(seen=["1"], baseline=True, review=[{"id": "1"}])
    f.session.steamid = "other"
    f._for_account()
    assert f.state["seen"] == [] and not f.state["baseline"] and f.state["steamid"] == "other"


def test_skipped_games_stay_on_the_page(world):
    f = world["make"]()
    f.state["games"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f.skip(10)
    games = f.snapshot()["games"]
    assert [(g["appid"], g["skipped"]) for g in games] == [(10, True)]


def test_games_start_without_holding_the_lock(world):
    f = world["make"]()
    f.state["games"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f._choose_games()
    assert f.idler.lock_free == [True] and list(f.state["idling"]) == ["10"]


def test_items_wait_for_you_unless_auto(world):
    f = world["make"]()
    world["inventories"] = [{"key": "730/2", "appid": 730, "context": 2, "name": "CS2", "count": 2}]
    case = {"assetid": "c1", "appid": 730, "context": 2, "amount": 1, "name": "Case", "hash": "Case",
            "game": "CS2", "icon": "", "kind": "Container"}
    world["items"]["730/2"] = [case]
    f._check_items(manual=True)                       # no inventory switched on: only lists them
    assert f.state["items"]["review"] == [] and f.state["items"]["inventories"][0]["key"] == "730/2"
    f.update_settings({"items_inventories": ["730/2"], "items_new": "auto"})
    f._check_items(manual=False)                      # first look at this inventory: nothing happens
    assert f.state["queue"] == [] and f.state["items"]["review"] == []
    world["items"]["730/2"] = [case, dict(case, assetid="c2")]
    f._check_items(manual=False)                      # a new one, with Auto on: listed
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["c2"]
    f._check_items(manual=True)                       # the older one waits for review
    assert [e["id"] for e in f.state["items"]["review"]] == ["c1"]
    f.decide_review({"c1"}, "always", items=True)
    assert f.settings["items_keep"] == ["Case"] and f.state["items"]["review"] == []


def test_listing_uses_todays_price(world):
    f = world["make"]()
    f._do("sell", {"kind": "card", "card": card(), "detail": {"buyer": 500}})
    assert world["sold"] == [("1", rules.received(50))]


# --- finding Steam on each system -------------------------------------------------------------
def test_library_folders_are_read():
    vdf = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"C:\\\\Program Files (x86)\\\\Steam"\n\t}\n' \
          '\t"1"\n\t{\n\t\t"path"\t\t"D:\\\\SteamLibrary"\n\t}\n}\n'
    assert idler.parse_library_folders(vdf) == ["C:\\Program Files (x86)\\Steam", "D:\\SteamLibrary"]


def test_packaged_app_is_its_own_idler(monkeypatch):
    monkeypatch.setattr(idler.sys, "frozen", True, raising=False)
    monkeypatch.setattr(idler.sys, "executable", "/Applications/Card Farm.app/Contents/MacOS/Card Farm")
    assert idler.child_command("lib") == ["/Applications/Card Farm.app/Contents/MacOS/Card Farm", "--idle-child", "lib"]


def test_idler_child_gives_up_on_a_bad_library():
    import subprocess
    assert subprocess.run(idler.child_command("/no/such/library"), stdin=subprocess.DEVNULL).returncode == 2


def test_idling_starts_before_the_inventory_is_read(world, monkeypatch):
    """A slow or rate-limited inventory mustn't keep games from starting."""
    f = world["make"]()
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    monkeypatch.setattr(farm_mod, "steam_pid", lambda: 1)
    monkeypatch.setattr(farm_mod, "game_you_are_playing", lambda ours: None)

    def limited(session):
        raise steam.HttpError(429)
    monkeypatch.setattr(steam, "cards_in_inventory", limited)
    with pytest.raises(steam.HttpError):
        f.tick()
    assert list(f.state["idling"]) == ["10"] and f.status == "Farming Game"


def test_market_requests_are_spaced_out(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(steam, "get_json", lambda url: asked.append(time.time()) or {"goo_value": "5"})
    p = steam.Prices(str(tmp_path / "prices.json"))
    p.GAP = 0.2
    for appid in (1, 2, 3):
        p.gems(appid, 3)
    assert all(b - a >= 0.19 for a, b in zip(asked, asked[1:])) and len(asked) == 3
