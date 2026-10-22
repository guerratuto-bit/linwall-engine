# =============================================================================
#  LinWall Engine v1.6.1 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Utilidades X11: anclaje Z-Order DEBAJO de nemo-desktop/xfdesktop, repintado forzado y ahorro inteligente.

Solución definitiva al Z-Order en Cinnamon (Muffin), XFCE (Xfwm4) y MATE (Marco):
1. Cuando ``linux-wallpaperengine --window 0x0x<W>x<H>`` abre su ventana GLFW, la mapea
   inicialmente como ``_NET_WM_WINDOW_TYPE_NORMAL`` (capa normal, por encima de ``nemo-desktop``).
2. ``pin_to_desktop(..., remap=True)`` aplica:
   * ``WM_HINTS.input = 0`` (sin foco de entrada, igual que ``xwinwrap -nf``).
   * ``_NET_WM_USER_TIME = 0`` (evita que el WM eleve o enfoque la ventana al mapearla).
   * ``_NET_WM_BYPASS_COMPOSITOR = 2`` (evita que Muffin desactive el compositor, igual que ``xwinwrap -un``).
   * ``_NET_WM_WINDOW_TYPE = _NET_WM_WINDOW_TYPE_DESKTOP`` y ``_NET_WM_STATE_BELOW``.
   * Un ciclo único ``XUnmapWindow`` -> átomos -> ``XMapWindow`` + ``XLowerWindow`` para que Muffin
     reclasifique la ventana dentro de ``META_LAYER_DESKTOP`` (Capa 0, la misma capa que ``nemo-desktop``).
3. ``enforce_z_order_below_desktop()`` utiliza directamente ``libX11.so.6`` (``XLowerWindow``,
   ``XRaiseWindow``, ``XRestackWindows`` y el mensaje EWMH ``_NET_RESTACK_WINDOW`` con
   ``sibling = nemo_desktop_wid`` y ``detail = Below``) junto con ``xdotool windowlower`` /
   ``windowraise`` cada 0.2s durante los primeros 3 segundos, garantizando que el fondo quede
   estrictamente DEBAJO de los iconos del escritorio y de la barra de tareas.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import time
from typing import Callable, Iterable

from linwall.monitors import Monitor
from linwall.paths import which

log = logging.getLogger(__name__)

SHAPE_INPUT = 2  # <X11/extensions/shapeconst.h>
INPUT_HINT_FLAG = 1  # InputHint en <X11/Xutil.h>
CLIENT_MESSAGE = 33  # ClientMessage en <X11/X.h>
SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # SubstructureNotifyMask | SubstructureRedirectMask
STACK_ABOVE = 0  # Above en <X11/X.h>
STACK_BELOW = 1  # Below en <X11/X.h>

DESKTOP_CLASSES = ("nemo-desktop", "xfdesktop", "caja", "pcmanfm")
PANEL_CLASSES = ("cinnamon-panel", "xfce4-panel", "mate-panel", "lxpanel", "plank")
DESKTOP_AND_PANEL_CLASSES = DESKTOP_CLASSES + PANEL_CLASSES


class _XWMHints(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_long),
        ("input", ctypes.c_int),
        ("initial_state", ctypes.c_int),
        ("icon_pixmap", ctypes.c_ulong),
        ("icon_window", ctypes.c_ulong),
        ("icon_x", ctypes.c_int),
        ("icon_y", ctypes.c_int),
        ("icon_mask", ctypes.c_ulong),
        ("window_group", ctypes.c_ulong),
    ]


class _ClientMessageData(ctypes.Union):
    _fields_ = [
        ("b", ctypes.c_char * 20),
        ("s", ctypes.c_short * 10),
        ("l", ctypes.c_long * 5),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _ClientMessageData),
    ]


def _parse_wid(raw: str | int) -> int | None:
    if isinstance(raw, int):
        return raw if raw > 0 else None
    s = str(raw).strip()
    if not s:
        return None
    try:
        val = int(s, 16) if s.startswith(("0x", "0X")) else int(s)
        return val if val > 0 else None
    except ValueError:
        return None


