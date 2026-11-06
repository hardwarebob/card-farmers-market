"""GTK desktop frontend for the separate Card Farm HTTP service."""
import json
import os
import pkgutil

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, WebKit  # noqa: E402

from .service import ensure_running, service_is_farming  # noqa: E402
from .tray import FrontendTray  # noqa: E402

APP_ID = "io.github.cardfarm.CardFarm"  # kept from the Card Farm days: the menu entry, icon and settings use it
NAME = "Card Farmer's Market"
HOME = os.environ.get("CARD_FARM_URL", f"http://127.0.0.1:{os.environ.get('CARD_FARM_PORT', '8765')}/")


class CardFarmApp(Adw.Application):
    def __init__(self, make_farm, data_dir, background=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.make_farm, self.data_dir, self.start_hidden = make_farm, data_dir, background
        self.service_url = None
        self.window = self.view = None
        self._surface = None
        self.held = False
        self.tray = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name(NAME)
        _install_icon()
        Gtk.Window.set_default_icon_name(APP_ID)
        try:
            self.service_url = ensure_running(self.data_dir)
        except Exception as e:
            print(e, flush=True)
        if self.service_url:
            GLib.timeout_add_seconds(1, self._check_service_stopping)
        self.tray = FrontendTray(lambda: GLib.idle_add(self.activate),
                                 lambda: GLib.idle_add(self.quit))
        self.tray.start()
        for name, accels, act in (("quit", ["<Control>q"], self.quit),
                                  ("reload", ["<Control>r"], lambda: self.view and self.view.reload())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *a, act=act: act())
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self):
        if self.start_hidden:  # `card-farm --background`: farm now, show the window when asked
            self.start_hidden = False
            self._hold()
            return
        if self.window:
            self.window.present()
            return
        self.window = Adw.ApplicationWindow(application=self, title=NAME, default_width=1100, default_height=820)
        self.window.connect("close-request", self._on_close)
        self.window.connect("map", self._watch_minimize)

        menu = Gio.Menu()
        menu.append("Reload", "app.reload")
        menu.append("Quit", "app.quit")
        header = Adw.HeaderBar()
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        self.view = WebKit.WebView()
        self.view.connect("decide-policy", self._stay_inside)
        self.view.connect("context-menu", lambda *a: True)  # no browser menu (Back, Inspect, ...)
        self.view.load_uri(self.service_url or HOME)

        body = Adw.ToolbarView()
        body.add_top_bar(header)
        body.set_content(self.view)
        self.window.set_content(body)
        self.window.present()
        if os.environ.get("CARD_FARM_SELFTEST"):
            GLib.timeout_add_seconds(1, self._selftest, os.environ["CARD_FARM_SELFTEST"], [60])

    def _selftest(self, out, tries):
        """CARD_FARM_SELFTEST=<file>: once the page has drawn the farm's state, write what it shows
        to <file> and quit (how builds are checked)."""
        def done(view, result):
            try:
                shown = json.loads(view.evaluate_javascript_finish(result).to_string())
            except Exception as e:
                shown = {"state": False, "error": repr(e)}
            if shown.get("state") or tries[0] <= 0:
                with open(out, "w") as f:
                    json.dump(dict(shown, ok=bool(shown.get("state"))), f)
                self.quit()
            else:
                tries[0] -= 1
                GLib.timeout_add_seconds(1, self._selftest, out, tries)
        self.view.evaluate_javascript("JSON.stringify({state: !!state, signin: !document.querySelector('#signin').hidden, "
                                      "button: document.querySelector('#start-signin').textContent})", -1, None, None, None, done)
        return False

    def _stay_inside(self, view, decision, kind):
        """Links that leave Card Farm open in the normal browser."""
        if kind not in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        if uri.startswith(self.service_url or HOME) or uri.startswith("about:"):
            return False
        Gtk.UriLauncher.new(uri).launch(self.window, None, None, None)
        decision.ignore()
        return True

    def _hold(self):
        if not self.held:
            self.hold()  # keep running with no window
            self.held = True

    def _on_close(self, window):
        if not service_is_farming(self.service_url or HOME):
            return False
        window.set_visible(False)
        if not self.held:
            self._hold()
        return True

    def _watch_minimize(self, window):
        surface = window.get_surface()
        if isinstance(surface, Gdk.Toplevel) and surface is not self._surface:
            self._surface = surface
            surface.connect("notify::state", self._on_toplevel_state)
        return False

    def _on_toplevel_state(self, surface, _property):
        if surface.get_state() & Gdk.ToplevelState.MINIMIZED:
            self.window.set_visible(False)
            self._hold()

    def _check_service_stopping(self):
        if os.path.exists(os.path.join(self.data_dir, "service-stopping")):
            self.quit()
            return False
        return True

    def _notify(self, title, body):
        note = Gio.Notification.new(title)
        note.set_body(body)
        self.send_notification(None, note)
        return False

    def do_shutdown(self):
        if self.tray:
            self.tray.stop()
        from .service import clear_frontend
        clear_frontend(self.data_dir)
        Adw.Application.do_shutdown(self)


def _install_icon():
    """Put Card Farm's icon where the desktop looks for app icons (the menu entry and the window
    use it by name)."""
    icon = pkgutil.get_data("cardfarm", "ui/icon.svg")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dest = os.path.join(base, "icons", "hicolor", "scalable", "apps", f"{APP_ID}.svg")
    try:
        if not os.path.exists(dest) or open(dest, "rb").read() != icon:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(icon)
    except OSError:
        pass


def run_window(make_farm, data_dir, background=False):
    app = CardFarmApp(make_farm, data_dir, background)
    app.register(None)
    if app.get_is_remote():  # already running: show that one (or leave it be, for --background)
        if not background:
            app.activate()
        return 0
    return app.run(None)
