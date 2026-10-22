# =============================================================================
#  LinWall Engine v1.6.0 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Integración con el entorno de escritorio (Cinnamon / XFCE / MATE / otros).

En Cinnamon (``nemo-desktop``), XFCE (``xfdesktop``) y MATE (``caja``) el gestor de
escritorio crea una ventana de tipo DESKTOP a pantalla completa que tapa la ventana
raíz de X11. Este módulo permite alternar esa capa en tiempo real de forma *reversible*
y segura entre «Mostrar Iconos» y «Modo Inmersivo (Sin Iconos)»:

* Cinnamon: ``gsettings org.nemo.desktop desktop-layout`` (o ``show-desktop-icons``).
* MATE:     ``gsettings org.mate.background show-desktop-icons``.
* XFCE:     ``xfdesktop --quit`` y relanzado posterior de ``xfdesktop``.

El valor original se guarda en el fichero de estado para poder restaurarlo incluso
tras reiniciar la sesión.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

from linwall import paths

log = logging.getLogger(__name__)

DESKTOP_PROCESSES = ("nemo-desktop", "xfdesktop", "caja", "pcmanfm", "pcmanfm-qt")


# ----------------------------------------------------------------- detección
def current_de() -> str:
    raw = " ".join(
        os.environ.get(v, "") for v in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")
    ).lower()
    for key, name in (("cinnamon", "cinnamon"), ("xfce", "xfce"), ("mate", "mate"),
                      ("gnome", "gnome"), ("kde", "kde"), ("plasma", "kde"), ("lxqt", "lxqt"),
                      ("lxde", "lxde"), ("budgie", "budgie")):
        if key in raw:
            return name
    for proc, name in (("nemo-desktop", "cinnamon"), ("xfdesktop", "xfce"), ("caja", "mate")):
        if process_running(proc):
            return name
    return "other"


def session_type() -> str:
    st = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if st:
        return st
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11" if os.environ.get("DISPLAY") else "unknown"


