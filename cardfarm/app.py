"""Card Farm's desktop window (GTK 4 + libadwaita), showing the control panel in its own window.

One instance at a time: opening Card Farm again brings the window back. Closing the window while
farming keeps farming in the background; Quit (in the menu) stops everything."""
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gio, GLib, Gtk, WebKit  # noqa: E402

APP_ID = "io.github.cardfarm.CardFarm"


class CardFarmApp(Adw.Application):
    def __init__(self, start_farm, port):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.start_farm, self.port = start_farm, port
        self.farm = None
        self.window = self.view = None
        self.background = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.farm = self.start_farm()  # None when a background service already runs one
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *a: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        reload_action = Gio.SimpleAction.new("reload", None)
        reload_action.connect("activate", lambda *a: self.view and self.view.reload())
        self.add_action(reload_action)

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        self.window = Adw.ApplicationWindow(application=self, title="Card Farm", default_width=1100, default_height=820)
        self.window.connect("close-request", self._on_close)

        menu = Gio.Menu()
        menu.append("Reload", "app.reload")
        menu.append("Quit", "app.quit")
        header = Adw.HeaderBar()
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        self.view = WebKit.WebView()
        self.view.connect("decide-policy", self._stay_inside)
        self.view.connect("load-failed", self._retry_soon)
        self.view.load_uri(self._url())

        body = Adw.ToolbarView()
        body.add_top_bar(header)
        body.set_content(self.view)
        self.window.set_content(body)
        self.window.present()

    def _url(self):
        return f"http://127.0.0.1:{self.port}/?app=1"

    def _retry_soon(self, *a):
        GLib.timeout_add_seconds(2, lambda: self.view.load_uri(self._url()) and False)
        return True

    def _stay_inside(self, view, decision, kind):
        """Links that leave Card Farm open in the normal browser."""
        if kind != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        if uri.startswith(f"http://127.0.0.1:{self.port}/") or uri.startswith("about:"):
            return False
        Gtk.UriLauncher.new(uri).launch(self.window, None, None, None)
        decision.ignore()
        return True

    def _on_close(self, window):
        farming = self.farm and self.farm.session.signed_in and self.farm.settings["farming"]
        if not farming:
            return False  # closing the last window quits
        window.set_visible(False)
        if not self.background:
            self.hold()  # keep running with no window
            self.background = True
            note = Gio.Notification.new("Card Farm keeps farming")
            note.set_body("It's running in the background. Open Card Farm again to see it, or use Quit in its menu to stop.")
            self.send_notification("background", note)
        return True

    def do_shutdown(self):
        if self.farm:
            self.farm.idler.stop_all()
        Adw.Application.do_shutdown(self)


def run_window(start_farm, port):
    return CardFarmApp(start_farm, port).run(None)