def _run(args: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Fallo ejecutando %s: %s", args, exc)
        return None


def get_active_window() -> str | None:
    """Devuelve el WID de la ventana con foco actual (para restaurarlo tras lanzar GLFW o auditar foco)."""
    if which("xdotool"):
        res = _run(["xdotool", "getactivewindow"], timeout=1.2)
        if res and res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    if which("xprop"):
        res = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], timeout=1.2)
        if res and res.returncode == 0 and "#" in res.stdout:
            wid = res.stdout.split("#", 1)[1].strip().split(",")[0].strip()
            if _parse_wid(wid) is not None:
                return wid
    return None


def windows_for_pids(
    pids: Iterable[int],
    wm_class: str | None = None,
    exclude_wids: set[int] | None = None,
) -> list[str]:
    """IDs de ventana (hex) cuyo ``_NET_WM_PID`` pertenece a ``pids`` (o ``WM_CLASS`` coincide)."""
    pid_set = {int(p) for p in pids}
    exclude = exclude_wids or set()
    found: list[str] = []
    seen: set[int] = set()

    def _add(raw_wid: str) -> None:
        val = _parse_wid(raw_wid)
        if val is not None and val not in exclude and val not in seen:
            seen.add(val)
            found.append(hex(val))

    if which("wmctrl") and pid_set:
        res = _run(["wmctrl", "-lp"], timeout=2.0)
        if res and res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) >= 3 and parts[2].isdigit() and int(parts[2]) in pid_set:
                    _add(parts[0])

    if not found and which("xdotool") and pid_set:
        for pid in pid_set:
            res = _run(["xdotool", "search", "--pid", str(pid)], timeout=2.0)
            if res and res.returncode == 0:
                for w in res.stdout.split():
                    _add(w)

    if not found and wm_class:
        if which("wmctrl"):
            res = _run(["wmctrl", "-lx"], timeout=2.0)
            if res and res.returncode == 0:
                cls_low = wm_class.lower()
                for line in res.stdout.splitlines():
                    parts = line.split(None, 4)
                    if len(parts) >= 3 and cls_low in parts[2].lower():
                        _add(parts[0])
        if not found and which("xdotool"):
            res = _run(["xdotool", "search", "--class", wm_class], timeout=2.0)
            if res and res.returncode == 0:
                for w in res.stdout.split():
                    _add(w)

    return found


def list_all_windows() -> set[int]:
    """Devuelve el conjunto de IDs de ventana actuales (enteros) para filtrar ventanas previas."""
    wids: set[int] = set()
    if which("wmctrl"):
        res = _run(["wmctrl", "-l"], timeout=2.0)
        if res and res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split(None, 1)
                if parts:
                    val = _parse_wid(parts[0])
                    if val is not None:
                        wids.add(val)
    return wids


def wait_for_windows(
    pid_provider: Callable[[], list[int]],
    expected: int = 1,
    timeout: float = 10.0,
    wm_class: str | None = None,
    exclude_wids: set[int] | None = None,
) -> list[str]:
    """Espera con polling ultrarrápido (0.04s) a que aparezcan las ventanas o el proceso termine."""
    deadline = time.monotonic() + timeout
    windows: list[str] = []
    started = time.monotonic()
    while time.monotonic() < deadline:
        pids = pid_provider()
        if not pids and (time.monotonic() - started) > 0.2:
            break
        windows = windows_for_pids(pids, wm_class=wm_class, exclude_wids=exclude_wids)
        if len(windows) >= expected:
            return windows
        time.sleep(0.04)
    return windows


