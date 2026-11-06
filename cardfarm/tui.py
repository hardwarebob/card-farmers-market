"""Curses-based terminal frontend for the Card Farm service."""
import curses
import json
import os
import sys
import time
import webbrowser

from .service import clear_frontend, ensure_running, request


def _text(value):
    return "".join(" " if ord(c) < 32 else c for c in str(value))


def _put(screen, y, x, value, width, attr=0):
    if y < 0 or y >= curses.LINES or width <= 0:
        return
    try:
        screen.addnstr(y, x, _text(value), width, attr)
    except (curses.error, UnicodeError):
        pass


def run_tui(data_dir):
    """Connect to the managed local service (or CARD_FARM_URL) and run the terminal UI."""
    if not (getattr(sys.stdin, "isatty", lambda: False)() and
            getattr(sys.stdout, "isatty", lambda: False)()):
        raise RuntimeError("the terminal interface needs an interactive terminal")
    base = ensure_running(data_dir)
    try:
        return curses.wrapper(_main, base, data_dir)
    finally:
        clear_frontend(data_dir)


def _main(screen, base, data_dir):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(800)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)

    page, selected, message = 0, 0, "Connected to " + base
    state = {}
    last_poll = 0

    def set_message(text):
        nonlocal message
        message = text

    while True:
        if os.path.exists(os.path.join(data_dir, "service-stopping")):
            message = "Service is stopping; closing the terminal frontend"
            _draw(screen, state, page, selected, message, base)
            time.sleep(.5)
            return 0
        now = time.monotonic()
        if now - last_poll >= 1:
            try:
                status, _, data = request(base, "GET", "/api/state", timeout=1)
                if status != 200:
                    raise RuntimeError(f"service returned HTTP {status}")
                state = json.loads(data)
                last_poll = now
            except Exception as e:
                message = f"Service request failed: {e}"
                last_poll = now
        _draw(screen, state, page, selected, message, base)
        key = screen.getch()
        if key == -1:
            continue
        if key in (ord("q"), ord("Q")):
            if key == ord("Q") and _confirm(screen, "Stop the farm service? Press y to confirm"):
                _post(base, "/api/quit", {}, set_message)
            return 0
        if key in (ord("1"), ord("2"), ord("3")):
            page = key - ord("1")
            selected = 0
        elif key in (curses.KEY_DOWN, ord("j")):
            selected += 1
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (ord("f"), ord("F")):
            settings = state.get("settings", {})
            _post(base, "/api/settings", {"farming": not settings.get("farming", False)}, set_message)
        elif key in (ord("r"), ord("R")):
            _post(base, "/api/refresh", {}, set_message)
        elif key in (ord("c"), ord("C")):
            _post(base, "/api/review-existing", {}, set_message)
        elif key in (ord("i"), ord("I")):
            _post(base, "/api/items/check", {}, set_message)
        elif key in (ord("o"), ord("O")):
            webbrowser.open(base + "/")
            message = "Opened the web interface in your browser"
        elif page == 1 and key in (ord("s"), ord("g")):
            reviews = state.get("review", [])
            if selected < len(reviews):
                entry = reviews[selected]
                if entry.get("pricing") == "queued":
                    message = "That card is still being priced"
                else:
                    action = "sell" if key == ord("s") else "gems"
                    label = entry.get("card", {}).get("name", "this card")
                    if _confirm(screen, f"{action.title()} {label}? Press y to confirm"):
                        _post(base, "/api/review", {"ids": [entry["id"]], "action": action}, set_message)
        elif page == 1 and key in (ord("h"), ord("H")):
            reviews = state.get("review", [])
            if selected < len(reviews):
                entry = reviews[selected]
                if entry.get("pricing") == "queued":
                    message = "That card is still being priced"
                else:
                    _post(base, "/api/review", {"ids": [entry["id"]], "action": "keep"}, set_message)
        if page == 1:
            selected = min(selected, max(0, len(state.get("review", [])) - 1))
        elif page == 2:
            selected = min(selected, max(0, len(state.get("history", [])) - 1))
        else:
            selected = 0


