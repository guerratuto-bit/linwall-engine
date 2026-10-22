# =============================================================================
#  LinWall Engine v1.6.1 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Interfaz de línea de comandos.

    linwall                 abre la GUI
    linwall --restore       restaura el último fondo (usado por el autostart, sin GTK)
    linwall --apply ID      aplica un fondo por ID de workshop o ruta
    linwall --stop          detiene el fondo y restaura el escritorio
    linwall --list          lista los fondos encontrados
    linwall --status        muestra el estado del motor
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from linwall import (
    __ai_credits__,
    __app_name__,
    __author__,
    __country__,
    __email__,
    __license__,
    __version__,
    autostart,
    paths,
)
from linwall.config import Config


def _setup_logging(verbose: bool) -> None:
    paths.ensure_dirs()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(paths.APP_LOG, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _resolve_wallpaper(target: str, cfg: Config):
    from linwall.scanner import Wallpaper, find_by_id, parse_project

    p = Path(target).expanduser()
    if p.is_dir():
        return parse_project(p)
    wp = find_by_id(target, cfg.get("extra_dirs") or [])
    if wp:
        return wp
    last = cfg.get("last_wallpaper")
    if isinstance(last, dict) and str(last.get("id")) == target:
        return Wallpaper.from_dict(last)
    return None


def cmd_apply(target: str, cfg: Config, wait_desktop: bool = False) -> int:
    from linwall.engine import Engine, EngineError, PlaybackOptions

    wp = _resolve_wallpaper(target, cfg)
    if not wp:
        print(f"No se encontró el fondo «{target}».", file=sys.stderr)
        return 2
    try:
        warnings = Engine().apply(
            wp,
            PlaybackOptions.from_config(cfg, wp_id=wp.id),
            wait_desktop=wait_desktop,
        )
    except EngineError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"Aviso: {w}")
    cfg.update(last_wallpaper=wp.to_dict(), active=True, save=True)
    if cfg.get("autostart"):
        autostart.enable()
    print(f"Fondo aplicado: {wp.title} [{wp.type}] ({wp.id})")
    return 0


def cmd_restore(cfg: Config, delay: float) -> int:
    last = cfg.get("last_wallpaper")
    if not cfg.get("active") or not isinstance(last, dict):
        print("No hay ningún fondo activo que restaurar.")
        return 0
    if delay > 0:
        time.sleep(delay)
    target = last.get("path") if Path(str(last.get("path", ""))).is_dir() else str(last.get("id"))
    return cmd_apply(target, cfg, wait_desktop=True)


def cmd_stop(cfg: Config) -> int:
    from linwall.engine import Engine

    for msg in Engine().stop(restore_desktop=True):
        print(msg)
    cfg.update(active=False, save=True)
    print("Fondo detenido.")
    return 0


def cmd_list(cfg: Config) -> int:
    from linwall.scanner import find_workshop_dirs, scan

    dirs = find_workshop_dirs(cfg.get("extra_dirs") or [])
    if not dirs:
        print("No se encontró ninguna carpeta de Steam Workshop 431960.")
        return 1
    for d in dirs:
        print(f"# {d}")
    for wp in scan(cfg.get("extra_dirs") or []):
        print(f"{wp.id:>12}  {wp.type.upper():<6}  {wp.title}")
    return 0


def cmd_status(cfg: Config) -> int:
    from linwall.desktop import desktop_summary
    from linwall.engine import Engine

    eng = Engine()
    assets_dir = paths.find_wallpaper_engine_assets_dir()
    print(f"{__app_name__} v{__version__} ({__license__})")
    print(f"Autor      : {__author__} ({__country__}) <{__email__}>")
    print(f"IA / Arq.  : {__ai_credits__}")
    print(f"Escritorio : {desktop_summary()}")
    for name, path in eng.binaries().items():
        print(f"{name:<22}: {path or 'NO INSTALADO'}")
    print(f"{'Assets WE (Steam)':<22}: {assets_dir or 'NO ENCONTRADO'}")
    print(f"Motor activo: {'sí' if eng.is_running() else 'no'}"
          f"{' (' + str(eng.state.get('wallpaper_id')) + ')' if eng.is_running() else ''}")
    print(f"Autostart  : {'sí' if autostart.is_enabled() else 'no'}")
    print(f"Config     : {paths.CONFIG_FILE}")
    print(f"Log motor  : {paths.ENGINE_LOG}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="linwall", description=f"{__app_name__} {__version__}")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--restore", action="store_true", help="restaurar el último fondo (sin GUI)")
    group.add_argument("--apply", metavar="ID|RUTA", help="aplicar un fondo por ID o carpeta")
    group.add_argument("--stop", action="store_true", help="detener el fondo animado")
    group.add_argument("--list", action="store_true", help="listar fondos encontrados")
    group.add_argument("--status", action="store_true", help="estado del motor y dependencias")
    group.add_argument("--refresh-autostart", action="store_true",
                       help="regenerar el .desktop de autostart si está activo")
    parser.add_argument("--delay", type=float, default=0.0, help="segundos de espera antes de restaurar")
    parser.add_argument("--monitor", help="monitor destino (nombre xrandr o 'all')")
    parser.add_argument("--fps", type=int, choices=(30, 60))
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    cfg = Config.load()
    if args.monitor:
        cfg.set("monitor", args.monitor)
    if args.fps:
        cfg.set("fps", args.fps)

    if args.restore:
        return cmd_restore(cfg, args.delay)
    if args.apply:
        return cmd_apply(args.apply, cfg)
    if args.stop:
        return cmd_stop(cfg)
    if args.list:
        return cmd_list(cfg)
    if args.status:
        return cmd_status(cfg)
    if args.refresh_autostart:
        if cfg.get("autostart") and autostart.is_enabled():
            autostart.enable()
        return 0

    try:
        from linwall.gui.app import run_gui
    except (ImportError, ValueError) as exc:
        print("No se pudo cargar GTK 3 (PyGObject). Instala: "
              "sudo apt install python3-gi gir1.2-gtk-3.0\n" f"Detalle: {exc}", file=sys.stderr)
        return 1
    return run_gui(cfg)