def find_desktop_and_panel_windows(
    exclude_wids: Iterable[str | int] = (),
) -> tuple[list[str], list[str], str | None]:
    """Localiza los WIDs de ``nemo-desktop``/``xfdesktop``/``caja`` y de los paneles del escritorio."""
    excl: set[int] = set()
    for raw in exclude_wids:
        val = _parse_wid(raw)
        if val is not None:
            excl.add(val)

    desktop_wids: list[str] = []
    panel_wids: list[str] = []
    linwall_gui_wid: str | None = None
    seen_desk: set[int] = set()
    seen_panel: set[int] = set()

    def _add_desk(raw: str) -> None:
        v = _parse_wid(raw)
        if v is not None and v not in excl and v not in seen_desk:
            seen_desk.add(v)
            desktop_wids.append(hex(v))

    def _add_panel(raw: str) -> None:
        v = _parse_wid(raw)
        if v is not None and v not in excl and v not in seen_panel:
            seen_panel.add(v)
            panel_wids.append(hex(v))

    if which("wmctrl"):
        res = _run(["wmctrl", "-lx"], timeout=2.0)
        if res and res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 3:
                    continue
                wid = parts[0]
                wclass = parts[2].lower()
                wtitle = parts[4].lower() if len(parts) >= 5 else ""
                if "linux-wallpaperengine" in wclass or "xwinwrap" in wclass or "linwall-wallpaper" in wtitle:
                    continue
                if "linwall" in wclass or "linwall engine" in wtitle:
                    linwall_gui_wid = wid
                elif any(k in wclass for k in DESKTOP_CLASSES) or wtitle in ("desktop", "escritorio"):
                    _add_desk(wid)
                elif any(k in wclass for k in PANEL_CLASSES):
                    _add_panel(wid)

    if which("xdotool"):
        for cls_name in ("nemo-desktop", "xfdesktop", "caja"):
            res = _run(["xdotool", "search", "--onlyvisible", "--class", cls_name], timeout=1.5)
            if not res or res.returncode != 0 or not res.stdout.strip():
                res = _run(["xdotool", "search", "--class", cls_name], timeout=1.5)
            if res and res.returncode == 0:
                for w in res.stdout.split():
                    _add_desk(w)
        for cls_name in ("cinnamon-panel", "xfce4-panel", "mate-panel"):
            res = _run(["xdotool", "search", "--onlyvisible", "--class", cls_name], timeout=1.5)
            if res and res.returncode == 0:
                for w in res.stdout.split():
                    _add_panel(w)

    return desktop_wids, panel_wids, linwall_gui_wid


def make_click_through(wid: str) -> bool:
    """Vacía la región ShapeInput (igual que ``xwinwrap -ni``) para no robar clics del escritorio."""
    window_id = _parse_wid(wid)
    if window_id is None:
        return False
    try:
        x11_lib = ctypes.CDLL("libX11.so.6")
        xfixes = ctypes.CDLL("libXfixes.so.3")
        x11_lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11_lib.XOpenDisplay.restype = ctypes.c_void_p
        x11_lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11_lib.XFlush.argtypes = [ctypes.c_void_p]

        xfixes.XFixesCreateRegion.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        xfixes.XFixesCreateRegion.restype = ctypes.c_ulong
        xfixes.XFixesSetWindowShapeRegion.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong
        ]
        xfixes.XFixesDestroyRegion.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

        dpy = x11_lib.XOpenDisplay(None)
        if not dpy:
            return False
        try:
            region = xfixes.XFixesCreateRegion(dpy, None, 0)
            xfixes.XFixesSetWindowShapeRegion(dpy, window_id, SHAPE_INPUT, 0, 0, region)
            xfixes.XFixesDestroyRegion(dpy, region)
            x11_lib.XFlush(dpy)
            return True
        finally:
            x11_lib.XCloseDisplay(dpy)
    except Exception as exc:  # noqa: BLE001
        log.debug("No se pudo aplicar ShapeInput vacío a %s: %s", wid, exc)
        return False


def _set_no_focus_wm_hints(wid: str) -> None:
    """Aplica ``WM_HINTS.input = False`` (igual que ``xwinwrap -nf``) vía ``libX11.so.6``."""
    window_id = _parse_wid(wid)
    if window_id is None:
        return
    try:
        x11_lib = ctypes.CDLL("libX11.so.6")
        x11_lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11_lib.XOpenDisplay.restype = ctypes.c_void_p
        x11_lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11_lib.XSetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XWMHints)]
        x11_lib.XFlush.argtypes = [ctypes.c_void_p]

        dpy = x11_lib.XOpenDisplay(None)
        if not dpy:
            return
        try:
            hints = _XWMHints()
            hints.flags = INPUT_HINT_FLAG
            hints.input = 0
            x11_lib.XSetWMHints(dpy, window_id, ctypes.byref(hints))
            x11_lib.XFlush(dpy)
        finally:
            x11_lib.XCloseDisplay(dpy)
    except Exception as exc:  # noqa: BLE001
        log.debug("No se pudo aplicar XSetWMHints(input=0) a %s: %s", wid, exc)


