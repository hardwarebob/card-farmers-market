"""card-farm: opens Card Farm's window (and farms while it's open or in the background).
card-farm --background: farm without opening the window (on Windows and macOS: start minimised).

CARD_FARM_DATA picks where settings and the Steam sign-in live (default: ~/.local/share/card-farm on
Linux, %APPDATA%\\Card Farm on Windows, ~/Library/Application Support/Card Farm on macOS)."""
import os
import sys


def data_dir():
    if os.environ.get("CARD_FARM_DATA"):
        return os.environ["CARD_FARM_DATA"]
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Card Farm")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Card Farm")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "card-farm")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--idle-child":  # one game's idler (the packaged app runs itself)
        from .idler import child
        return child(sys.argv[2])
    data = data_dir()
    background = "--background" in sys.argv
    from .farm import Farm
    if sys.platform.startswith("linux"):
        try:
            from .app import run_window
        except (ImportError, ValueError) as e:
            sys.exit(f"Card Farm needs GTK 4, libadwaita and WebKitGTK 6 with their Python bindings ({e}).")
        sys.exit(run_window(lambda: Farm(data), background=background))
    try:
        from .app_webview import run_window
    except ImportError as e:
        sys.exit(f"Card Farm needs the pywebview package here ({e}).")
    sys.exit(run_window(lambda: Farm(data), data, background=background))


if __name__ == "__main__":
    main()
