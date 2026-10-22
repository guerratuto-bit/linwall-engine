# =============================================================================
#  LinWall Engine v1.6.1 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Rutas estándar (XDG) y descubrimiento de assets de Steam Wallpaper Engine."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

APP_NAME = "linwall"
WORKSHOP_APPID = "431960"

HOME = Path.home()


def _xdg(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else default


CONFIG_HOME = _xdg("XDG_CONFIG_HOME", HOME / ".config")
DATA_HOME = _xdg("XDG_DATA_HOME", HOME / ".local" / "share")
STATE_HOME = _xdg("XDG_STATE_HOME", HOME / ".local" / "state")
CACHE_HOME = _xdg("XDG_CACHE_HOME", HOME / ".cache")

CONFIG_DIR = CONFIG_HOME / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

STATE_DIR = STATE_HOME / APP_NAME
STATE_FILE = STATE_DIR / "state.json"
ENGINE_LOG = STATE_DIR / "engine.log"
APP_LOG = STATE_DIR / "linwall.log"

DATA_DIR = DATA_HOME / APP_NAME
BIN_DIR = DATA_DIR / "bin"
LWE_INSTALL_DIR = DATA_DIR / "linux-wallpaperengine"

CACHE_DIR = CACHE_HOME / APP_NAME
THUMB_DIR = CACHE_DIR / "thumbs"

AUTOSTART_DIR = CONFIG_HOME / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "linwall-restore.desktop"

_runtime = os.environ.get("XDG_RUNTIME_DIR")
RUNTIME_DIR = (Path(_runtime) if _runtime else Path(f"/tmp/linwall-{os.getuid()}")) / APP_NAME

# Raíces de Steam conocidas (nativo, enlace ~/.steam, Flatpak y Snap).
STEAM_ROOTS = [
    HOME / ".steam" / "steam",
    HOME / ".steam" / "root",
    HOME / ".local" / "share" / "Steam",
    HOME / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    HOME / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
    HOME / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
]

# Rutas de workshop exigidas explícitamente.
DEFAULT_WORKSHOP_DIRS = [
    root / "steamapps" / "workshop" / "content" / WORKSHOP_APPID for root in STEAM_ROOTS
]

# Rutas estándar de la carpeta oficial 'assets' de Wallpaper Engine en Steam.
DEFAULT_WE_ASSETS_DIRS = [
    HOME / ".steam" / "steam" / "steamapps" / "common" / "wallpaper_engine" / "assets",
    HOME / ".local" / "share" / "Steam" / "steamapps" / "common" / "wallpaper_engine" / "assets",
    HOME / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam" / "steamapps" / "common" / "wallpaper_engine" / "assets",
     *[root / "steamapps" / "common" / "wallpaper_engine" / "assets" for root in STEAM_ROOTS],
]

_VDF_PATH = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"', re.IGNORECASE)


def steam_library_roots() -> list[Path]:
    """Lee ``libraryfolders.vdf`` para encontrar bibliotecas de Steam en discos secundarios."""
    roots: list[Path] = []
    for root in STEAM_ROOTS:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            content = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _VDF_PATH.finditer(content):
            roots.append(Path(match.group(1).replace("\\\\", "\\")))
    return roots


def find_wallpaper_engine_assets_dir() -> Path | None:
    """Busca automáticamente la carpeta ``assets`` oficial de Wallpaper Engine en Steam."""
    candidates: list[Path] = list(DEFAULT_WE_ASSETS_DIRS)
    for lib in steam_library_roots():
        candidates.append(lib / "steamapps" / "common" / "wallpaper_engine" / "assets")
    candidates.append(LWE_INSTALL_DIR / "assets")
    candidates.append(Path("/usr/share/linux-wallpaperengine/assets"))

    for cand in candidates:
        try:
            if cand.is_dir():
                return cand.resolve()
        except OSError:
            continue
    return None


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, STATE_DIR, CACHE_DIR, THUMB_DIR, RUNTIME_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def which(name: str) -> str | None:
    """Busca un ejecutable en ``$PATH`` y después en ``~/.local/share/linwall/bin``."""
    found = shutil.which(name)
    if found:
        return found
    local = BIN_DIR / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return None
