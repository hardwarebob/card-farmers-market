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


def unblock_downloaded_files():
    """Windows marks every file unzipped from a download as "from the internet", and .NET then
    refuses to load the DLLs the window needs (pythonnet: "Failed to resolve
    Python.Runtime.Loader.Initialize"). Take that mark off Card Farm's own files."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    for folder, _, files in os.walk(os.path.dirname(sys.executable)):
        for name in files:
            try:
                os.remove(os.path.join(folder, name) + ":Zone.Identifier")
            except OSError:
                pass


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--idle-child":  # one game's idler (the packaged app runs itself)
        from .idler import child
        return child(sys.argv[2])
    data = data_dir()
    background = "--background" in sys.argv
    if "--tui" in sys.argv:
        from .tui import run_tui
        try:
            return run_tui(data)
        except RuntimeError as e:
            sys.exit(str(e))
    if "--service" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(prog="card-farm --service")
        parser.add_argument("--service", action="store_true")
        parser.add_argument("--host", default=os.environ.get("CARD_FARM_HOST", "127.0.0.1"))
        parser.add_argument("--port", type=int, default=int(os.environ.get("CARD_FARM_PORT", "8765")))
        args = parser.parse_args(sys.argv[1:])
        from .farm import Farm
        from .service import serve
        return serve(lambda: Farm(data), args.host, args.port)
    from .farm import Farm
    if sys.platform.startswith("linux"):
        try:
            from .app import run_window
        except (ImportError, ValueError) as e:
            sys.exit(f"Card Farmer's Market needs GTK 4, libadwaita and WebKitGTK 6 with their Python bindings ({e}).")
        sys.exit(run_window(lambda: Farm(data), data, background=background))
    unblock_downloaded_files()
    try:
        from .app_webview import run_window
    except ImportError as e:
        sys.exit(f"Card Farmer's Market needs the pywebview package here ({e}).")
    sys.exit(run_window(lambda: Farm(data), data, background=background))


if __name__ == "__main__":
    main()
