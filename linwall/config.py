# =============================================================================
#  LinWall Engine v1.6.0 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Persistencia de la configuración del usuario en ``~/.config/linwall/config.json``."""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any

from linwall import paths

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "last_wallpaper": None,      # dict de Wallpaper.to_dict()
    "active": False,             # ¿había un fondo activo al cerrar sesión?
    "monitor": "all",            # "all" o nombre xrandr (eDP-1, HDMI-1...)
    "volume": 50,                # 0..100
    "muted": True,
    "fps": 30,                   # 30 / 60
    "autostart": True,
    "layer_mode": "auto",        # auto | desktop | root
    "hide_desktop_icons": False, # Mostrar iconos por defecto (Modo Inmersivo opcional)
    "pause_on_fullscreen": True, # Pausar (SIGSTOP) al maximizar/fullscreen otra app
    "wallpaper_modes": {},       # dict[str, str]: overrides por ID ("auto" | "fix_scene" | "compat")
    "extra_dirs": [],            # carpetas adicionales con proyectos
    "filter_type": "all",
}

VALID_FPS = (30, 60)
VALID_LAYERS = ("auto", "desktop", "root")
VALID_WP_MODES = ("auto", "fix_scene", "compat")
DEFAULT_FIX_SCENE_IDS = {"2867422409"}  # Yuta Okkotsu: desempaquetar y limpiar scene.json por defecto


class Config:
    def __init__(self, data: dict[str, Any] | None = None):
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        if data:
            self.data.update(data)
        self._sanitize()

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls) -> "Config":
        try:
            with open(paths.CONFIG_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}
        return cls(raw)

    def save(self) -> None:
        with self._lock:
            paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = paths.CONFIG_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, paths.CONFIG_FILE)

    # ------------------------------------------------------------- helpers
    def _sanitize(self) -> None:
        d = self.data
        try:
            d["volume"] = max(0, min(100, int(d.get("volume", 50))))
        except (TypeError, ValueError):
            d["volume"] = 50
        try:
            d["fps"] = int(d.get("fps", 30))
        except (TypeError, ValueError):
            d["fps"] = 30
        if d["fps"] not in VALID_FPS:
            d["fps"] = 30
        if d.get("layer_mode") not in VALID_LAYERS:
            d["layer_mode"] = "auto"
        if not isinstance(d.get("extra_dirs"), list):
            d["extra_dirs"] = []
        if not isinstance(d.get("wallpaper_modes"), dict):
            d["wallpaper_modes"] = {}
        else:
            d["wallpaper_modes"] = {
                str(k): str(v)
                for k, v in d["wallpaper_modes"].items()
                if str(v) in VALID_WP_MODES
            }
        if not isinstance(d.get("monitor"), str) or not d["monitor"]:
            d["monitor"] = "all"
        for key in ("muted", "autostart", "hide_desktop_icons", "pause_on_fullscreen", "active"):
            d[key] = bool(d.get(key, DEFAULTS[key]))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def set(self, key: str, value: Any, save: bool = False) -> None:
        with self._lock:
            self.data[key] = value
            self._sanitize()
        if save:
            self.save()

    def update(self, save: bool = False, **values: Any) -> None:
        with self._lock:
            self.data.update(values)
            self._sanitize()
        if save:
            self.save()

    def get_wallpaper_mode(self, wp_id: str | None) -> str:
        """Devuelve el modo de motor para ``wp_id``: ``'auto'``, ``'fix_scene'`` o ``'compat'``."""
        if not wp_id:
            return "auto"
        sid = str(wp_id)
        with self._lock:
            modes = self.data.get("wallpaper_modes") or {}
            if sid in modes:
                mode = str(modes.get(sid, "auto"))
                return mode if mode in VALID_WP_MODES else "auto"
            if sid in DEFAULT_FIX_SCENE_IDS:
                return "fix_scene"
            return "auto"

    def set_wallpaper_mode(self, wp_id: str, mode: str, save: bool = True) -> None:
        """Guarda la preferencia de motor (``'auto'`` | ``'fix_scene'`` | ``'compat'``) para un fondo."""
        if not wp_id:
            return
        sid = str(wp_id)
        clean_mode = mode if mode in VALID_WP_MODES else "auto"
        with self._lock:
            modes = dict(self.data.get("wallpaper_modes") or {})
            if clean_mode == "auto" and sid not in DEFAULT_FIX_SCENE_IDS:
                modes.pop(sid, None)
            else:
                modes[sid] = clean_mode
            self.data["wallpaper_modes"] = modes
            self._sanitize()
        if save:
            self.save()
