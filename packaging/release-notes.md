Card Farmer's Market farms your Steam trading card drops, then sells each card or turns it into gems by rules you set. The Items tab can also list skins, cases and other marketable items, only when you say so.

**New in 0.3:**
- **Market tab:** your listings next to today's prices, with Reprice (cancel and relist) and Cancel; your sales by day and by game, and an optional notice when something sells.
- **Gems tab:** pack gems into sacks and sell them; booster packs whose market price beats their gem cost, made and listed in one click.
- **Cards tab:** card sets you hold most of: "Buy & craft" buys the missing cards (within your limit, checked before anything is bought) and crafts the badge; the set's cards are kept, not sold, until it's done. Also how long each game took per drop.
- New name: Card Farmer's Market (the download files keep the Card Farm name so updates keep working).
- Updates install themselves: use the Update button at the top when a new version is out.

## Downloads

| System | File | How to start it |
|---|---|---|
| Windows 10 / 11 (64-bit) | `Card-Farm-Windows.zip` | Unzip it anywhere and run **Card Farm.exe** in the `Card Farm` folder. Windows may warn that the app is from an unknown publisher: choose **More info → Run anyway**. |
| macOS 12 or newer, Apple silicon (M1 and later) | `Card-Farm-macOS.zip` | Unzip it and move **Card Farm** to Applications. The first time, right-click it and choose **Open**, then **Open** again (it isn't notarised by Apple). |
| Linux | `card-farm.pyz` | `chmod +x card-farm.pyz && ./card-farm.pyz`. Needs Python 3.10+, GTK 4, libadwaita and WebKitGTK 6 with PyGObject (most GNOME and KDE desktops have them). |

Steam must be running and signed in. Sign-in is by QR code with the Steam mobile app; It never sees your password. Market listings still need your OK in the Steam mobile app.

The Windows and macOS downloads contain Python and the pywebview library; their licences are in `THIRD-PARTY-NOTICES.txt` inside the download.
