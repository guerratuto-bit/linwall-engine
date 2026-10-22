# =============================================================================
#  LinWall Engine v1.6.1 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Aplicación Gtk.Application de LinWall Engine."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_STDERR_FILTER_INSTALLED = False


def install_gtk_stderr_filter() -> None:
    """Filtra a nivel de descriptor C (fd 2) los avisos del tema GTK3 del sistema
    (``Theme parsing error: gtk.css`` y ``Gtk-WARNING``) para mantener la terminal 100% limpia.
    """
    global _STDERR_FILTER_INSTALLED
    if _STDERR_FILTER_INSTALLED:
        return
    _STDERR_FILTER_INSTALLED = True
    try:
        orig_fd = os.dup(2)
        r_fd, w_fd = os.pipe()
        os.dup2(w_fd, 2)
        os.close(w_fd)
        sys.stderr = os.fdopen(
            2, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False
        )
        noise_tokens = (
            "Theme parsing error:",
            "gtk.css:",
            "Gtk-WARNING",
            "Gdk-WARNING",
            "Atk-WARNING",
            "dbind-WARNING",
            "AT-SPI:",
        )

        def _pump() -> None:
            try:
                with os.fdopen(r_fd, "r", encoding="utf-8", errors="replace") as reader:
                    for line in reader:
                        if any(tok in line for tok in noise_tokens):
                            continue
                        try:
                            os.write(orig_fd, line.encode("utf-8", errors="replace"))
                        except OSError:
                            break
            except OSError:
                pass

        threading.Thread(target=_pump, name="linwall-stderr-filter", daemon=True).start()
    except OSError:
        pass


install_gtk_stderr_filter()

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from linwall import __app_id__, __app_name__, __version__, paths  # noqa: E402
from linwall.config import Config  # noqa: E402
from linwall.gui.window import BANNER_FILE, ICON_FILE, LinWallWindow  # noqa: E402

CSS_FILE = Path(__file__).with_name("style.css")


class LinWallSplash(Gtk.ApplicationWindow):
    """Pantalla de inicio (Splash Screen) con el banner oficial, barra de progreso
    y consola en vivo de 2 líneas mostrando el código en ejecución.
    """

    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title=f"{__app_name__} v{__version__}")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_type_hint(Gdk.WindowTypeHint.SPLASHSCREEN)
        self.set_keep_above(True)
        self.set_default_size(720, -1)
        self.get_style_context().add_class("linwall-splash")

        if ICON_FILE.is_file():
            try:
                self.set_icon_from_file(str(ICON_FILE))
            except GLib.Error:
                pass

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.get_style_context().add_class("splash-card")

        if BANNER_FILE.is_file():
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(BANNER_FILE), 720, 402, True)
                outer.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            except GLib.Error:
                pass

        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end"):
            getattr(bottom, f"set_margin_{side}")(22)
        bottom.set_margin_top(12)
        bottom.set_margin_bottom(16)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_lbl = Gtk.Label(
            label=f"Iniciando {__app_name__} v{__version__}…",
            xalign=0,
        )
        self.status_lbl.get_style_context().add_class("splash-status")
        self.percent_lbl = Gtk.Label(label="0%", xalign=1)
        self.percent_lbl.get_style_context().add_class("splash-percent")
        header_row.pack_start(self.status_lbl, True, True, 0)
        header_row.pack_end(self.percent_lbl, False, False, 0)
        bottom.pack_start(header_row, False, False, 0)

        self.progress = Gtk.ProgressBar()
        self.progress.get_style_context().add_class("splash-progress")
        self.progress.set_fraction(0.05)
        bottom.pack_start(self.progress, False, False, 0)

        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        log_box.get_style_context().add_class("splash-log-box")
        self.line1 = Gtk.Label(
            label="> [linwall.__main__] main() -> iniciando cliente GTK 3 (X11)…",
            xalign=0,
        )
        self.line1.set_ellipsize(Pango.EllipsizeMode.END)
        self.line1.set_max_width_chars(88)
        self.line1.get_style_context().add_class("splash-log-line1")

        self.line2 = Gtk.Label(
            label="> [linwall.config] Config.load() <- ~/.config/linwall/config.json",
            xalign=0,
        )
        self.line2.set_ellipsize(Pango.EllipsizeMode.END)
        self.line2.set_max_width_chars(88)
        self.line2.get_style_context().add_class("splash-log-line2")

        log_box.pack_start(self.line1, False, False, 0)
        log_box.pack_start(self.line2, False, False, 0)
        bottom.pack_start(log_box, False, False, 0)

        outer.pack_start(bottom, False, False, 0)
        self.add(outer)

    def update_step(self, fraction: float, status: str, log1: str, log2: str) -> None:
        clamped = max(0.0, min(1.0, fraction))
        self.progress.set_fraction(clamped)
        self.percent_lbl.set_text(f"{int(round(clamped * 100))}%")
        self.status_lbl.set_text(status)
        self.line1.set_text(log1)
        self.line2.set_text(log2)


