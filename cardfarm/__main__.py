"""card-farm (or python -m cardfarm): the farm loop plus its control page.

CARD_FARM_DATA picks where settings and the Steam sign-in live (default ~/.local/share/card-farm),
CARD_FARM_PORT the page's port (default 8797)."""
import os
import threading

from .farm import Farm
from .web import PORT, serve


def main():
    data = os.environ.get("CARD_FARM_DATA") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "card-farm")
    port = int(os.environ.get("CARD_FARM_PORT") or PORT)
    farm = Farm(data)
    threading.Thread(target=serve, args=(farm, port), daemon=True).start()
    print(f"Card Farm on http://127.0.0.1:{port}", flush=True)
    farm.run()


if __name__ == "__main__":
    main()