def enforce_z_order_below_desktop(
    lower_wids: Iterable[str | int],
    desktop_wids: Iterable[str | int],
    panel_wids: Iterable[str | int],
) -> None:
    """Fuerza en el servidor X11 y en el WM (Muffin/Xfwm4/Marco) el Z-Order exacto:

    ``[paneles] > [nemo-desktop / xfdesktop / caja] > [linux-wallpaperengine]``

    Usa ``ctypes.CDLL('libX11.so.6')`` para:
    1. ``XLowerWindow(dpy, wallpaper_wid)``
    2. ``XRaiseWindow(dpy, nemo_desktop_wid)`` y ``XRaiseWindow(dpy, panel_wid)``
    3. Mensaje EWMH ``_NET_RESTACK_WINDOW`` (``source=2``, ``sibling=nemo_desktop_wid``, ``detail=Below``)
       que invoca ``meta_window_stack_just_below(wallpaper, nemo_desktop)`` dentro de Muffin.
    4. ``XRestackWindows(dpy, [paneles..., escritorios..., fondos...])``
    5. ``XClearArea(dpy, ..., exposures=1)`` para que los iconos se repinten en el acto.
    """
    low_ids = [w for w in (_parse_wid(x) for x in lower_wids) if w is not None]
    desk_ids = [w for w in (_parse_wid(x) for x in desktop_wids) if w is not None and w not in low_ids]
    pan_ids = [w for w in (_parse_wid(x) for x in panel_wids) if w is not None and w not in low_ids]

    try:
        x11_lib = ctypes.CDLL("libX11.so.6")
        x11_lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11_lib.XOpenDisplay.restype = ctypes.c_void_p
        x11_lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11_lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11_lib.XDefaultRootWindow.restype = ctypes.c_ulong
        x11_lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11_lib.XInternAtom.restype = ctypes.c_ulong
        x11_lib.XLowerWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11_lib.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11_lib.XRestackWindows.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
        x11_lib.XSendEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
            ctypes.POINTER(_XClientMessageEvent),
        ]
        x11_lib.XClearArea.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
        ]
        x11_lib.XFlush.argtypes = [ctypes.c_void_p]

        dpy = x11_lib.XOpenDisplay(None)
        if not dpy:
            return
        try:
            root = x11_lib.XDefaultRootWindow(dpy)
            atom_restack = x11_lib.XInternAtom(dpy, b"_NET_RESTACK_WINDOW", 0)

            def _send_restack(win: int, sibling: int, detail: int) -> None:
                if not atom_restack:
                    return
                ev = _XClientMessageEvent()
                ev.type = CLIENT_MESSAGE
                ev.send_event = 1
                ev.display = dpy
                ev.window = win
                ev.message_type = atom_restack
                ev.format = 32
                ev.data.l[0] = 2  # 2 = Pager / herramienta de sistema (honrado siempre por Muffin)
                ev.data.l[1] = sibling
                ev.data.l[2] = detail
                ev.data.l[3] = 0
                ev.data.l[4] = 0
                x11_lib.XSendEvent(dpy, root, 0, SUBSTRUCTURE_MASK, ctypes.byref(ev))

            # 1) Bajar la ventana del fondo al fondo de la pila X11 y justo debajo de nemo-desktop
            for lw in low_ids:
                x11_lib.XLowerWindow(dpy, lw)
                _send_restack(lw, 0, STACK_BELOW)
                for dw in desk_ids:
                    _send_restack(lw, dw, STACK_BELOW)

            # 2) Elevar nemo-desktop / xfdesktop / caja por encima de la ventana del fondo
            for dw in desk_ids:
                x11_lib.XRaiseWindow(dpy, dw)
                _send_restack(dw, 0, STACK_ABOVE)

            # 3) Elevar los paneles (cinnamon-panel / xfce4-panel / mate-panel)
            for pw in pan_ids:
                x11_lib.XRaiseWindow(dpy, pw)
                _send_restack(pw, 0, STACK_ABOVE)

            # 4) XRestackWindows atómico de arriba hacia abajo: [paneles, escritorio, fondo]
            ordered = pan_ids + desk_ids + low_ids
            if len(ordered) >= 2:
                arr_t = ctypes.c_ulong * len(ordered)
                arr = arr_t(*ordered)
                x11_lib.XRestackWindows(dpy, arr, len(ordered))

            # 5) Enviar Expose directo a las ventanas de iconos y paneles para repintado inmediato
            for w in desk_ids + pan_ids:
                x11_lib.XClearArea(dpy, w, 0, 0, 0, 0, 1)

            x11_lib.XFlush(dpy)
        finally:
            x11_lib.XCloseDisplay(dpy)
    except Exception as exc:  # noqa: BLE001
        log.debug("Error en enforce_z_order_below_desktop (libX11): %s", exc)


