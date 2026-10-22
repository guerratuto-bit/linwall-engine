# =============================================================================
#  LinWall Engine v1.6.0 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Tarjeta de la cuadrícula: miniatura + insignia de tipo + título."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, Gtk, Pango  # noqa: E402

from linwall.scanner import Wallpaper  # noqa: E402

THUMB_W, THUMB_H = 232, 131
TYPE_LABELS = {"video": "VIDEO", "scene": "SCENE", "web": "WEB"}


class WallpaperCard(Gtk.FlowBoxChild):
    def __init__(self, wp: Wallpaper):
        super().__init__()
        self.wp = wp
        self.get_style_context().add_class("wp-card")
        self.set_valign(Gtk.Align.START)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        overlay = Gtk.Overlay()
        self.image = Gtk.Image()
        self.image.set_size_request(THUMB_W, THUMB_H)
        self.image.get_style_context().add_class("wp-thumb")
        self.image.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
        overlay.add(self.image)

        badge = Gtk.Label(label=TYPE_LABELS.get(wp.type, wp.type.upper() or "?"))
        ctx = badge.get_style_context()
        ctx.add_class("badge")
        ctx.add_class(f"badge-{wp.type if wp.type in TYPE_LABELS else 'other'}")
        badge.set_halign(Gtk.Align.START)
        badge.set_valign(Gtk.Align.START)
        badge.set_margin_start(6)
        badge.set_margin_top(6)
        overlay.add_overlay(badge)

        self.active_badge = Gtk.Label(label="● ACTIVO")
        self.active_badge.get_style_context().add_class("badge")
        self.active_badge.get_style_context().add_class("badge-active")
        self.active_badge.set_halign(Gtk.Align.END)
        self.active_badge.set_valign(Gtk.Align.START)
        self.active_badge.set_margin_end(6)
        self.active_badge.set_margin_top(6)
        self.active_badge.set_no_show_all(True)
        overlay.add_overlay(self.active_badge)

        if not wp.is_supported:
            unsupported = Gtk.Label(label="NO SOPORTADO")
            unsupported.get_style_context().add_class("badge")
            unsupported.get_style_context().add_class("badge-other")
            unsupported.set_halign(Gtk.Align.END)
            unsupported.set_valign(Gtk.Align.END)
            unsupported.set_margin_end(6)
            unsupported.set_margin_bottom(6)
            overlay.add_overlay(unsupported)

        title = Gtk.Label(label=wp.title)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(28)
        title.set_xalign(0)
        title.get_style_context().add_class("wp-title")

        box.pack_start(overlay, False, False, 0)
        box.pack_start(title, False, False, 0)
        self.add(box)
        self.set_tooltip_text(f"{wp.title}\nTipo: {wp.type.upper()}  ·  ID: {wp.id}")

        self._search_key = f"{wp.title} {wp.id} {' '.join(wp.tags)}".casefold()

    def matches(self, query: str, type_filter: str) -> bool:
        if type_filter != "all" and self.wp.type != type_filter:
            return False
        return not query or all(part in self._search_key for part in query.casefold().split())

    def set_pixbuf(self, pixbuf: GdkPixbuf.Pixbuf | None) -> None:
        if pixbuf is not None:
            self.image.set_from_pixbuf(pixbuf)

    def set_active(self, active: bool) -> None:
        self.active_badge.set_visible(active)
        ctx = self.get_style_context()
        (ctx.add_class if active else ctx.remove_class)("wp-card-active")
