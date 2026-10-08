import atexit
import tkinter as tk
from tkinter import messagebox

from . import config
from .files import sweep, wipe_incognito
from .ui import App


def main() -> None:
    cfg = config.load()
    root = tk.Tk()
    try:
        app = App(root, cfg)
    except OSError as e:
        root.withdraw()
        messagebox.showerror(
            "Whisper",
            f"Cannot listen on port {cfg['port']}.\n\nWhisper is probably already "
            f"running on this computer.\n\n({e})")
        return
    # Only now is it certain no other instance owns the files directory.
    sweep()
    # Covers exits that bypass the window's close button (e.g. Ctrl+C).
    atexit.register(wipe_incognito)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.node.stop()
        app.session.close()


if __name__ == "__main__":
    main()
