"""Card Farm's window (GTK 4 + libadwaita). The page inside is drawn by WebKit from Card Farm's own
files and talks to the farm in this same program through a private cardfarm:// address: nothing
listens on the network, and no browser can open it.

One copy at a time: opening Card Farm again brings its window back. Closing the window while
farming keeps farming in the background; Quit (in the menu) stops everything."""
import os
import shutil
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gio, GLib, Gtk, WebKit  # noqa: E402

from . import api  # noqa: E402

APP_ID = "io.github.cardfarm.CardFarm"
HOME = "cardfarm://app/"


class CardFarmApp(Adw.Application):
    def __init__(self, make_farm, background=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.make_farm, self.start_hidden = make_farm, background
        self.farm = None
        self.window = self.view = None
        self.held = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        _install_icon()
        Gtk.Window.set_default_icon_name(APP_ID)
        self.farm = self.make_farm()
        self.farm.notify = lambda title, body: GLib.idle_add(self._notify, title, body)
        threading.Thread(target=self.farm.run, daemon=True).start()
        WebKit.WebContext.get_default().register_uri_scheme("cardfarm", self._answer)
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
        self.window = Adw.ApplicationWindow(application=self, title="Card Farm", default_width=1100, default_height=820)
        self.window.connect("close-request", self._on_close)

        menu = Gio.Menu()
        menu.append("Reload", "app.reload")
        menu.append("Quit", "app.quit")
        header = Adw.HeaderBar()
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        self.view = WebKit.WebView()
        self.view.connect("decide-policy", self._stay_inside)
        self.view.connect("context-menu", lambda *a: True)  # no browser menu (Back, Inspect, ...)
        self.view.load_uri(HOME)

        body = Adw.ToolbarView()
        body.add_top_bar(header)
        body.set_content(self.view)
        self.window.set_content(body)
        self.window.present()

    def _answer(self, request):
        """A request from the page: read it here, answer it on a worker thread (the farm may take a
        moment), and hand the answer back on GTK's thread."""
        method, path = request.get_http_method() or "GET", request.get_path() or "/"
        body, stream = b"", request.get_http_body()
        while stream:
            chunk = stream.read_bytes(65536, None).get_data()
            if not chunk:
                break
            body += chunk

        def work():
            status, kind, data = api.answer(self.farm, method, path, body)
            GLib.idle_add(self._finish, request, status, kind, data)
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _finish(request, status, kind, data):
        response = WebKit.URISchemeResponse.new(Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data)), len(data))
        response.set_status(status, None)
        response.set_content_type(kind)
        request.finish_with_response(response)
        return False

    def _stay_inside(self, view, decision, kind):
        """Links that leave Card Farm open in the normal browser."""
        if kind != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        if uri.startswith(HOME) or uri.startswith("about:"):
            return False
        Gtk.UriLauncher.new(uri).launch(self.window, None, None, None)
        decision.ignore()
        return True

    def _hold(self):
        if not self.held:
            self.hold()  # keep running with no window
            self.held = True

    def _on_close(self, window):
        farming = self.farm.session.signed_in and (self.farm.settings["farming"] or self.farm.state["queue"])
        if not farming:
            self.quit()
            return False
        window.set_visible(False)
        if not self.held:
            self._hold()
            self._notify("Card Farm keeps farming", "It's running in the background. Open Card Farm again "
                                                    "to see it, or use Quit in its menu to stop.")
        return True

    def _notify(self, title, body):
        note = Gio.Notification.new(title)
        note.set_body(body)
        self.send_notification(None, note)
        return False

    def do_shutdown(self):
        if self.farm:
            self.farm.idler.stop_all()
            self.farm.save()
        Adw.Application.do_shutdown(self)


def _install_icon():
    """Put Card Farm's icon where the desktop looks for app icons (the menu entry and the window
    use it by name)."""
    src = os.path.join(os.path.dirname(__file__), "ui", "icon.svg")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dest = os.path.join(base, "icons", "hicolor", "scalable", "apps", f"{APP_ID}.svg")
    try:
        if not os.path.exists(dest) or open(dest, "rb").read() != open(src, "rb").read():
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest)
    except OSError:
        pass


def run_window(make_farm, background=False):
    app = CardFarmApp(make_farm, background)
    app.register(None)
    if app.get_is_remote():  # already running: show that one (or leave it be, for --background)
        if not background:
            app.activate()
        return 0
    return app.run(None)