class LinWallApp(Gtk.Application):
    def __init__(self, cfg: Config):
        super().__init__(application_id=__app_id__, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.cfg = cfg
        self.window: LinWallWindow | None = None
        self.splash: LinWallSplash | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.connect("parsing-error", lambda *_args: True)
        try:
            provider.load_from_data(CSS_FILE.read_bytes())
        except Exception:  # noqa: BLE001
            pass
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
        if ICON_FILE.is_file():
            try:
                Gtk.Window.set_default_icon_from_file(str(ICON_FILE))
            except GLib.Error:
                pass

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def do_activate(self) -> None:
        if self.window is not None:
            self.window.present()
            return

        self.splash = LinWallSplash(self)
        self.splash.show_all()
        self.splash.present()
        self._run_startup_sequence()

    def _run_startup_sequence(self) -> None:
        step_idx = 0

        def _tail_engine_log() -> str:
            try:
                if paths.ENGINE_LOG.is_file():
                    lines = [
                        ln.strip()
                        for ln in paths.ENGINE_LOG.read_text(errors="replace").splitlines()
                        if ln.strip()
                    ]
                    if lines:
                        return lines[-1][:95]
            except OSError:
                pass
            return "sin eventos previos en engine.log"

        def _advance() -> bool:
            nonlocal step_idx
            step_idx += 1
            if self.splash is None:
                return False

            if step_idx == 1:
                self.splash.update_step(
                    0.22,
                    "Cargando configuración y entorno X11…",
                    "> [linwall.paths] ensure_dirs() -> inicializando XDG_STATE_HOME y runtime IPC",
                    "> [linwall.monitors] list_monitors() -> ejecutando xrandr --listmonitors",
                )
                return True

            if step_idx == 2:
                self.window = LinWallWindow(self, self.cfg)
                bins = self.window.engine.binaries()
                ok_bins = ", ".join(k for k, v in bins.items() if v) or "verificando"
                self.splash.update_step(
                    0.50,
                    "Verificando motores nativos (mpv / xwinwrap / linux-wallpaperengine)…",
                    f"> [linwall.engine] Engine.binaries() -> activos: {ok_bins}",
                    "> [linwall.paths] find_wallpaper_engine_assets_dir() -> vinculando assets de Steam",
                )
                return True

            if step_idx == 3:
                self.splash.update_step(
                    0.78,
                    "Escaneando biblioteca de Steam Workshop (AppID 431960)…",
                    "> [linwall.scanner] scan(431960) -> procesando project.json y scene.pkg…",
                    f"> [engine.log] {_tail_engine_log()}",
                )
                return True

            if step_idx == 4:
                n_cards = len(self.window.cards) if self.window else 0
                self.splash.update_step(
                    1.0,
                    "¡LinWall Engine listo!",
                    f"> [linwall.gui.window] _populate() -> {n_cards} fondos cargados en cuadrícula FlowBox",
                    "> [linwall.gui.app] LinWallWindow.present() -> abriendo interfaz principal…",
                )
                return True

            if self.window is not None:
                self.window.show_all()
                self.window.present()
            if self.splash is not None:
                self.splash.destroy()
                self.splash = None
            return False

        GLib.timeout_add(340, _advance)


def run_gui(cfg: Config) -> int:
    install_gtk_stderr_filter()
    GLib.set_application_name(__app_name__)
    GLib.set_prgname("linwall")
    return LinWallApp(cfg).run([sys.argv[0]])