def _post(base, path, body, set_message):
    try:
        status, _, data = request(base, "POST", path, json.dumps(body).encode(), timeout=10)
        result = json.loads(data)
        set_message(result.get("error") or ("Request sent" if status < 400 else f"HTTP {status}"))
    except Exception as e:
        set_message(f"Request failed: {e}")


def _confirm(screen, prompt):
    _put(screen, curses.LINES - 2, 0, prompt, curses.COLS, curses.color_pair(2) if curses.has_colors() else 0)
    screen.refresh()
    return screen.getch() in (ord("y"), ord("Y"))


def _draw(screen, state, page, selected, message, base):
    screen.erase()
    width, height = curses.COLS, curses.LINES
    title = "Card Farmer's Market  |  1 Overview  2 Cards  3 Activity"
    _put(screen, 0, 0, title, width, curses.A_BOLD)
    tab_name = ("Overview", "Cards to review", "Recent activity")[page]
    _put(screen, 1, 0, f"{tab_name}  •  {state.get('account') or 'Not signed in'}", width,
         curses.color_pair(3) if curses.has_colors() else curses.A_BOLD)
    _put(screen, 2, 0, f"Service: {base}   Status: {state.get('status', 'Connecting…')}", width)
    if state.get("error"):
        _put(screen, 3, 0, "Error: " + state["error"], width, curses.color_pair(2) if curses.has_colors() else 0)

    bottom = max(5, height - 2)
    if page == 0:
        settings = state.get("settings", {})
        stats = state.get("stats", {})
        _put(screen, 5, 0, f"Farming: {'ON' if settings.get('farming') else 'OFF'}   "
             f"Drops left: {state.get('drops_left', 0)}   Cards farmed: {stats.get('drops', 0)}   "
             f"Listed: {stats.get('listed', 0)}   Gems: {stats.get('gems', 0)}", width)
        _put(screen, 7, 0, "Games currently idling:", width, curses.A_BOLD)
        games = state.get("idling", [])
        if not games:
            _put(screen, 8, 0, "None", width)
        for row, game in enumerate(games[:max(0, bottom - 9)], 8):
            minutes = int(game.get("played", 0))
            _put(screen, row, 2, f"{game.get('name', '?')}  {minutes // 60}h {minutes % 60:02}m", width - 2)
        if not state.get("signed_in"):
            _put(screen, min(height - 4, 10), 0,
                 "Sign in with the web interface: press o to open it in your browser.", width)
        _put(screen, height - 2, 0, "f Toggle farming   r Refresh   c Sort cards I have   i Check items   o Web UI   q Exit TUI   Q Stop service", width,
             curses.color_pair(1) if curses.has_colors() else 0)
    elif page == 1:
        reviews = state.get("review", [])
        _put(screen, 4, 0, f"{len(reviews)} cards   Up/Down or j/k select   s Sell   g Gems   h Keep", width)
        start = max(0, selected - max(1, bottom - 7))
        for i, entry in enumerate(reviews[start:start + max(0, bottom - 5)], start):
            card = entry.get("card", {})
            pricing = entry.get("pricing") == "queued"
            price = "Pricing queued" if pricing else f"ask {entry.get('detail', {}).get('buyer', 'n/a')} cents"
            line = f"{card.get('name', '?')} — {card.get('game', '?')} — {price}"
            attr = curses.A_REVERSE if i == selected else 0
            if pricing and curses.has_colors():
                attr |= curses.color_pair(2)
            _put(screen, 5 + i - start, 0, line, width, attr)
        _put(screen, height - 2, 0, "s Sell (confirm)   g Convert to gems (confirm)   h Keep   q Exit TUI", width,
             curses.color_pair(1) if curses.has_colors() else 0)
    else:
        history = state.get("history", [])
        start = max(0, selected - max(1, bottom - 5))
        for i, entry in enumerate(history[start:start + max(0, bottom - 3)], start):
            line = f"{entry.get('kind', 'info'):>5}  {entry.get('text', '')}"
            _put(screen, 4 + i - start, 0, line, width, curses.A_REVERSE if i == selected else 0)
        _put(screen, height - 2, 0, "Up/Down or j/k scroll   q Exit TUI", width,
             curses.color_pair(1) if curses.has_colors() else 0)
    _put(screen, height - 1, 0, message, width, curses.color_pair(2) if curses.has_colors() else 0)
    screen.refresh()
