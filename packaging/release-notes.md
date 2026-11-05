Card Farmer's Market farms your Steam trading card drops, then sells each card or turns it into gems by rules you set. The Items tab can also list skins, cases and other marketable items, only when you say so.

**New in 0.4:**
- **Stays signed in.** The sign-in now renews itself the way the Steam website does. Before, it stopped working about a day after signing in: drops weren't seen anymore and the games list went stale (games looked like they still had drops).
- **Start / Stop farming** at the top of the window.
- **Amounts in cents or dollars:** limits show as 50¢ under a dollar and $5.00 from there on; type either.
- A **Support on Ko-fi** link at the top of the window, for anyone who'd like to leave a tip.
- **Skipping:** a game you skip stays skipped until you unskip it, **Unskip all** brings every game back, and free-to-play games like CS2 (whose drops come from in-game purchases, while you play) are skipped unless you choose **Try**.
- **Market tab:** your listings next to today's prices, with Reprice (cancel and relist) and Cancel; a 30-day earnings chart, your sales by day and by game, and an optional notice when something sells.
- **Gems tab:** pack gems into sacks and sell them; booster packs whose market price beats their gem cost, made and listed in one click; booster packs you already have, sold or opened, whichever is worth more.
- **Level up tab:** pick a level and a budget, and it finds the cheapest badge crafts and does them, buying now or with buy orders under the market price.
- **Cards tab:** card sets you hold most of: "Buy & craft" buys the missing cards (within your limit, checked before anything is bought) and crafts the badge; the set's cards are kept, not sold, until it's done.
- **Sales checks:** cards, items and booster packs are priced at what really sells, not at old asking prices nobody pays.
- **Easier on Steam's market:** prices are looked up while signed in, and when Steam says "too many requests", lookups pause for a while instead of failing over and over.
- **One Mac download for every Mac:** `Card-Farm-macOS.zip` runs on Apple silicon and Intel.
- New name: Card Farmer's Market (the download files keep the Card Farm name for now, so updates keep working). Updates install themselves with the Update button at the top.

## Downloads

| System | File | How to start it |
|---|---|---|
| Windows 10 / 11 (64-bit) | `Card-Farm-Windows.zip` | Unzip it anywhere and run **Card Farm.exe** in the `Card Farm` folder. Windows may warn that the app is from an unknown publisher: choose **More info → Run anyway**. |
| macOS 12 or newer, Apple silicon or Intel | `Card-Farm-macOS.zip` | Unzip it and move **Card Farm** to Applications. The first time, right-click it and choose **Open**, then **Open** again (it isn't notarised by Apple). `Card-Farm-macOS-Intel.zip` is the same app, there so older copies on Intel Macs can update. |
| Linux | `card-farm.pyz` | `chmod +x card-farm.pyz && ./card-farm.pyz`. Needs Python 3.10+, GTK 4, libadwaita and WebKitGTK 6 with PyGObject (most GNOME and KDE desktops have them). |

Steam must be running and signed in. Sign-in is by QR code with the Steam mobile app; it never sees your password. Market listings still need your OK in the Steam mobile app.

The Windows and macOS downloads contain Python and the pywebview library; their licences are in `THIRD-PARTY-NOTICES.txt` inside the download.
