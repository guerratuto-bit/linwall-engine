# =============================================================================
#  LinWall Engine v1.6.1 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Ventana principal de LinWall Engine (GTK 3)."""

from __future__ import annotations

import logging
import random
import subprocess
import threading
import time
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

from linwall import (  # noqa: E402
    __ai_credits__,
    __app_name__,
    __author__,
    __country__,
    __email__,
    __github_url__,
    __license__,
    __version__,
    __youtube_url__,
    autostart,
    paths,
)
from linwall.config import Config  # noqa: E402
from linwall.desktop import desktop_summary  # noqa: E402
from linwall.engine import Engine, EngineError, PlaybackOptions, load_state  # noqa: E402
from linwall.gui.card import THUMB_H, THUMB_W, WallpaperCard  # noqa: E402
from linwall.gui.thumbs import load_wallpaper_cover  # noqa: E402
from linwall.monitors import list_monitors  # noqa: E402
from linwall.scanner import Wallpaper, find_workshop_dirs, scan  # noqa: E402

log = logging.getLogger(__name__)

PREVIEW_W, PREVIEW_H = 300, 169
_PKG_DIR = Path(__file__).resolve().parent.parent
LOGO_FILE = _PKG_DIR / "logo.jpeg"
BANNER_FILE = _PKG_DIR / "banner.jpeg"
ICON_PNG_FILE = Path(__file__).with_name("linwall.png")
ICON_SVG_FILE = Path(__file__).with_name("linwall.svg")
ICON_FILE = LOGO_FILE if LOGO_FILE.is_file() else (ICON_PNG_FILE if ICON_PNG_FILE.is_file() else ICON_SVG_FILE)
TYPE_FILTERS = (("all", "Todos los tipos"), ("video", "Video"), ("scene", "Scene"), ("web", "Web"))
LAYER_MODES = (
    ("auto", "Auto (recomendado)"),
    ("desktop", "Ventana de escritorio (-fdt)"),
    ("root", "Ventana raíz (override-redirect)"),
)
WP_ENGINE_MODES = (
    ("auto", "Auto (Nativo Scene / Video)"),
    ("fix_scene", "Fix Capas Scene (Desempaquetar y limpiar)"),
    ("compat", "Modo Compatibilidad (Forzar Loop Animado / Fix Capas)"),
)


def _idle(cb: Callable, *args: Any) -> None:
    def wrapper() -> bool:
        cb(*args)
        return False
    GLib.idle_add(wrapper)


class LinWallWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, cfg: Config):
        super().__init__(application=app, title=f"{__app_name__} v{__version__}")
        self.cfg = cfg
        self.engine = Engine()
        self.cards: dict[str, WallpaperCard] = {}
        self.selected: Wallpaper | None = None
        self.active_id: str | None = self.engine.active_wallpaper_id()
        self._busy = False
        self._smart_pause_busy = False
        self._last_smart_paused = False
        self._auto_mute_busy = False
        self._last_temp_muted = False
        self._loading_ui = True
        self._timers: dict[str, int] = {}
        self._scan_gen = 0
        self._apply_lock = threading.Lock()
        self._apply_seq = 0
        self._applying_wp_id: str | None = None
        self._last_apply_ts: float = 0.0
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="linwall-thumb")

        self.set_default_size(1320, 800)
        self.get_style_context().add_class("linwall-window")
        if ICON_FILE.is_file():
            try:
                self.set_icon_from_file(str(ICON_FILE))
            except GLib.Error:
                self.set_icon_name("linwall")

        self._build_header()
        self._build_body()
        self.connect("destroy", self._on_destroy)

        self._refresh_monitors()
        self._refresh_engine_info()
        self._loading_ui = False
        self.rescan()
        GLib.timeout_add_seconds(4, self._poll_engine)
        GLib.timeout_add_seconds(2, self._poll_smart_pause)
        GLib.timeout_add(1000, self._poll_window_monitor)

    # ================================================================ UI
    def _build_header(self) -> None:
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(f"{__app_name__} v{__version__}")
        hb.set_subtitle(f"Fondos de Wallpaper Engine en Linux · por {__author__} ({__country__})")

        if ICON_FILE.is_file():
            try:
                logo_pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(ICON_FILE), 28, 28, True)
                logo_img = Gtk.Image.new_from_pixbuf(logo_pb)
                logo_img.set_tooltip_text(f"{__app_name__} v{__version__}")
                hb.pack_start(logo_img)
            except GLib.Error:
                pass

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Buscar por nombre o ID…")
        self.search.set_width_chars(28)
        self.search.connect("search-changed", self._on_filter_changed)
        hb.pack_start(self.search)

        self.type_combo = Gtk.ComboBoxText()
        for tid, label in TYPE_FILTERS:
            self.type_combo.append(tid, label)
        self.type_combo.set_active_id(self.cfg.get("filter_type") or "all")
        if self.type_combo.get_active_id() is None:
            self.type_combo.set_active_id("all")
        self.type_combo.connect("changed", self._on_filter_changed)
        hb.pack_start(self.type_combo)

        self.random_btn = Gtk.Button(label="🎲 Aleatorio")
        self.random_btn.get_style_context().add_class("random-header-btn")
        self.random_btn.set_tooltip_text("Elegir y aplicar un fondo al azar de la biblioteca con un clic")
        self.random_btn.connect("clicked", self._on_random_wallpaper)
        hb.pack_start(self.random_btn)

        about_btn = Gtk.Button(label="ⓘ Créditos / Acerca de")
        about_btn.get_style_context().add_class("about-header-btn")
        about_btn.set_tooltip_text(f"Acerca de {__app_name__} v{__version__} — {__author__} ({__country__})")
        about_btn.connect("clicked", self._on_show_about)
        hb.pack_end(about_btn)

        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Volver a escanear Steam Workshop")
        refresh.connect("clicked", lambda *_: self.rescan())
        hb.pack_end(refresh)

        add = Gtk.Button.new_from_icon_name("folder-new-symbolic", Gtk.IconSize.BUTTON)
        add.set_tooltip_text("Añadir carpeta de proyectos adicional")
        add.connect("clicked", self._on_add_folder)
        hb.pack_end(add)

        self.set_titlebar(hb)

    def _build_body(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.infobar = Gtk.InfoBar()
        self.infobar.get_style_context().add_class("linwall-infobar")
        self.infobar.set_show_close_button(True)
        self.info_label = Gtk.Label(xalign=0)
        self.info_label.set_line_wrap(True)
        self.info_label.set_selectable(True)
        self.infobar.get_content_area().add(self.info_label)
        self.infobar.connect("response", lambda bar, *_: bar.set_revealed(False))
        self.infobar.set_revealed(False)
        root.pack_start(self.infobar, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.get_style_context().add_class("main-area")

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.flow = Gtk.FlowBox()
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.flow.set_homogeneous(True)
        self.flow.set_min_children_per_line(1)
        self.flow.set_max_children_per_line(12)
        self.flow.set_activate_on_single_click(False)
        self.flow.set_row_spacing(2)
        self.flow.set_column_spacing(2)
        for side in ("start", "end", "top", "bottom"):
            getattr(self.flow, f"set_margin_{side}")(10)
        self.flow.set_filter_func(self._filter_func)
        self.flow.set_sort_func(self._sort_func)
        self.flow.connect("selected-children-changed", self._on_selection_changed)
        self.flow.connect("child-activated", lambda _f, child: self._apply(child.wp))
        scrolled.add(self.flow)
        self.stack.add_named(scrolled, "grid")

        self.stack.add_named(self._build_loading(), "loading")
        self.stack.add_named(self._build_empty(), "empty")

        body.pack_start(self.stack, True, True, 0)
        body.pack_start(self._build_sidebar(), False, False, 0)
        root.pack_start(body, True, True, 0)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status.get_style_context().add_class("status-bar")
        self.spinner = Gtk.Spinner()
        self.status_label = Gtk.Label(label="Listo", xalign=0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.count_label = Gtk.Label(label="")
        status.pack_start(self.spinner, False, False, 0)
        status.pack_start(self.status_label, True, True, 0)
        status.pack_end(self.count_label, False, False, 0)
        root.pack_start(status, False, False, 0)

        self.add(root)

    def _build_loading(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_valign(Gtk.Align.CENTER)
        outer.set_halign(Gtk.Align.CENTER)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class("splash-card")
        card.set_size_request(620, -1)

        if BANNER_FILE.is_file():
            try:
                bpb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(BANNER_FILE), 620, 346, True)
                card.pack_start(Gtk.Image.new_from_pixbuf(bpb), False, False, 0)
            except GLib.Error:
                pass

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end", "bottom"):
            getattr(info_box, f"set_margin_{side}")(18)
        info_box.set_margin_top(6)

        self.loading_bar = Gtk.ProgressBar()
        self.loading_bar.get_style_context().add_class("splash-progress")
        self.loading_bar.set_show_text(True)
        self.loading_bar.set_text("Escaneando biblioteca de Steam Workshop (431960)…")
        self.loading_bar.set_fraction(0.35)
        info_box.pack_start(self.loading_bar, False, False, 0)

        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        log_box.get_style_context().add_class("splash-log-box")
        self.loading_log_line1 = Gtk.Label(
            label="> [linwall.scanner] find_workshop_dirs() -> buscando contenido AppID 431960…",
            xalign=0,
        )
        self.loading_log_line1.set_ellipsize(Pango.EllipsizeMode.END)
        self.loading_log_line1.get_style_context().add_class("splash-log-line1")
        self.loading_log_line2 = Gtk.Label(
            label="> [linwall.scanner] scan() -> leyendo project.json y scene.pkg…",
            xalign=0,
        )
        self.loading_log_line2.set_ellipsize(Pango.EllipsizeMode.END)
        self.loading_log_line2.get_style_context().add_class("splash-log-line2")
        log_box.pack_start(self.loading_log_line1, False, False, 0)
        log_box.pack_start(self.loading_log_line2, False, False, 0)
        info_box.pack_start(log_box, False, False, 0)

        card.pack_start(info_box, False, False, 0)
        outer.pack_start(card, False, False, 0)
        return outer

    def _build_empty(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("folder-pictures-symbolic", Gtk.IconSize.DIALOG)
        icon.set_pixel_size(96)
        title = Gtk.Label(label="No se encontraron fondos de Wallpaper Engine")
        title.get_style_context().add_class("empty-title")
        self.empty_label = Gtk.Label()
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.empty_label.set_line_wrap(True)
        self.empty_label.set_max_width_chars(80)
        self.empty_label.get_style_context().add_class("dim")
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        add = Gtk.Button(label="Añadir carpeta…")
        add.connect("clicked", self._on_add_folder)
        again = Gtk.Button(label="Volver a escanear")
        again.connect("clicked", lambda *_: self.rescan())
        buttons.pack_start(add, False, False, 0)
        buttons.pack_start(again, False, False, 0)
        for w in (icon, title, self.empty_label, buttons):
            box.pack_start(w, False, False, 0)
        return box

    @staticmethod
    def _section(text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.get_style_context().add_class("section-title")
        return lbl

    @staticmethod
    def _row(label: str, widget: Gtk.Widget, tooltip: str | None = None) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_line_wrap(True)
        lbl.set_hexpand(True)
        widget.set_valign(Gtk.Align.CENTER)
        box.pack_start(lbl, True, True, 0)
        box.pack_end(widget, False, False, 0)
        if tooltip:
            box.set_tooltip_text(tooltip)
        return box

    def _build_sidebar(self) -> Gtk.Widget:
        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.set_size_request(PREVIEW_W + 34, -1)
        outer.get_style_context().add_class("sidebar")

        sb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sb.get_style_context().add_class("sidebar")
        for side in ("start", "end", "top", "bottom"):
            getattr(sb, f"set_margin_{side}")(14)

        self.preview = Gtk.Image()
        self.preview.set_size_request(PREVIEW_W, PREVIEW_H)
        self.preview.get_style_context().add_class("preview-frame")
        self.preview.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
        sb.pack_start(self.preview, False, False, 0)

        self.title_label = Gtk.Label(label="Selecciona un fondo", xalign=0)
        self.title_label.set_line_wrap(True)
        self.title_label.get_style_context().add_class("wp-big-title")
        self.meta_label = Gtk.Label(xalign=0)
        self.meta_label.get_style_context().add_class("dim")
        self.path_label = Gtk.Label(xalign=0)
        self.path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.path_label.set_selectable(True)
        self.path_label.get_style_context().add_class("dim")
        for w in (self.title_label, self.meta_label, self.path_label):
            sb.pack_start(w, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
        self.apply_btn = Gtk.Button(label="▶  Aplicar fondo")
        self.apply_btn.get_style_context().add_class("apply-btn")
        self.apply_btn.connect("clicked", lambda *_: self._apply())
        self.apply_btn.set_sensitive(False)
        self.stop_btn = Gtk.Button(label="■  Detener fondo")
        self.stop_btn.get_style_context().add_class("stop-btn")
        self.stop_btn.connect("clicked", lambda *_: self._stop())
        actions.pack_start(self.apply_btn, True, True, 0)
        actions.pack_start(self.stop_btn, True, True, 0)
        sb.pack_start(actions, False, False, 6)

        # ---------------- audio
        sb.pack_start(self._section("AUDIO"), False, False, 0)
        audio = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.mute_btn = Gtk.ToggleButton()
        self.mute_btn.set_active(bool(self.cfg.get("muted")))
        self.mute_btn.set_tooltip_text("Silenciar / activar sonido (se aplica en vivo)")
        self.mute_btn.connect("toggled", self._on_mute_toggled)
        self._update_mute_icon()
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_value(int(self.cfg.get("volume", 50)))
        self.volume.set_digits(0)
        self.volume.set_value_pos(Gtk.PositionType.RIGHT)
        self.volume.set_hexpand(True)
        self.volume.connect("value-changed", self._on_volume_slider_changed)
        audio.pack_start(self.mute_btn, False, False, 0)
        audio.pack_start(self.volume, True, True, 0)
        sb.pack_start(audio, False, False, 0)

        self.auto_mute_check = Gtk.CheckButton(
            label="🔇 Silenciar automáticamente al desenfocar el escritorio"
        )
        self.auto_mute_check.get_style_context().add_class("auto-mute-check")
        self.auto_mute_check.set_active(bool(self.cfg.get("auto_mute_on_unfocus", True)))
        self.auto_mute_check.set_tooltip_text(
            "Silencia dinámicamente el fondo cuando usas otra aplicación (ej. Chromium, VSCode) "
            "para liberar el reloj de PulseAudio/PipeWire y evitar desincronización de video, "
            "restaurando el sonido al volver al escritorio."
        )
        if self.auto_mute_check.get_child() is not None:
            self.auto_mute_check.get_child().set_line_wrap(True)
        self.auto_mute_check.connect("toggled", self._on_auto_mute_toggled)
        sb.pack_start(self.auto_mute_check, False, False, 2)

        # ---------------- rendimiento / pantalla
        sb.pack_start(self._section("RENDIMIENTO Y PANTALLA"), False, False, 0)
        self.fps_combo = Gtk.ComboBoxText()
        self.fps_combo.append("30", "30 FPS (ahorro)")
        self.fps_combo.append("60", "60 FPS (fluido)")
        self.fps_combo.set_active_id(str(self.cfg.get("fps", 30)))
        self.fps_combo.connect("changed", self._on_render_setting_changed, "fps")
        sb.pack_start(self._row("FPS", self.fps_combo), False, False, 0)

        self.pause_fs_switch = Gtk.Switch()
        self.pause_fs_switch.set_active(bool(self.cfg.get("pause_on_fullscreen", True)))
        self.pause_fs_switch.connect("notify::active", self._on_pause_fullscreen_changed)
        sb.pack_start(self._row(
            "⚡ Pausar fondo al usar otra app en pantalla completa/maximizada",
            self.pause_fs_switch,
            "Verifica cada 2 segundos si hay otra ventana maximizada o en pantalla completa y "
            "congela el motor con SIGSTOP (0% uso de GPU/CPU), reanudándolo con SIGCONT al minimizarla.",
        ), False, False, 0)

        mon_box = Gtk.Box(spacing=4)
        self.monitor_combo = Gtk.ComboBoxText()
        self.monitor_combo.connect("changed", self._on_render_setting_changed, "monitor")
        mon_refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        mon_refresh.set_tooltip_text("Detectar monitores de nuevo (xrandr --listmonitors)")
        mon_refresh.connect("clicked", lambda *_: self._refresh_monitors())
        mon_box.pack_start(self.monitor_combo, False, False, 0)
        mon_box.pack_start(mon_refresh, False, False, 0)
        sb.pack_start(self._row("Monitor", mon_box), False, False, 0)

        self.layer_combo = Gtk.ComboBoxText()
        for lid, label in LAYER_MODES:
            self.layer_combo.append(lid, label)
        self.layer_combo.set_active_id(self.cfg.get("layer_mode") or "auto")
        self.layer_combo.connect("changed", self._on_render_setting_changed, "layer_mode")
        sb.pack_start(self._row(
            "Capa", self.layer_combo,
            "Auto: ventana tipo DESKTOP en Cinnamon/XFCE/MATE y ventana raíz en gestores "
            "de ventanas mínimos (i3, Openbox…)."), False, False, 0)

        self.wp_engine_combo = Gtk.ComboBoxText()
        for mid, mlabel in WP_ENGINE_MODES:
            self.wp_engine_combo.append(mid, mlabel)
        self.wp_engine_combo.set_active_id("auto")
        self.wp_engine_combo.connect("changed", self._on_wp_engine_mode_changed)
        sb.pack_start(self._row(
            "Motor de este fondo",
            self.wp_engine_combo,
            "Si un fondo SCENE muestra capas fantasma o sprites ocultos en OpenGL (ej. Yuta Okkotsu), "
            "usa «Fix Capas Scene (Desempaquetar y limpiar)» para desempaquetar scene.pkg en caché y "
            "eliminar objetos ocultos en alta definición, o «Modo Compatibilidad» para forzar mpv.",
        ), False, False, 0)

        # ---------------- modo de escritorio (tiempo real)
        sb.pack_start(self._section("MODO DE ESCRITORIO"), False, False, 0)
        mode_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mode_box.get_style_context().add_class("mode-box")

        self.show_icons_btn = Gtk.Button(label="🗂️ Mostrar Iconos")
        self.show_icons_btn.set_tooltip_text(
            "Mantiene los iconos del escritorio y la barra de tareas visibles sobre el fondo "
            "animado (cambio instantáneo en tiempo real)."
        )
        self.show_icons_btn.connect("clicked", lambda *_: self._on_desktop_mode_clicked(False))

        self.immersive_btn = Gtk.Button(label="✨ Modo Inmersivo (Sin Iconos)")
        self.immersive_btn.set_tooltip_text(
            "Oculta limpiamente los iconos del escritorio para disfrutar del arte del fondo al 100%, "
            "manteniendo siempre visible la barra de tareas inferior (cambio instantáneo en tiempo real)."
        )
        self.immersive_btn.connect("clicked", lambda *_: self._on_desktop_mode_clicked(True))

        mode_box.pack_start(self.show_icons_btn, False, False, 0)
        mode_box.pack_start(self.immersive_btn, False, False, 0)
        sb.pack_start(mode_box, False, False, 2)
        self._update_desktop_mode_buttons(bool(self.cfg.get("hide_desktop_icons")))

        self.autostart_switch = Gtk.Switch()
        self.autostart_switch.set_active(bool(self.cfg.get("autostart")))
        self.autostart_switch.connect("notify::active", self._on_autostart_changed)
        sb.pack_start(self._row(
            "Iniciar con el sistema", self.autostart_switch,
            f"Crea {paths.AUTOSTART_FILE} para restaurar el último fondo al iniciar sesión "
            "sin abrir esta ventana."), False, False, 0)

        # ---------------- motores
        sb.pack_start(self._section("MOTORES"), False, False, 0)
        self.engines_label = Gtk.Label(xalign=0)
        self.engines_label.set_line_wrap(True)
        sb.pack_start(self.engines_label, False, False, 0)
        log_btn = Gtk.Button(label="Abrir log del motor")
        log_btn.connect("clicked", self._on_open_log)
        sb.pack_start(log_btn, False, False, 4)

        about_btn = Gtk.Button(label="ⓘ  Créditos / Acerca de")
        about_btn.connect("clicked", self._on_show_about)
        sb.pack_start(about_btn, False, False, 2)

        about = Gtk.Label(
            label=f"{__app_name__} v{__version__} · {__license__}\nPor {__author__} ({__country__})",
            xalign=0,
        )
        about.get_style_context().add_class("dim")
        sb.pack_end(about, False, False, 0)

        outer.add(sb)
        return outer

    # ========================================================= utilidades
    def _run_bg(self, fn: Callable[[], Any], on_ok: Callable[[Any], None],
                on_err: Callable[[BaseException], None]) -> None:
        def worker() -> None:
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - se muestra al usuario
                log.exception("Error en tarea en segundo plano")
                _idle(on_err, exc)
            else:
                _idle(on_ok, result)
        threading.Thread(target=worker, daemon=True).start()

    def _debounce(self, key: str, ms: int, fn: Callable[[], None]) -> None:
        old = self._timers.pop(key, None)
        if old:
            GLib.source_remove(old)

        def fire() -> bool:
            self._timers.pop(key, None)
            fn()
            return False
        self._timers[key] = GLib.timeout_add(ms, fire)

    def _set_status(self, text: str, busy: bool = False) -> None:
        self.status_label.set_text(text)
        self.status_label.set_tooltip_text(text)
        (self.spinner.start if busy else self.spinner.stop)()

    def _show_info(
        self,
        text: str,
        kind: Gtk.MessageType = Gtk.MessageType.INFO,
        auto_hide_ms: int = 4000,
    ) -> None:
        if not text:
            return
        self.infobar.set_message_type(kind)
        self.info_label.set_text(text)
        self.infobar.set_revealed(True)
        if auto_hide_ms > 0:
            self._debounce("hide-infobar", auto_hide_ms, lambda: self.infobar.set_revealed(False))

    def _error_dialog(self, title: str, text: str) -> None:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.CLOSE, text=title)
        dlg.format_secondary_text(text)
        dlg.run()
        dlg.destroy()

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        if text:
            self._set_status(text, busy)
        elif not busy:
            self.spinner.stop()
        self._update_buttons()

    # ============================================================ escaneo
    def rescan(self) -> None:
        self._scan_gen += 1
        gen = self._scan_gen
        extra = list(self.cfg.get("extra_dirs") or [])
        self.stack.set_visible_child_name("loading")
        if hasattr(self, "loading_bar"):
            self.loading_bar.set_fraction(0.45)
            self.loading_bar.set_text("Escaneando biblioteca de Steam Workshop (431960)…")
            self.loading_log_line1.set_text(
                "> [linwall.scanner] find_workshop_dirs() -> buscando contenido AppID 431960…"
            )
            self.loading_log_line2.set_text(
                "> [linwall.scanner] scan() -> leyendo project.json y scene.pkg en segundo plano…"
            )
        self._set_status("Escaneando Steam Workshop…", busy=True)
        self._run_bg(lambda: (find_workshop_dirs(extra), scan(extra)),
                     lambda res: self._populate(gen, *res),
                     lambda exc: self._set_status(f"Error al escanear: {exc}"))

    def _populate(self, gen: int, dirs: list[Path], wallpapers: list[Wallpaper]) -> None:
        if gen != self._scan_gen:
            return
        if hasattr(self, "loading_bar"):
            self.loading_bar.set_fraction(1.0)
            self.loading_log_line1.set_text(
                f"> [linwall.scanner] scan() completado -> {len(wallpapers)} fondos detectados"
            )
            self.loading_log_line2.set_text(
                "> [linwall.gui.window] _populate() -> renderizando cuadrícula FlowBox y miniaturas"
            )
        for child in self.flow.get_children():
            self.flow.remove(child)
            child.destroy()
        self.cards.clear()

        for wp in wallpapers:
            card = WallpaperCard(wp)
            card.connect("button-press-event", self._on_card_button)
            self.flow.add(card)
            card.show_all()
            self.cards[wp.id] = card
            fut = self._executor.submit(load_wallpaper_cover, wp, THUMB_W, THUMB_H)
            fut.add_done_callback(lambda f, c=card, g=gen: _idle(self._set_thumb, c, g, f))

        self._mark_active()
        self._update_count()

        if wallpapers:
            self.stack.set_visible_child_name("grid")
            where = ", ".join(str(d) for d in dirs) or "carpetas extra"
            self._set_status(f"{len(wallpapers)} fondos encontrados en {where}")
            last = self.cfg.get("last_wallpaper") or {}
            target = self.cards.get(str(last.get("id"))) if isinstance(last, dict) else None
            if target:
                self.flow.select_child(target)
        else:
            searched = "\n".join(str(p) for p in paths.DEFAULT_WORKSHOP_DIRS[:3])
            self.empty_label.set_text(
                "Se buscó la carpeta del Workshop (AppID 431960) en:\n\n" + searched +
                "\n\nSuscríbete a fondos en el Workshop de Wallpaper Engine desde Steam "
                "(Wallpaper Engine puede instalarse con Proton) o añade una carpeta manualmente.")
            self.stack.set_visible_child_name("empty")
            self._set_status("No se encontraron fondos")

    def _set_thumb(self, card: WallpaperCard, gen: int, fut: Future) -> None:
        if gen != self._scan_gen or fut.cancelled():
            return
        try:
            card.set_pixbuf(fut.result())
        except Exception:  # noqa: BLE001
            pass

    # ======================================================= filtro/orden
    def _filter_func(self, child: WallpaperCard) -> bool:
        return child.matches(self.search.get_text().strip(), self.type_combo.get_active_id() or "all")

    @staticmethod
    def _sort_func(a: WallpaperCard, b: WallpaperCard) -> int:
        ka, kb = a.wp.title.casefold(), b.wp.title.casefold()
        return (ka > kb) - (ka < kb)

    def _on_filter_changed(self, *_: Any) -> None:
        self.flow.invalidate_filter()
        self._update_count()
        if not self._loading_ui:
            self.cfg.set("filter_type", self.type_combo.get_active_id() or "all")
            self._debounce("save", 500, self.cfg.save)

    def _update_count(self) -> None:
        q = self.search.get_text().strip()
        t = self.type_combo.get_active_id() or "all"
        visible = sum(1 for c in self.cards.values() if c.matches(q, t))
        self.count_label.set_text(f"{visible} / {len(self.cards)} fondos")

    def _on_random_wallpaper(self, *_: Any) -> None:
        """Elige un fondo aleatorio soportado de la biblioteca y lo aplica con un clic."""
        if not self.cards:
            return
        q = self.search.get_text().strip()
        t = self.type_combo.get_active_id() or "all"
        pool = [
            c for c in self.cards.values()
            if c.wp.is_supported and self._missing_engine_for(c.wp) is None and c.matches(q, t)
        ]
        if not pool:
            pool = [
                c for c in self.cards.values()
                if c.wp.is_supported and self._missing_engine_for(c.wp) is None
            ]
        if not pool:
            return
        if len(pool) > 1 and self.active_id:
            others = [c for c in pool if c.wp.id != self.active_id]
            if others:
                pool = others
        chosen = random.choice(pool)
        self.flow.select_child(chosen)
        self._apply(chosen.wp)

    # ========================================================== selección
    def _on_selection_changed(self, flow: Gtk.FlowBox) -> None:
        children = flow.get_selected_children()
        if not children:
            return
        wp = children[0].wp
        self.selected = wp
        self.title_label.set_text(wp.title)
        self.meta_label.set_text(f"{wp.type.upper()} · ID {wp.id}" + (f" · {wp.file}" if wp.file else ""))
        self.path_label.set_text(wp.path)
        self.path_label.set_tooltip_text(wp.path)
        if hasattr(self, "wp_engine_combo"):
            prev_loading = self._loading_ui
            self._loading_ui = True
            self.wp_engine_combo.set_active_id(self.cfg.get_wallpaper_mode(wp.id))
            self._loading_ui = prev_loading
        self.preview.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
        fut = self._executor.submit(load_wallpaper_cover, wp, PREVIEW_W, PREVIEW_H)
        fut.add_done_callback(lambda f, w=wp: _idle(self._set_preview, w, f))
        self._update_buttons()

    def _on_wp_engine_mode_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._loading_ui:
            return
        wp = self.selected
        if wp is None:
            return
        mode = combo.get_active_id() or "auto"
        self.cfg.set_wallpaper_mode(wp.id, mode, save=True)
        self._update_buttons()
        self._apply(wp, force=True)

    def _set_preview(self, wp: Wallpaper, fut: Future) -> None:
        if self.selected is not wp:
            return
        try:
            pb = fut.result()
        except Exception:  # noqa: BLE001
            pb = None
        if pb is not None:
            self.preview.set_from_pixbuf(pb)

    def _on_card_button(self, card: WallpaperCard, event: Gdk.EventButton) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False
        self.flow.select_child(card)
        menu = Gtk.Menu()
        for label, cb in (
            ("Aplicar fondo", lambda *_: self._apply(card.wp)),
            ("Abrir carpeta", lambda *_: self._xdg_open(card.wp.path)),
            ("Ver en Steam Workshop",
             lambda *_: self._xdg_open(f"https://steamcommunity.com/sharedfiles/filedetails/?id={card.wp.id}")),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        menu.show_all()
        menu.attach_to_widget(card, None)
        menu.popup_at_pointer(event)
        return True

    @staticmethod
    def _xdg_open(target: str) -> None:
        if not target or not isinstance(target, str):
            return
        target = target.strip()
        if not target or target.startswith("mailto:"):
            return
        if target.startswith(("https://", "http://")):
            threading.Thread(target=webbrowser.open, args=(target,), daemon=True).start()
            return
        try:
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            log.warning("xdg-open falló: %s", exc)

    # ============================================================ control
    def _missing_engine_for(self, wp: Wallpaper) -> str | None:
        bins = self.engine.binaries()
        mode = self.cfg.get_wallpaper_mode(wp.id)
        if wp.type == "video" or mode == "compat":
            if not bins["mpv"]:
                return "Falta mpv (ejecuta ./install.sh)"
            return None
        if wp.type in ("scene", "web") and not bins["linux-wallpaperengine"] and not bins["mpv"]:
            return "Falta linux-wallpaperengine / mpv (ejecuta ./install.sh)"
        if not wp.is_supported:
            return f"Tipo «{wp.type}» no soportado"
        return None

    def _update_buttons(self) -> None:
        wp = self.selected
        missing = self._missing_engine_for(wp) if wp else "Selecciona un fondo"
        self.apply_btn.set_sensitive(not self._busy and wp is not None and missing is None)
        self.apply_btn.set_tooltip_text(missing or "Aplicar este fondo (también: doble clic)")
        self.stop_btn.set_sensitive(not self._busy and self.active_id is not None)
        if hasattr(self, "random_btn"):
            self.random_btn.set_sensitive(not self._busy and len(self.cards) > 0)

    def _options(self, wp: Wallpaper | None = None) -> PlaybackOptions:
        target = wp or self.selected
        return PlaybackOptions.from_config(self.cfg, wp_id=target.id if target else None)

    def _apply(self, wp: Wallpaper | None = None, force: bool = False) -> None:
        wp = wp or self.selected
        if wp is None:
            return
        now = time.monotonic()
        with self._apply_lock:
            # Prevención de doble disparo: si el usuario hace doble clic en una tarjeta y enseguida
            # pulsa "Aplicar Fondo" sobre el mismo ID, ignorar el disparo duplicado.
            if not force and self._applying_wp_id == wp.id and (self._busy or (now - self._last_apply_ts) < 0.45):
                return
            self._last_apply_ts = now
            self._apply_seq += 1
            seq = self._apply_seq
            self._applying_wp_id = wp.id

        # Debounce de 300ms cuando ya hay una aplicación en curso para cancelar intentos intermedios
        if self._busy and not force:
            self._debounce("apply-debounce", 300, lambda w=wp, s=seq: self._dispatch_apply(w, s))
        else:
            old_timer = self._timers.pop("apply-debounce", None)
            if old_timer:
                GLib.source_remove(old_timer)
            self._dispatch_apply(wp, seq)

    def _dispatch_apply(self, wp: Wallpaper, seq: int) -> None:
        with self._apply_lock:
            if seq != self._apply_seq:
                return
        missing = self._missing_engine_for(wp)
        if missing:
            self._show_info(missing, Gtk.MessageType.INFO)
            return
        opts = self._options(wp)
        self._set_busy(True, f"Aplicando «{wp.title}»…")
        self._run_bg(
            lambda: self.engine.apply(wp, opts),
            lambda warnings, w=wp, s=seq: self._on_applied(w, warnings, s),
            lambda exc, s=seq: self._on_engine_error(exc, s),
        )

    def _on_applied(self, wp: Wallpaper, warnings: list[str], seq: int | None = None) -> None:
        with self._apply_lock:
            if seq is not None and seq != self._apply_seq:
                return
            self._applying_wp_id = None
        self.cfg.update(last_wallpaper=wp.to_dict(), active=True)
        self.cfg.save()
        try:
            autostart.sync(bool(self.cfg.get("autostart")))
        except OSError as exc:
            warnings.append(f"No se pudo escribir el autostart: {exc}")
        self.active_id = wp.id
        self._last_smart_paused = False
        self._mark_active()
        cur_mode = self.cfg.get_wallpaper_mode(wp.id)
        if cur_mode == "compat":
            mode_tag = " · COMPAT"
        elif cur_mode == "fix_scene":
            mode_tag = " · FIX-SCENE"
        else:
            mode_tag = ""
        self._set_busy(False, f"Fondo activo: {wp.title} [{wp.type.upper()}{mode_tag}]")
        if warnings:
            self._show_info("\n".join(warnings), Gtk.MessageType.INFO, auto_hide_ms=4000)
        else:
            self.infobar.set_revealed(False)

    def _on_engine_error(self, exc: BaseException, seq: int | None = None) -> None:
        with self._apply_lock:
            if seq is not None and seq != self._apply_seq:
                return
            self._applying_wp_id = None
        self._set_busy(False, "Error al aplicar el fondo")
        self.active_id = self.engine.active_wallpaper_id()
        self._mark_active()
        if isinstance(exc, EngineError):
            self._error_dialog("No se pudo aplicar el fondo", str(exc))
        else:
            self._error_dialog("Error inesperado", f"{type(exc).__name__}: {exc}")

    def _stop(self) -> None:
        with self._apply_lock:
            self._apply_seq += 1
            self._applying_wp_id = None
        self._set_busy(True, "Deteniendo fondo…")
        self._run_bg(lambda: self.engine.stop(restore_desktop=True), self._on_stopped,
                     self._on_engine_error)

    def _on_stopped(self, messages: list[str]) -> None:
        self.cfg.update(active=False)
        self.cfg.save()
        self.active_id = None
        self._last_smart_paused = False
        self._mark_active()
        self._set_busy(False, "Fondo detenido")
        if messages:
            self._show_info("\n".join(messages), Gtk.MessageType.INFO, auto_hide_ms=4000)

    def _reapply_active(self) -> None:
        if not self.active_id:
            return
        last = self.cfg.get("last_wallpaper")
        card = self.cards.get(self.active_id)
        wp = card.wp if card else (Wallpaper.from_dict(last) if isinstance(last, dict) else None)
        if wp:
            self._apply(wp, force=True)

    def _mark_active(self) -> None:
        for wid, card in self.cards.items():
            card.set_active(wid == self.active_id)
        self._update_buttons()

    def _poll_engine(self) -> bool:
        if self._busy:
            return True
        self.engine.state = load_state()
        current = self.engine.active_wallpaper_id()
        if current != self.active_id:
            self.active_id = current
            self._mark_active()
            if current is None:
                self._set_status("El motor de fondo ya no está en ejecución")
        return True

    def _poll_smart_pause(self) -> bool:
        """Verifica cada 2s si hay una ventana maximizada/fullscreen para pausar/reanudar (SIGSTOP/SIGCONT)."""
        if self._busy or self._smart_pause_busy or not self.active_id:
            return True
        enabled = bool(self.cfg.get("pause_on_fullscreen", True))
        self._smart_pause_busy = True

        def _done(paused: bool) -> None:
            self._smart_pause_busy = False
            if paused != self._last_smart_paused:
                self._last_smart_paused = paused
                if paused:
                    self._set_status(
                        "⚡ Ahorro inteligente activo: fondo pausado (otra app maximizada/fullscreen · 0% GPU/CPU)"
                    )
                else:
                    self._set_status("▶ Fondo reanudado automáticamente")

        def _err(_exc: BaseException) -> None:
            self._smart_pause_busy = False

        self._run_bg(lambda: self.engine.check_smart_pause(enabled), _done, _err)
        return True

    def _poll_window_monitor(self) -> bool:
        """WindowMonitor: verifica si la ventana activa no es el escritorio (nemo-desktop/xfdesktop/linwall)
        y aplica Auto-Mute temporal para liberar el reloj de PulseAudio/PipeWire.
        """
        if self._busy or self._auto_mute_busy or not self.active_id:
            return True
        enabled = bool(self.cfg.get("auto_mute_on_unfocus", True))
        volume = int(self.cfg.get("volume", 50))
        muted = bool(self.cfg.get("muted", True))
        self._auto_mute_busy = True

        def _done(temp_muted: bool) -> None:
            self._auto_mute_busy = False
            if temp_muted != self._last_temp_muted:
                self._last_temp_muted = temp_muted
                if temp_muted:
                    self._set_status(
                        "🔇 Auto-Mute activo: fondo silenciado temporalmente (otra aplicación en foco)"
                    )
                elif not muted and volume > 0:
                    self._set_status(f"🔊 Audio restaurado al enfocar escritorio ({volume}%)")

        def _err(_exc: BaseException) -> None:
            self._auto_mute_busy = False

        self._run_bg(
            lambda: self.engine.check_auto_mute(enabled, volume, muted),
            _done,
            _err,
        )
        return True

    # ============================================================ ajustes
    def _on_auto_mute_toggled(self, check: Gtk.CheckButton) -> None:
        if self._loading_ui:
            return
        enabled = bool(check.get_active())
        self.cfg.set("auto_mute_on_unfocus", enabled, save=True)
        volume = int(self.cfg.get("volume", 50))
        muted = bool(self.cfg.get("muted", True))
        self._run_bg(
            lambda: self.engine.check_auto_mute(enabled, volume, muted),
            lambda _tm: self._set_status(
                "🔇 Auto-Mute al desenfocar escritorio activado"
                if enabled
                else "🔊 Auto-Mute al desenfocar escritorio desactivado"
            ),
            lambda _exc: None,
        )

    def _update_mute_icon(self) -> None:
        name = "audio-volume-muted-symbolic" if self.mute_btn.get_active() else "audio-volume-high-symbolic"
        self.mute_btn.set_image(Gtk.Image.new_from_icon_name(name, Gtk.IconSize.BUTTON))

    def _on_mute_toggled(self, *_: Any) -> None:
        self._update_mute_icon()
        if self._loading_ui:
            return
        self._apply_audio_live()

    def _on_volume_slider_changed(self, *_: Any) -> None:
        if self._loading_ui:
            return
        vol = int(self.volume.get_value())
        # Si el usuario arrastra el control de volumen por encima de 0 y estaba en Mute,
        # desactivar Mute automáticamente para que escuche el cambio en caliente.
        if vol > 0 and self.mute_btn.get_active():
            self._loading_ui = True
            self.mute_btn.set_active(False)
            self._update_mute_icon()
            self._loading_ui = False
        self._apply_audio_live()

    def _apply_audio_live(self) -> None:
        volume = int(self.volume.get_value())
        muted = self.mute_btn.get_active()
        self.cfg.update(volume=volume, muted=muted)
        self._debounce("save", 400, self.cfg.save)
        if not self.active_id:
            return
        audio_text = "Silenciado" if muted or volume == 0 else f"Volumen {volume}%"

        def live_audio() -> None:
            self._run_bg(
                lambda: self.engine.set_volume(volume, muted),
                lambda ok: self._set_status(f"🔊 Audio en vivo: {audio_text}")
                if ok
                else self._debounce("reapply", 320, self._reapply_active),
                lambda _exc: None,
            )

        self._debounce("volume", 120, live_audio)

    def _on_render_setting_changed(self, combo: Gtk.ComboBoxText, key: str) -> None:
        if self._loading_ui:
            return
        value = combo.get_active_id()
        if value is None:
            return
        parsed = int(value) if key == "fps" else value
        self.cfg.set(key, parsed)
        self.cfg.save()
        if not self.active_id:
            return
        if key == "fps" and self.engine.state.get("kind") == "video":
            def live_fps() -> None:
                self._run_bg(
                    lambda: self.engine.set_fps(int(parsed)),
                    lambda ok: self._set_status(f"⚡ Rendimiento en vivo: {parsed} FPS") if ok else self._reapply_active(),
                    lambda _exc: self._reapply_active(),
                )
            self._debounce("fps-live", 150, live_fps)
        else:
            self._debounce("reapply", 300, self._reapply_active)

    def _on_pause_fullscreen_changed(self, switch: Gtk.Switch, _pspec: Any) -> None:
        if self._loading_ui:
            return
        enabled = switch.get_active()
        self.cfg.set("pause_on_fullscreen", enabled, save=True)
        if not enabled and self.engine.is_paused():
            self._run_bg(
                lambda: self.engine.set_paused(False),
                lambda _: self._set_status("Ahorro al maximizar desactivado (fondo en ejecución)"),
                lambda _exc: None,
            )
        else:
            self._set_status(
                "⚡ Pausa automática al maximizar activada"
                if enabled
                else "Pausa automática al maximizar desactivada"
            )

    def _update_desktop_mode_buttons(self, hide_icons: bool) -> None:
        show_ctx = self.show_icons_btn.get_style_context()
        imm_ctx = self.immersive_btn.get_style_context()
        if hide_icons:
            show_ctx.remove_class("mode-btn-active")
            show_ctx.add_class("mode-btn")
            imm_ctx.remove_class("mode-btn")
            imm_ctx.add_class("mode-btn-active")
        else:
            imm_ctx.remove_class("mode-btn-active")
            imm_ctx.add_class("mode-btn")
            show_ctx.remove_class("mode-btn")
            show_ctx.add_class("mode-btn-active")

    def _on_desktop_mode_clicked(self, hide_icons: bool) -> None:
        if self._loading_ui:
            return
        self.cfg.set("hide_desktop_icons", bool(hide_icons), save=True)
        self._update_desktop_mode_buttons(bool(hide_icons))
        mode_label = (
            "✨ Modo Inmersivo (Sin Iconos)"
            if hide_icons
            else "🗂️ Mostrar Iconos"
        )
        mon_spec = str(self.cfg.get("monitor") or "all")
        self._run_bg(
            lambda: self.engine.set_desktop_mode_live(bool(hide_icons), monitor_spec=mon_spec),
            lambda _msgs: self._set_status(f"Modo de escritorio activo en tiempo real: {mode_label}"),
            lambda exc: self._set_status(f"Error al cambiar modo de escritorio: {exc}"),
        )

    def _on_autostart_changed(self, switch: Gtk.Switch, _pspec: Any) -> None:
        if self._loading_ui:
            return
        enabled = switch.get_active()
        self.cfg.set("autostart", enabled, save=True)
        try:
            autostart.sync(enabled)
            self._set_status("Autostart activado" if enabled else "Autostart desactivado")
        except OSError as exc:
            self._show_info(f"No se pudo actualizar el autostart: {exc}", Gtk.MessageType.ERROR)

    def _refresh_monitors(self) -> None:
        prev_loading = self._loading_ui
        self._loading_ui = True
        monitors = list_monitors()
        self.monitor_combo.remove_all()
        self.monitor_combo.append("all", "Todos los monitores")
        for m in monitors:
            self.monitor_combo.append(m.name, m.label)
        wanted = self.cfg.get("monitor") or "all"
        if not self.monitor_combo.set_active_id(wanted):
            self.monitor_combo.set_active_id("all")
        self._loading_ui = prev_loading
        if not monitors:
            self._show_info("xrandr no devolvió monitores. ¿Estás en una sesión X11?",
                            Gtk.MessageType.WARNING)

    def _refresh_engine_info(self) -> None:
        lines = [f"<b>Escritorio:</b> {GLib.markup_escape_text(desktop_summary())}"]
        for name, path in self.engine.binaries().items():
            if path:
                lines.append(f"<span foreground='#a4d007'>✔</span> {name}")
            else:
                lines.append(f"<span foreground='#ff6b6b'>✘</span> {name} "
                             "<span foreground='#8f98a0'>(no instalado)</span>")
        self.engines_label.set_markup("\n".join(lines))

    def _on_add_folder(self, *_: Any) -> None:
        dialog = Gtk.FileChooserNative.new("Añadir carpeta de fondos", self,
                                           Gtk.FileChooserAction.SELECT_FOLDER, "_Añadir", "_Cancelar")
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_filename()
            extra = list(self.cfg.get("extra_dirs") or [])
            if folder and folder not in extra:
                extra.append(folder)
                self.cfg.set("extra_dirs", extra, save=True)
                self.rescan()
        dialog.destroy()

    def _on_open_log(self, *_: Any) -> None:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not paths.ENGINE_LOG.is_file() or paths.ENGINE_LOG.stat().st_size == 0:
            paths.ENGINE_LOG.write_text(
                f"=== {__app_name__} v{__version__} — Log del motor ===\n",
                encoding="utf-8",
            )
        for editor in ("xed", "gedit", "mousepad", "pluma", "kate", "leafpad"):
            ed_bin = paths.which(editor)
            if ed_bin:
                subprocess.Popen(
                    [ed_bin, str(paths.ENGINE_LOG)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return
        self._xdg_open(str(paths.ENGINE_LOG))

    def _on_show_about(self, *_: Any) -> None:
        dlg = Gtk.Dialog(
            title=f"Acerca de {__app_name__}",
            transient_for=self,
            modal=True,
            use_header_bar=1,
        )
        dlg.set_default_size(480, -1)
        dlg.set_resizable(False)
        dlg.get_style_context().add_class("linwall-about")

        content = dlg.get_content_area()
        content.set_spacing(12)
        for side in ("start", "end", "top", "bottom"):
            getattr(content, f"set_margin_{side}")(20)

        # Logo + Título
        if ICON_FILE.is_file():
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(ICON_FILE), 76, 76, True)
                content.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            except GLib.Error:
                pass

        title_lbl = Gtk.Label(label="LinWall Engine v1.6.1")
        title_lbl.get_style_context().add_class("about-title")
        content.pack_start(title_lbl, False, False, 0)

        sub_lbl = Gtk.Label(
            label="Cliente gráfico nativo para usar fondos de Wallpaper Engine (Steam Workshop 431960) en Linux"
        )
        sub_lbl.set_line_wrap(True)
        sub_lbl.set_max_width_chars(52)
        sub_lbl.set_justify(Gtk.Justification.CENTER)
        sub_lbl.get_style_context().add_class("dim")
        content.pack_start(sub_lbl, False, False, 0)

        # Tarjeta de autoría y créditos
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("about-card")

        author_lbl = Gtk.Label()
        author_lbl.set_markup(
            f"Desarrollado con orgullo desde <b>{GLib.markup_escape_text(__country__)}</b> "
            f"por <b><span foreground='#66c0f4'>{GLib.markup_escape_text(__author__)}</span></b>"
        )
        author_lbl.set_line_wrap(True)
        author_lbl.set_justify(Gtk.Justification.CENTER)
        author_lbl.get_style_context().add_class("about-author")
        card.pack_start(author_lbl, False, False, 2)

        ai_lbl = Gtk.Label()
        ai_lbl.set_markup(
            "<b>Colaboración de Arquitectura e IA:</b> "
            f"{GLib.markup_escape_text(__ai_credits__)}"
        )
        ai_lbl.set_justify(Gtk.Justification.CENTER)
        ai_lbl.get_style_context().add_class("dim")
        card.pack_start(ai_lbl, False, False, 0)

        lic_lbl = Gtk.Label()
        lic_lbl.set_markup(
            f"<b>Licencia:</b> GNU General Public License v3.0 ({GLib.markup_escape_text(__license__)})"
        )
        lic_lbl.set_justify(Gtk.Justification.CENTER)
        lic_lbl.get_style_context().add_class("dim")
        card.pack_start(lic_lbl, False, False, 0)

        # Caja de correo copiable al portapapeles (sin mailto: ni xdg-open para evitar
        # el diálogo 'Abrir con... No hay aplicaciones disponibles' cuando no hay cliente de correo)
        email_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        email_box.get_style_context().add_class("about-email-box")
        email_lbl = Gtk.Label()
        email_lbl.set_markup(
            f"✉  <b><span foreground='#66c0f4'>{GLib.markup_escape_text(__email__)}</span></b>"
        )
        email_lbl.set_selectable(True)
        email_lbl.set_hexpand(True)
        email_lbl.set_xalign(0)

        copy_btn = Gtk.Button(label="📋 Copiar correo")
        copy_btn.set_tooltip_text("Copiar walteguerrerob@gmail.com al portapapeles")

        def _copy_email(_btn: Gtk.Button) -> None:
            clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clip.set_text(__email__, -1)
            clip.store()
            copy_btn.set_label("✔ ¡Correo copiado!")
            self._set_status(f"✔ ¡Correo copiado al portapapeles ({__email__})!")

        copy_btn.connect("clicked", _copy_email)
        email_box.pack_start(email_lbl, True, True, 0)
        email_box.pack_end(copy_btn, False, False, 0)
        card.pack_start(email_box, False, False, 4)

        content.pack_start(card, False, False, 4)

        # Botones de GitHub y YouTube con webbrowser.open() sobre las URLs oficiales de Walter Guerrero
        links_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        gh_btn = Gtk.Button(label="🌐  Repositorio de GitHub")
        gh_btn.get_style_context().add_class("github-btn")
        gh_btn.connect(
            "clicked",
            lambda *_: threading.Thread(
                target=webbrowser.open,
                args=("https://github.com/guerratuto-bit/linwall-engine",),
                daemon=True,
            ).start(),
        )

        yt_btn = Gtk.Button(label="▶  Canal de YouTube (Tutoriales)")
        yt_btn.get_style_context().add_class("youtube-btn")
        yt_btn.connect(
            "clicked",
            lambda *_: threading.Thread(
                target=webbrowser.open,
                args=("https://www.youtube.com/@guerrapre/videos",),
                daemon=True,
            ).start(),
        )

        links_box.pack_start(gh_btn, True, True, 0)
        links_box.pack_start(yt_btn, True, True, 0)
        content.pack_start(links_box, False, False, 2)

        close_btn = Gtk.Button(label="Cerrar")
        close_btn.connect("clicked", lambda *_: dlg.destroy())
        content.pack_start(close_btn, False, False, 0)

        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def _on_destroy(self, *_: Any) -> None:
        for source in self._timers.values():
            GLib.source_remove(source)
        self._timers.clear()
        try:
            self.cfg.save()
        except OSError:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)