def force_desktop_repaint(
    lower_wids: Iterable[str] = (),
    restore_focus_wid: str | None = None,
    trigger_xrefresh: bool = True,
) -> None:
    """Baja los fondos, eleva ``nemo-desktop``/paneles y fuerza el repintado inmediato."""
    low_list = [str(w) for w in lower_wids if w]
    desktop_wids, panel_wids, linwall_gui_wid = find_desktop_and_panel_windows(exclude_wids=low_list)

    has_xdotool = bool(which("xdotool"))
    if has_xdotool:
        for wid in low_list:
            _run(["xdotool", "windowlower", str(wid)], timeout=1.5)
        for wid in desktop_wids:
            _run(["xdotool", "windowraise", str(wid)], timeout=1.5)
        for wid in panel_wids:
            _run(["xdotool", "windowraise", str(wid)], timeout=1.5)

    # Refuerzo directo en libX11.so.6 (XLowerWindow + XRaiseWindow + _NET_RESTACK_WINDOW + XRestackWindows)
    enforce_z_order_below_desktop(low_list, desktop_wids, panel_wids)

    if has_xdotool:
        focus_target = restore_focus_wid or linwall_gui_wid
        if focus_target:
            _run(["xdotool", "windowactivate", str(focus_target)], timeout=1.5)

    if trigger_xrefresh and which("xrefresh"):
        _run(["xrefresh"], timeout=1.5)


def raise_desktop_and_panels(lower_wids: Iterable[str] = ()) -> None:
    """Alias compatible para elevar iconos/paneles y forzar su repintado."""
    force_desktop_repaint(lower_wids=lower_wids, trigger_xrefresh=True)


def _apply_xprop_desktop_atoms(wid: str) -> None:
    _set_no_focus_wm_hints(wid)
    if not which("xprop"):
        return
    # _NET_WM_USER_TIME = 0 indica a Muffin/Xfwm4 que no enfoque ni eleve la ventana al mapearse.
    _run(["xprop", "-id", wid, "-f", "_NET_WM_USER_TIME", "32c",
          "-set", "_NET_WM_USER_TIME", "0"], timeout=1.5)
    # _NET_WM_BYPASS_COMPOSITOR = 2 impide que Muffin desactive la composición (igual que xwinwrap -un).
    _run(["xprop", "-id", wid, "-f", "_NET_WM_BYPASS_COMPOSITOR", "32c",
          "-set", "_NET_WM_BYPASS_COMPOSITOR", "2"], timeout=1.5)
    _run(["xprop", "-id", wid, "-f", "_MOTIF_WM_HINTS", "32c",
          "-set", "_MOTIF_WM_HINTS", "0x2, 0x0, 0x0, 0x0, 0x0"], timeout=1.5)
    _run(["xprop", "-id", wid, "-f", "_NET_WM_WINDOW_TYPE", "32a",
          "-set", "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_DESKTOP"], timeout=1.5)
    _run(["xprop", "-id", wid, "-f", "_NET_WM_STATE", "32a",
          "-set", "_NET_WM_STATE",
          "_NET_WM_STATE_BELOW,_NET_WM_STATE_STICKY,"
          "_NET_WM_STATE_SKIP_TASKBAR,_NET_WM_STATE_SKIP_PAGER"], timeout=1.5)


