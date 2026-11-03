"""card-farm: opens Card Farm's window (and farms while it's open or in the background).
card-farm --background: farm without opening the window; running card-farm again shows it.

CARD_FARM_DATA picks where settings and the Steam sign-in live (default ~/.local/share/card-farm)."""
import os
import sys


def main():
    data = os.environ.get("CARD_FARM_DATA") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "card-farm")
    try:
        from .app import run_window
    except (ImportError, ValueError) as e:
        sys.exit(f"Card Farm needs GTK 4, libadwaita and WebKitGTK 6 with their Python bindings ({e}).")
    from .farm import Farm
    sys.exit(run_window(lambda: Farm(data), background="--background" in sys.argv))


if __name__ == "__main__":
    main()
