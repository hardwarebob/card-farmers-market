# Card Farm

Farm your Steam trading card drops on Linux, Windows or macOS, then sell each card or turn it into gems by rules you set.
Card Farm is a desktop app with its own window. It keeps farming in the background when you close it.

- **Native idling.** It tells your running Steam client that a game is playing, the way the game itself would, through Steam's own API library. No Wine, Proton or helper programs.
- **Notices stalls.** It checks your badges page as it goes. A game that stops dropping cards gets restarted, then set aside for a while. When Steam restarts, the games are started again.
- **Sell or gems, decided per card.** Each card is priced against the market and against its gem value, using the current Sack of Gems price.
  - For selling and for gems separately, you choose **Auto**, **Review** (the card waits for you in the window) or **Off**.
  - You can also keep foils, keep every card from chosen games, set a minimum price, and match the lowest listing or undercut it by 1¢.
  - Only real card drops are handled automatically. A card that turns up any other way (a trade, a booster pack, one you bought to finish a badge) waits for your review.
- **Sort the cards you already have.** "Sort cards I have" runs your whole inventory through the same rules. Cards already listed and waiting for confirmation are left out.
- **Items too, when you ask.** The Items tab finds your other inventories (Counter-Strike 2, Team Fortress 2, the Steam inventory's backgrounds and emoticons, and so on) and prices what's marketable in the ones you switch on. Nothing is listed unless you say so: "New items" is Off until you change it, and its Auto mode only lists items that arrive after you turn it on.
- **Pauses while you play.** When you launch a game from Steam, farming waits until you're done.
- **Signs in by QR code** through the Steam mobile app. Card Farm never sees your password.

Market listings still need your OK in the Steam mobile app (Confirmations → Confirm all). Card Farm
tells you when some are waiting. Prices are looked up again just before each listing.

## Download

Get the latest version from the [Releases page](https://github.com/maybeimdreaming/card-farm/releases):

- **Windows 10/11:** `Card-Farm-Windows.zip`. Unzip it and run `Card Farm.exe`. Windows may say the publisher is unknown: **More info → Run anyway**. Closing the window while farming minimises it; use **Quit** at the top of the page to stop.
- **macOS (Apple silicon):** `Card-Farm-macOS.zip`. Move `Card Farm` to Applications; the first time, right-click it and choose **Open** (it isn't notarised).
- **Linux:** `card-farm.pyz`. `chmod +x card-farm.pyz && ./card-farm.pyz`, or install with pipx as below.

Steam must be running and signed in, and you need the Steam mobile app with Steam Guard for the sign-in and for confirming market listings. Card Farm finds Steam's own API library (`steam_api64.dll`, `libsteam_api.dylib` or `libsteam_api.so`) in any game you have installed and doesn't ship a copy; on Linux, any Proton version has one.

### Linux requirements

- Python 3.10 or newer, and `curl`
- GTK 4, libadwaita and WebKitGTK 6 with their Python bindings (PyGObject). Most GNOME and KDE desktops have these already.

### Install on Linux with pipx

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
menu to see it, or choose **Quit** in its menu to stop. Only one copy runs at a time.

To start farming when you log in, without opening the window (`card-farm --background`):

```sh
curl -o ~/.config/autostart/card-farm.desktop \
  https://raw.githubusercontent.com/maybeimdreaming/card-farm/main/extras/card-farm-autostart.desktop
```

Opening Card Farm from the menu then shows that running copy. (On Windows and macOS, `--background`
starts Card Farm minimised.)

Settings, the review lists and your Steam sign-in are kept in `~/.local/share/card-farm` on Linux,
`%APPDATA%\Card Farm` on Windows and `~/Library/Application Support/Card Farm` on macOS; the sign-in
file is readable only by you. `CARD_FARM_DATA` changes the folder.

## How the decision works

For each card, Card Farm compares two numbers:

1. What you'd receive from selling at the lowest current listing, after Steam's fee and the game's fee.
2. What the card's gems would fetch once sold as part of a 1,000-gem Sack of Gems.

The card is sold if the first number is higher, and turned into gems otherwise. Gems also win when
nobody is selling the card, or when the price is under your minimum. Gems are usually worth very
little, so most cards with a buyer end up sold.

Items other than cards are only ever sold (or kept). One is offered for sale when someone else is
selling it and the price is at least your "Don't list under" amount.

## Good to know

- Card drops come at Steam's pace, about one every 20–40 minutes of play per game. No tool can change that. What Card Farm saves is the time lost when an idler silently stalls.
- Turning a card into gems can't be undone. Listings can be cancelled from the Steam Market page.
- Card Farm doesn't run a web server or open a network port. Its window talks to the farm inside the same program, and only Steam's own servers are contacted.
- The Windows and macOS builds aren't signed by Microsoft or Apple, so the first start shows a warning (see Download).
- Card Farm is not affiliated with Valve. Use it on your own account, at your own pace.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests
python3 -m cardfarm                # window
python3 -m cardfarm --background   # farm without opening the window
python packaging/build.py linux    # dist/card-farm.pyz; windows / macos need: pip install pyinstaller pywebview pillow
```

Releases are built by `.github/workflows/release.yml` when a `v*` tag is pushed. Each build is
opened once on a real Windows and macOS machine to check its window works before it's published.

The code uses only the Python standard library, plus the system's web view for the window (GTK and
WebKitGTK on Linux; on Windows and macOS the pywebview library, which uses Edge WebView2 or WebKit),
and `curl` for web requests, because Steam's market rate-limits Python's own HTTP client much harder. The QR
code encoder (`cardfarm/qr.py`) and the icon were made for this project.

## License

MIT
