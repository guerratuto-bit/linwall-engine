# =============================================================================
#  LinWall Engine v1.6.1 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Gestión del archivo ``~/.config/autostart/linwall-restore.desktop``."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

from linwall import paths

PACKAGE_PARENT = Path(__file__).resolve().parent.parent


def restore_command() -> str:
    """Comando ligero (sin GUI) que restaura el último fondo al iniciar sesión."""
    launcher = shutil.which("linwall") or str(Path.home() / ".local" / "bin" / "linwall")
    if os.access(launcher, os.X_OK):
        return f"{shlex.quote(launcher)} --restore --delay 4"
    python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    return (f"env PYTHONPATH={shlex.quote(str(PACKAGE_PARENT))} "
            f"{shlex.quote(python)} -m linwall --restore --delay 4")


def desktop_entry() -> str:
    icon_png = Path(__file__).resolve().parent / "gui" / "linwall.png"
    icon_val = str(icon_png) if icon_png.is_file() else "linwall"
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=LinWall Engine (restaurar fondo)",
        "Name[es]=LinWall Engine (restaurar fondo)",
        "Comment=Restaura el último fondo animado de Wallpaper Engine",
        f"Exec={restore_command()}",
        f"Icon={icon_val}",
        "Terminal=false",
        "NoDisplay=false",
        "Hidden=false",
        "X-GNOME-Autostart-enabled=true",
        "X-GNOME-Autostart-Delay=3",
        "X-MATE-Autostart-Delay=3",
        "",
    ])


def is_enabled() -> bool:
    return paths.AUTOSTART_FILE.is_file()


def enable() -> Path:
    paths.AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    tmp = paths.AUTOSTART_FILE.with_suffix(".tmp")
    tmp.write_text(desktop_entry(), encoding="utf-8")
    os.chmod(tmp, 0o755)
    os.replace(tmp, paths.AUTOSTART_FILE)
    return paths.AUTOSTART_FILE


def disable() -> None:
    try:
        paths.AUTOSTART_FILE.unlink()
    except FileNotFoundError:
        pass


def sync(enabled: bool) -> None:
    enable() if enabled else disable()
