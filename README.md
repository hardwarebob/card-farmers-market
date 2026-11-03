# Card Farm

Farm your Steam trading card drops on Linux, then sell each card or turn it into gems by rules you set.
Card Farm is a desktop app. It keeps farming in the background when you close its window.

- **Native Linux idling.** It tells your running Steam client that a game is playing, the way the game itself would. No Wine, Proton or `.exe` helpers.
- **Notices stalls.** It checks your badges page as it goes. A game that stops dropping cards gets restarted, then set aside for a while.
- **Sell or gems, decided per card.** Each card is priced against the market and against its gem value, using the current Sack of Gems price.
  - For selling and for gems separately, you choose **Auto**, **Review** (the card waits for you on the page) or **Off**.
  - You can also keep foils, keep every card from chosen games, set a minimum price, and match the lowest listing or undercut it by 1¢.
- **Sort the cards you already have.** "Review cards I already have" runs your whole inventory through the same rules.
- **Pauses while you play.** When you launch a game from Steam, farming waits until you're done.
- **Signs in by QR code** through the Steam mobile app. Card Farm never sees your password.

Market listings still need your OK in the Steam mobile app (Confirmations → Confirm all). Card Farm
tells you when some are waiting.

## Requirements

- Linux, with the Steam client running and signed in
- Python 3.10 or newer, and `curl`
- For the window: GTK 4, libadwaita and WebKitGTK 6 with their Python bindings (PyGObject). Most GNOME and KDE desktops have these already. Without them, Card Farm runs without a window and you use its panel in a browser at <http://127.0.0.1:8797>.
- Steam's own `libsteam_api.so`, which comes with any Proton version or most native Linux games in your library. Card Farm finds it automatically and doesn't ship a copy.
- The Steam mobile app with Steam Guard, needed for the sign-in and for market listings

## Install

```sh
pipx install git+https://github.com/maybeimdreaming/card-farm
card-farm
```

`card-farm` opens the window. To add it to your app menu:

```sh
curl -o ~/.local/share/applications/io.github.cardfarm.CardFarm.desktop \
  https://raw.githubusercontent.com/maybeimdreaming/card-farm/main/extras/io.github.cardfarm.CardFarm.desktop
```

Closing the window while farming keeps Card Farm running in the background; open it again from the
menu to see it, or choose **Quit** in its menu to stop.

To farm from login without opening the window (`card-farm --headless`):

```sh
mkdir -p ~/.config/systemd/user
curl -o ~/.config/systemd/user/card-farm.service https://raw.githubusercontent.com/maybeimdreaming/card-farm/main/extras/card-farm.service
systemctl --user enable --now card-farm
```

The window then connects to that background copy instead of starting a second one.

Settings, the review list and your Steam sign-in are kept in `~/.local/share/card-farm`; the sign-in
file is readable only by you. `CARD_FARM_DATA` and `CARD_FARM_PORT` change the folder and the port.

## How the decision works

For each card, Card Farm compares two numbers:

1. What you'd receive from selling at the lowest current listing, after Steam's fee and the game's fee.
2. What the card's gems would fetch once sold as part of a 1,000-gem Sack of Gems.

The card is sold if the first number is higher, and turned into gems otherwise. Gems also win when
nobody is selling the card, or when the price is under your minimum. Gems are usually worth very
little, so most cards with a buyer end up sold.

## Good to know

- Card drops come at Steam's pace, about one every 20–40 minutes of play per game. No tool can change that. What Card Farm saves is the time lost when an idler silently stalls.
- Turning a card into gems can't be undone. Listings can be cancelled from the Steam Market page.
- The control panel is served only to your own computer. Every change needs a header that other websites can't send, so a page open in another tab can't act on your account.
- Card Farm is not affiliated with Valve. Use it on your own account, at your own pace.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests
python3 -m cardfarm              # window
python3 -m cardfarm --headless   # no window; panel at http://127.0.0.1:8797
```

The code uses only the Python standard library, plus `curl` for web requests, because Steam's market
rate-limits Python's own HTTP client much harder. The QR code encoder (`cardfarm/qr.py`) is written
for this project.

## License

MIT