def pin_to_desktop(
    wid: str,
    monitor: Monitor,
    lower_window: bool = True,
    remap: bool = True,
    restore_focus_wid: str | None = None,
    trigger_xrefresh: bool = True,
) -> bool:
    """Convierte una ventana en capa DESKTOP y la sitúa estrictamente DEBAJO de ``nemo-desktop``.

    * Cuando ``remap=True`` (primer anclaje de la ventana GLFW de ``linux-wallpaperengine``),
      desmapea brevemente la ventana, aplica ``_NET_WM_WINDOW_TYPE_DESKTOP``, ``WM_HINTS.input=0``
      y ``_NET_WM_USER_TIME=0``, y vuelve a mapearla para que Muffin la mueva de ``META_LAYER_NORMAL``
      a ``META_LAYER_DESKTOP`` (la misma capa que ``nemo-desktop`` y ``xwinwrap -fdt``).
    * Enseguida ejecuta ``XLowerWindow`` + ``XRaiseWindow(nemo-desktop)`` + ``_NET_RESTACK_WINDOW``
      para que quede al fondo de ``META_LAYER_DESKTOP``, detrás de los iconos.
    """
    ok = True
    has_xprop = bool(which("xprop"))
    has_wmctrl = bool(which("wmctrl"))
    has_xdotool = bool(which("xdotool"))

    make_click_through(wid)

    if has_xprop:
        _apply_xprop_desktop_atoms(wid)
        if remap and has_xdotool:
            _run(["xdotool", "windowunmap", wid], timeout=1.5)
            _apply_xprop_desktop_atoms(wid)
            _run(["xdotool", "windowmap", wid], timeout=1.5)
    else:
        ok = False

    if has_wmctrl:
        _run(["wmctrl", "-i", "-r", wid, "-b", "remove,fullscreen,above,maximized_vert,maximized_horz"], timeout=1.5)
        if remap:
            _run(["wmctrl", "-i", "-r", wid, "-e",
                  f"0,{monitor.x},{monitor.y},{monitor.width},{monitor.height}"], timeout=1.5)
        _run(["wmctrl", "-i", "-r", wid, "-b", "add,below,sticky"], timeout=1.5)
        _run(["wmctrl", "-i", "-r", wid, "-b", "add,skip_taskbar,skip_pager"], timeout=1.5)
    elif has_xdotool and remap:
        _run(["xdotool", "windowmove", wid, str(monitor.x), str(monitor.y)], timeout=1.5)
        _run(["xdotool", "windowsize", wid, str(monitor.width), str(monitor.height)], timeout=1.5)
    elif not has_wmctrl and not has_xdotool:
        ok = False

    if has_xdotool:
        if remap:
            _run(["xdotool", "windowmove", wid, str(monitor.x), str(monitor.y)], timeout=1.5)
            _run(["xdotool", "windowsize", wid, str(monitor.width), str(monitor.height)], timeout=1.5)
        _run(["xdotool", "windowlower", wid], timeout=1.5)

    make_click_through(wid)
    force_desktop_repaint(
        lower_wids=[wid] if lower_window else [wid],
        restore_focus_wid=restore_focus_wid,
        trigger_xrefresh=trigger_xrefresh,
    )
    return ok


def enforce_desktop_layer_async(
    pid_monitors: list[tuple[Callable[[], list[int]], Monitor, set[int]]],
    restore_focus_wid: str | None = None,
    duration: float = 3.0,
    interval: float = 0.2,
) -> None:
    """Bucle de control que ejecuta cada 0.2s durante los primeros 3 segundos:
    * ``xdotool windowlower <WALLPAPER_WID>``
    * ``xdotool windowraise <NEMO_DESKTOP_WID>``
    * ``xdotool windowraise <PANEL_WID>``
    * ``XLowerWindow`` + ``XRaiseWindow`` + ``_NET_RESTACK_WINDOW`` + ``XRestackWindows`` directo en ``libX11.so.6``.
    """
    remapped_wids: set[str] = set()

    def _worker() -> None:
        deadline = time.monotonic() + duration
        first_pass = True
        while time.monotonic() < deadline:
            any_alive = False
            lowered: list[str] = []
            for pid_fn, monitor, exclude_wids in pid_monitors:
                pids = pid_fn()
                if not pids:
                    continue
                any_alive = True
                wids = windows_for_pids(
                    pids, wm_class="linux-wallpaperengine", exclude_wids=exclude_wids
                )
                for wid in wids:
                    # Si una ventana secundaria apareció después del primer pin, hacerle remap una sola vez
                    need_remap = wid not in remapped_wids and not first_pass
                    remapped_wids.add(wid)
                    if need_remap:
                        pin_to_desktop(
                            wid,
                            monitor,
                            lower_window=True,
                            remap=True,
                            restore_focus_wid=restore_focus_wid,
                            trigger_xrefresh=True,
                        )
                    else:
                        _apply_xprop_desktop_atoms(wid)
                        make_click_through(wid)
                    lowered.append(wid)

            if lowered:
                force_desktop_repaint(
                    lower_wids=lowered,
                    restore_focus_wid=restore_focus_wid if first_pass else None,
                    trigger_xrefresh=first_pass,
                )
            first_pass = False
            if not any_alive:
                break
            time.sleep(interval)

    threading.Thread(target=_worker, daemon=True, name="linwall-x11-enforcer").start()


