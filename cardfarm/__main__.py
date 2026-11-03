"""card-farm: opens Card Farm's window (and farms while it's open or in the background).
card-farm --headless: farm with no window; the control panel is then at http://127.0.0.1:8797.

CARD_FARM_DATA picks where settings and the Steam sign-in live (default ~/.local/share/card-farm),
CARD_FARM_PORT the panel's port (default 8797)."""
import json
import os
import sys
import threading
import urllib.request

from .farm import Farm
from .web import PORT, serve


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return "signed_in" in json.load(r)
    except Exception:
        return False


def main():
    data = os.environ.get("CARD_FARM_DATA") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "card-farm")
    port = int(os.environ.get("CARD_FARM_PORT") or PORT)

    def start_farm(block=False):
        if already_running(port):
            return None  # e.g. the background service; the window just shows it
        farm = Farm(data)
        threading.Thread(target=serve, args=(farm, port), daemon=True).start()
        print(f"Card Farm on http://127.0.0.1:{port}", flush=True)
        if block:
            farm.run()
        threading.Thread(target=farm.run, daemon=True).start()
        return farm

    if "--headless" not in sys.argv:
        try:
            from .app import run_window
        except (ImportError, ValueError) as e:
            print(f"No window ({e}); needs GTK 4, libadwaita and WebKitGTK 6. Running headless instead.", flush=True)
        else:
            sys.exit(run_window(start_farm, port))
    if already_running(port):
        sys.exit(f"Card Farm is already running on http://127.0.0.1:{port}")
    start_farm(block=True)


if __name__ == "__main__":
    main()
