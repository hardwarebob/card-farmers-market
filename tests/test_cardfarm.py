import hashlib
import json
import pathlib
import threading
import time
import urllib.request
from urllib.error import HTTPError

import pytest

from cardfarm import qr, rules, steam, web

HERE = pathlib.Path(__file__).parent


# --- rules ---------------------------------------------------------------------------------
def card(**kw):
    return dict({"assetid": "1", "appid": 10, "name": "Hero", "game": "Game", "hash": "10-Hero",
                 "foil": False, "marketable": True}, **kw)


S = dict(rules.DEFAULTS)


def test_received_matches_steam_fees():
    # what a buyer pays -> what the seller gets
    assert [rules.received(c) for c in (3, 10, 18, 92, 115)] == [1, 8, 16, 80, 100]


def test_sells_when_the_market_beats_gems():
    action, d = rules.decide(card(), 18, 80, 70, S)
    assert action == "sell" and d["buyer"] == 18 and d["receive"] == 16


def test_gems_when_nobody_sells_or_too_cheap():
    assert rules.decide(card(), None, 20, 70, S)[0] == "gems"
    assert rules.decide(card(), 2, 20, 70, S)[0] == "gems"       # under min_sell_cents
    rich_gems = rules.decide(card(), 4, 2000, 70, S)              # 2000 gems are worth more than 2c
    assert rich_gems[0] == "gems"


def test_keep_rules():
    assert rules.decide(card(appid=7), 50, 20, 70, dict(S, never_touch=[7]))[0] == "keep"
    assert rules.decide(card(foil=True), 90, 20, 70, dict(S, foils="keep"))[0] == "keep"
    assert rules.decide(card(), None, 0, 70, S)[0] == "keep"


def test_undercut_by_a_cent():
    assert rules.decide(card(), 18, 0, 70, dict(S, pricing="undercut"))[1]["buyer"] == 17


def test_settings_are_cleaned():
    assert rules.clean({"sell": "auto", "gems": "maybe", "games_at_once": 99, "bogus": 1,
                        "never_touch": ["5", 5, 6]}) == {"sell": "auto", "games_at_once": 32, "never_touch": [5, 6]}


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


# --- the page's door ------------------------------------------------------------------------------
class FakeFarm:
    def __init__(self):
        self.settings = {}

    def snapshot(self):
        return {"ok": True}

    def update_settings(self, patch):
        self.settings.update(patch)

    class session:
        qr = None


@pytest.fixture(scope="module")
def server():
    farm = FakeFarm()
    threading.Thread(target=web.serve, args=(farm, 18797), daemon=True).start()
    time.sleep(0.5)
    return farm


def call(path, body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:18797{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except HTTPError as e:
        return e.code


def test_changes_need_the_header(server):
    assert call("/api/state") == 200
    assert call("/api/settings", {"sell": "off"}) == 403
    assert "sell" not in server.settings
    assert call("/api/settings", {"sell": "off"}, {"X-Card-Farm": "1"}) == 200
    assert server.settings == {"sell": "off"}


def test_other_hosts_are_refused(server):
    assert call("/api/state", headers={"Host": "evil.example:18797"}) == 403