def has_maximized_or_fullscreen_window(exclude_pids: Iterable[int] = ()) -> bool:
    """Devuelve True si hay otra aplicación visible maximizada o en pantalla completa en el escritorio actual."""
    if not which("wmctrl") or not which("xprop"):
        return False

    excluded = {int(p) for p in exclude_pids}
    excluded.add(os.getpid())

    # 1) Escritorio virtual activo
    current_desktop = "0"
    res_d = _run(["wmctrl", "-d"], timeout=1.5)
    if res_d and res_d.returncode == 0:
        for line in res_d.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "*":
                current_desktop = parts[0]
                break

    # 2) Ventanas del escritorio actual
    res_w = _run(["wmctrl", "-lpx"], timeout=1.5)
    if not res_w or res_w.returncode != 0:
        return False

    for line in res_w.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 4:
            continue
        wid, desk, pid_str, wm_class = parts[0], parts[1], parts[2], parts[3].lower()
        title = parts[5].lower() if len(parts) >= 6 else ""

        if desk not in (current_desktop, "-1"):
            continue
        if pid_str.isdigit() and int(pid_str) in excluded:
            continue
        if any(k in wm_class for k in DESKTOP_AND_PANEL_CLASSES):
            continue
        if "linwall" in wm_class or "linux-wallpaperengine" in wm_class or "xwinwrap" in wm_class:
            continue
        if "linwall-wallpaper" in title or "linwall engine" in title:
            continue

        # 3) Comprobar átomos EWMH / ICCCM de visibilidad y maximizado/fullscreen
        xp = _run(["xprop", "-id", wid, "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE", "WM_STATE"], timeout=1.0)
        if not xp or xp.returncode != 0:
            continue
        out = xp.stdout
        if "_NET_WM_WINDOW_TYPE_DESKTOP" in out or "_NET_WM_WINDOW_TYPE_DOCK" in out:
            continue
        if "_NET_WM_STATE_HIDDEN" in out or "window state: Iconic" in out or "window state: Withdrawn" in out:
            continue
        is_fs = "_NET_WM_STATE_FULLSCREEN" in out
        is_max = "_NET_WM_STATE_MAXIMIZED_VERT" in out and "_NET_WM_STATE_MAXIMIZED_HORZ" in out
        if is_fs or is_max:
            return True

    return False


def is_desktop_or_linwall_focused(exclude_pids: Iterable[int] = ()) -> bool:
    """Devuelve ``True`` si la ventana activa es el escritorio vacío (``nemo-desktop``, ``xfdesktop``,
    ``caja``), el panel del sistema o la propia ventana de ``linwall``.
    Devuelve ``False`` cuando el usuario tiene enfocada cualquier otra aplicación (ej. Chromium, VSCode).
    """
    wid = get_active_window()
    parsed = _parse_wid(wid) if wid else None
    if parsed is None:
        # Sin ventana activa => el foco está en el escritorio raíz / vacío
        return True

    allowed_pids = {int(p) for p in exclude_pids}
    allowed_pids.add(os.getpid())

    if which("xprop"):
        xp = _run(
            [
                "xprop",
                "-id",
                hex(parsed),
                "WM_CLASS",
                "_NET_WM_WINDOW_TYPE",
                "_NET_WM_PID",
                "WM_NAME",
                "_NET_WM_NAME",
            ],
            timeout=1.2,
        )
        if not xp or xp.returncode != 0:
            return True
        out = xp.stdout
        out_low = out.lower()

        if "_net_wm_window_type_desktop" in out_low or "_net_wm_window_type_dock" in out_low:
            return True

        for line in out.splitlines():
            if line.startswith("_NET_WM_PID") and "=" in line:
                pid_val = line.split("=", 1)[1].strip()
                if pid_val.isdigit() and int(pid_val) in allowed_pids:
                    return True

        if any(cls_name in out_low for cls_name in DESKTOP_AND_PANEL_CLASSES):
            return True
        if "linwall" in out_low or "linux-wallpaperengine" in out_low or "xwinwrap" in out_low:
            return True
        return False

    return True