def process_running(name: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def running_desktop_manager() -> str | None:
    for proc in DESKTOP_PROCESSES:
        if process_running(proc):
            return proc
    return None


# ----------------------------------------------------------------- gsettings
def _gsettings(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def gsettings_has_key(schema: str, key: str) -> bool:
    res = _gsettings("list-keys", schema)
    return bool(res and res.returncode == 0 and key in res.stdout.split())


def gsettings_get(schema: str, key: str) -> str | None:
    res = _gsettings("get", schema, key)
    if res and res.returncode == 0:
        return res.stdout.strip()
    return None


def gsettings_set(schema: str, key: str, value: str) -> bool:
    res = _gsettings("set", schema, key, value)
    return bool(res and res.returncode == 0)


def _spawn_detached(argv: list[str]) -> None:
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log.warning("No se pudo lanzar %s: %s", argv[0], exc)


# ------------------------------------------------------------------ gestor
class DesktopIconsManager:
    """Oculta y muestra la capa de iconos del escritorio en tiempo real y de forma reversible."""

    STATE_KEY = "desktop_hidden"

    def __init__(self, de: str | None = None):
        self.de = de or current_de()

    def wait_for_desktop(self, timeout: float = 12.0) -> str | None:
        """Espera (útil al iniciar sesión) a que arranque el gestor de escritorio."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = running_desktop_manager()
            if proc:
                return proc
            time.sleep(0.5)
        return None

    def _plan(self) -> dict[str, Any] | None:
        proc = running_desktop_manager()
        if self.de == "cinnamon" or proc == "nemo-desktop":
            for key, hidden, visible in (
                ("desktop-layout", "'false::false'", "'true::false'"),
                ("show-desktop-icons", "false", "true"),
            ):
                if gsettings_has_key("org.nemo.desktop", key):
                    return {
                        "method": "gsettings",
                        "schema": "org.nemo.desktop",
                        "key": key,
                        "hidden": hidden,
                        "visible": visible,
                    }
        if self.de == "mate" or proc == "caja":
            if gsettings_has_key("org.mate.background", "show-desktop-icons"):
                return {
                    "method": "gsettings",
                    "schema": "org.mate.background",
                    "key": "show-desktop-icons",
                    "hidden": "false",
                    "visible": "true",
                }
        if self.de == "xfce" or proc == "xfdesktop":
            return {"method": "xfdesktop"}
        if proc in ("pcmanfm", "pcmanfm-qt"):
            return {"method": "pcmanfm", "process": proc}
        return None

    def hide(self, state: dict[str, Any]) -> str | None:
        """Activa el Modo Inmersivo (Sin Iconos). Devuelve un mensaje informativo o ``None``."""
        if state.get(self.STATE_KEY):
            # Ya oculto: re-aplicar sin sobreescribir el valor original guardado.
            info = state[self.STATE_KEY]
            if info.get("method") == "gsettings":
                gsettings_set(info["schema"], info["key"], info["hidden"])
            elif info.get("method") == "xfdesktop" and process_running("xfdesktop"):
                self._quit_xfdesktop()
            return None

        plan = self._plan()
        if not plan:
            return None
        if plan["method"] == "gsettings":
            original = gsettings_get(plan["schema"], plan["key"])
            if original is None:
                return "No se pudo leer la configuración del escritorio."
            plan["original"] = original
            if original != plan["hidden"]:
                gsettings_set(plan["schema"], plan["key"], plan["hidden"])
            state[self.STATE_KEY] = plan
            return f"Modo Inmersivo activado: iconos ocultos ({plan['schema']} {plan['key']})."
        if plan["method"] == "xfdesktop":
            if not process_running("xfdesktop"):
                return None
            self._quit_xfdesktop()
            state[self.STATE_KEY] = plan
            return "xfdesktop detenido temporalmente (se restaurará al mostrar iconos o detener el fondo)."
        if plan["method"] == "pcmanfm":
            # pcmanfm dibuja el escritorio con --desktop; se reinicia al restaurar.
            subprocess.run(["pkill", "-f", f"{plan['process']} --desktop"], check=False)
            state[self.STATE_KEY] = plan
            return f"{plan['process']} --desktop detenido temporalmente."
        return None

    def show(self, state: dict[str, Any]) -> str | None:
        """Activa explícitamente «Mostrar Iconos» en tiempo real."""
        msg = self.restore(state)
        plan = self._plan()
        if not plan:
            return msg
        if plan["method"] == "gsettings":
            current = gsettings_get(plan["schema"], plan["key"])
            visible_val = plan.get("visible", "'true::false'")
            if current == plan["hidden"]:
                gsettings_set(plan["schema"], plan["key"], visible_val)
                return "Iconos del escritorio visibles."
            if self.de == "cinnamon" and not process_running("nemo-desktop"):
                _spawn_detached(["nemo-desktop"])
                return "nemo-desktop iniciado: iconos del escritorio visibles."
        elif plan["method"] == "xfdesktop" and not process_running("xfdesktop"):
            _spawn_detached(["xfdesktop"])
            return "xfdesktop iniciado: iconos del escritorio visibles."
        elif plan["method"] == "pcmanfm":
            proc = plan.get("process", "pcmanfm")
            if not process_running(proc):
                _spawn_detached([proc, "--desktop"])
                return f"{proc} --desktop iniciado: iconos visibles."
        return msg

    def apply_live(self, hide_icons: bool, state: dict[str, Any]) -> str | None:
        """Alterna en caliente entre Modo Inmersivo (hide_icons=True) y Mostrar Iconos (False)."""
        return self.hide(state) if hide_icons else self.show(state)

    @staticmethod
    def _quit_xfdesktop() -> None:
        subprocess.run(["xfdesktop", "--quit"], capture_output=True, timeout=5, check=False)
        # xfce4-session podría relanzarlo: segundo intento tras un momento.
        time.sleep(1.5)
        if process_running("xfdesktop"):
            subprocess.run(["xfdesktop", "--quit"], capture_output=True, timeout=5, check=False)

    def restore(self, state: dict[str, Any]) -> str | None:
        info = state.pop(self.STATE_KEY, None)
        if not info:
            return None
        method = info.get("method")
        if method == "gsettings" and info.get("original") is not None:
            orig = info["original"]
            # Si el valor original ya era 'oculto', usar el valor visible explícito al restaurar.
            if orig == info.get("hidden") and info.get("visible"):
                orig = info["visible"]
            gsettings_set(info["schema"], info["key"], orig)
            return "Iconos del escritorio restaurados."
        if method == "xfdesktop" and not process_running("xfdesktop"):
            _spawn_detached(["xfdesktop"])
            return "xfdesktop restaurado."
        if method == "pcmanfm":
            proc = info.get("process", "pcmanfm")
            _spawn_detached([proc, "--desktop"])
            return f"{proc} --desktop restaurado."
        return None


def desktop_summary() -> str:
    de = current_de()
    names = {"cinnamon": "Cinnamon", "xfce": "XFCE", "mate": "MATE", "gnome": "GNOME",
             "kde": "KDE Plasma", "lxqt": "LXQt", "lxde": "LXDE", "budgie": "Budgie",
             "other": "Otro / WM"}
    return f"{names.get(de, de)} · {session_type().upper()}"


__all__ = [
    "DesktopIconsManager", "current_de", "session_type", "running_desktop_manager",
    "desktop_summary", "process_running", "paths",
]
