"""Small cross-platform tray icon for restoring or closing the desktop frontend."""


class FrontendTray:
    def __init__(self, show, quit):
        self.icon = None
        self.show = show
        self.quit = quit

    def start(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.line((32, 59, 32, 34), fill="#467b2a", width=5)
            draw.ellipse((20, 50, 31, 56), fill="#73a846")
            draw.ellipse((33, 42, 45, 48), fill="#73a846")
            for box, colour in (((15, 13, 31, 29), "#f6cf45"), ((33, 13, 49, 29), "#f291b5"),
                                ((24, 3, 40, 19), "#fbf7ee"), ((24, 23, 40, 39), "#c59df0")):
                draw.ellipse(box, fill=colour)
            draw.ellipse((27, 17, 37, 27), fill="#8a5a22")
            menu = pystray.Menu(
                pystray.MenuItem("Open Card Farmer's Market", lambda *_: self.show(), default=True),
                pystray.MenuItem("Quit frontend", lambda *_: self.quit()),
            )
            self.icon = pystray.Icon("card-farm", image, "Card Farmer's Market", menu)
            self.icon.run_detached()
        except Exception as e:
            # Headless sessions and desktops without a tray protocol can still use the window.
            print(f"System tray unavailable: {e}", flush=True)
            self.icon = None

    def stop(self):
        if self.icon:
            self.icon.stop()
            self.icon = None
