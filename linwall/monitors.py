# =============================================================================
#  LinWall Engine v1.6.0 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Detección de monitores mediante ``xrandr --listmonitors``."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# " 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1"
_LISTMON = re.compile(
    r"^\s*(\d+):\s+\+?(\*?)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)\s*$"
)
# "HDMI-1 connected primary 1920x1080+0+0 (normal ...)"
_QUERY = re.compile(
    r"^(\S+)\s+connected\s+(primary\s+)?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)"
)


@dataclass(frozen=True)
class Monitor:
    index: int
    name: str
    width: int
    height: int
    x: int
    y: int
    primary: bool = False

    @property
    def geometry(self) -> str:
        """Formato X11 ``WxH+X+Y`` (xwinwrap, mpv)."""
        return f"{self.width}x{self.height}+{self.x}+{self.y}"

    @property
    def lwe_geometry(self) -> str:
        """Formato ``XxYxWxH`` usado por ``linux-wallpaperengine --window``."""
        return f"{self.x}x{self.y}x{self.width}x{self.height}"

    @property
    def label(self) -> str:
        star = " ★" if self.primary else ""
        return f"{self.name} ({self.width}×{self.height}){star}"


def parse_listmonitors(text: str) -> list[Monitor]:
    monitors = []
    for line in text.splitlines():
        m = _LISTMON.match(line)
        if not m:
            continue
        idx, star, _, w, h, x, y, name = m.groups()
        monitors.append(Monitor(int(idx), name, int(w), int(h), int(x), int(y), bool(star)))
    return monitors


def parse_query(text: str) -> list[Monitor]:
    monitors = []
    for line in text.splitlines():
        m = _QUERY.match(line)
        if not m:
            continue
        name, primary, w, h, x, y = m.groups()
        monitors.append(Monitor(len(monitors), name, int(w), int(h), int(x), int(y), bool(primary)))
    return monitors


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def list_monitors() -> list[Monitor]:
    monitors = parse_listmonitors(_run(["xrandr", "--listmonitors"]))
    if not monitors:
        monitors = parse_query(_run(["xrandr", "--query"]))
    if monitors and not any(m.primary for m in monitors):
        first = monitors[0]
        monitors[0] = Monitor(first.index, first.name, first.width, first.height,
                              first.x, first.y, True)
    return monitors


def resolve(selection: str, monitors: list[Monitor] | None = None) -> list[Monitor]:
    """Traduce la selección de la UI (``all`` o nombre) en la lista de monitores destino."""
    monitors = monitors if monitors is not None else list_monitors()
    if not monitors:
        return []
    if selection == "all":
        return monitors
    chosen = [m for m in monitors if m.name == selection]
    if chosen:
        return chosen
    primary = [m for m in monitors if m.primary]
    return primary or monitors[:1]
