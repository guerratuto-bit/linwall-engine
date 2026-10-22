# =============================================================================
#  LinWall Engine v1.6.1 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Motor híbrido de renderizado v1.6.1.

* VIDEO  -> ``xwinwrap`` + ``mpv --loop-file=inf --keepaspect=yes --panscan=1.0 --scale=ewa_lanczossharp``
  (y filtro ``--lavfi-complex`` Blur-Pad 1080p + ``--panscan=0.3`` para ``preview.gif`` cuadrados o < 600px).
* SCENE / WEB -> ``linux-wallpaperengine`` con desempaquetador ``scene.pkg`` y limpiador de capas
  fantasma/invisibles en ``~/.cache/linwall/unpacked/<id>/``, inyección automática de ``--assets-dir``,
  Z-Order debajo de ``nemo-desktop``/``xfdesktop``, Modo Compatibilidad HD y prevención de condiciones de carrera.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linwall import monitors as mon
from linwall import paths, pkg, x11
from linwall.desktop import DesktopIconsManager, current_de, running_desktop_manager, session_type
from linwall.scanner import Wallpaper

log = logging.getLogger(__name__)

ENGINE_BINARIES = ("xwinwrap", "mpv", "linux-wallpaperengine")
# /proc/<pid>/comm se trunca a 15 caracteres.
_STRAY_COMMS = {"xwinwrap", "linux-wallpaper", "linux-wallpaperengine"}
MPV_MARKER = "--x11-name=linwall"
MAX_LOG_BYTES = 2 * 1024 * 1024
STATIC_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
COMPAT_NOTICE = "ℹ️ Usando modo de compatibilidad para esta escena"
FIX_SCENE_NOTICE = "✨ Escena desempaquetada y capas ocultas limpiadas en caché"

_LWE_ASSETS_FLAG_CACHE: str | None = None


class EngineError(RuntimeError):
    """Error presentable al usuario."""


@dataclass
class PlaybackOptions:
    volume: int = 50
    muted: bool = True
    fps: int = 30
    monitor: str = "all"
    layer_mode: str = "auto"
    hide_desktop_icons: bool = False
    engine_mode: str = "auto"  # "auto" | "fix_scene" | "compat"

    @classmethod
    def from_config(cls, cfg, wp_id: str | None = None) -> "PlaybackOptions":
        target_id = wp_id
        if not target_id:
            last = cfg.get("last_wallpaper")
            if isinstance(last, dict):
                target_id = str(last.get("id") or "")
        engine_mode = (
            cfg.get_wallpaper_mode(target_id)
            if hasattr(cfg, "get_wallpaper_mode")
            else "auto"
        )
        return cls(
            volume=int(cfg.get("volume", 50)),
            muted=bool(cfg.get("muted", True)),
            fps=int(cfg.get("fps", 30)),
            monitor=str(cfg.get("monitor", "all")),
            layer_mode=str(cfg.get("layer_mode", "auto")),
            hide_desktop_icons=bool(cfg.get("hide_desktop_icons", False)),
            engine_mode=engine_mode,
        )


def _detect_lwe_assets_flag(lwe_bin: str) -> str:
    """Detecta en ``linux-wallpaperengine --help`` el nombre exacto del flag de assets (``--assets-dir`` o ``--assets``)."""
    global _LWE_ASSETS_FLAG_CACHE
    if _LWE_ASSETS_FLAG_CACHE is not None:
        return _LWE_ASSETS_FLAG_CACHE
    flag = "--assets-dir"
    try:
        res = subprocess.run(
            [lwe_bin, "--help"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        if "--assets-dir" in out:
            flag = "--assets-dir"
        elif "--assets" in out:
            flag = "--assets"
    except (OSError, subprocess.SubprocessError):
        pass
    _LWE_ASSETS_FLAG_CACHE = flag
    return flag


def _prepare_lwe_assets(lwe_bin: str) -> tuple[list[str], Path | None]:
    """Localiza ``assets`` de Wallpaper Engine en Steam, crea symlink en ``LWE_INSTALL_DIR`` y devuelve ``(args, cwd)``."""
    assets_dir = paths.find_wallpaper_engine_assets_dir()
    lwe_dir = paths.LWE_INSTALL_DIR if paths.LWE_INSTALL_DIR.is_dir() else None

    if assets_dir and lwe_dir:
        link_target = lwe_dir / "assets"
        if not link_target.exists():
            try:
                link_target.symlink_to(assets_dir, target_is_directory=True)
            except OSError:
                pass

    cwd: Path | None = None
    if lwe_dir and lwe_dir.is_dir():
        cwd = lwe_dir
    elif assets_dir and assets_dir.parent.is_dir():
        cwd = assets_dir.parent

    if assets_dir:
        flag = _detect_lwe_assets_flag(lwe_bin)
        return [flag, str(assets_dir)], cwd
    return [], cwd


# ------------------------------------------------------------------ estado
def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def load_state() -> dict[str, Any]:
    try:
        with open(paths.STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    if state.get("boot_id") != _boot_id():
        state.pop("pgids", None)
        state.pop("ipc_sockets", None)
    return state


def save_state(state: dict[str, Any]) -> None:
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["boot_id"] = _boot_id()
    tmp = paths.STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, paths.STATE_FILE)


# --------------------------------------------------------------- procesos
def _reap_zombies() -> None:
    """Recolecta procesos hijos terminados para que no queden como zombis."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break
        except OSError:
            break


def _read_proc(pid: int) -> tuple[str, list[str], str]:
    """(comm, argv, estado) de un PID, o cadenas vacías si no existe."""
    base = Path("/proc") / str(pid)
    try:
        comm = (base / "comm").read_text().strip()
        argv = [a for a in (base / "cmdline").read_bytes().decode(errors="replace").split("\0") if a]
        stat = (base / "stat").read_text()
        st = stat.rsplit(")", 1)[1].split()[0] if ")" in stat else ""
        return comm, argv, st
    except (OSError, IndexError):
        return "", [], ""


def _is_engine_process(pid: int) -> bool:
    comm, argv, st = _read_proc(pid)
    if not comm or st == "Z":
        return False
    if comm in _STRAY_COMMS:
        return True
    if comm == "mpv" and _is_wallpaper_mpv(argv):
        return True
    joined = " ".join(argv)
    return "linux-wallpaperengine" in joined or "xwinwrap" in joined


def _is_wallpaper_mpv(argv: list[str]) -> bool:
    """Solo se consideran fondo las instancias de mpv incrustadas (-wid) o marcadas."""
    for a in argv:
        if a == MPV_MARKER or a in ("-wid", "--wid") or a.startswith(("--wid=", "-wid=")):
            return True
    return False


def _pgid_alive(pgid: int) -> bool:
    _reap_zombies()
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if os.getpgid(pid) == pgid and _read_proc(pid)[2] != "Z":
                return True
        except (ProcessLookupError, PermissionError):
            continue
    return False


def _group_pids(pgid: int, alive_only: bool = False) -> list[int]:
    _reap_zombies()
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                pid = int(entry.name)
                if os.getpgid(pid) == pgid:
                    if alive_only and _read_proc(pid)[2] == "Z":
                        continue
                    pids.append(pid)
            except (ProcessLookupError, PermissionError):
                continue
    return pids


def _find_user_engine_pids() -> list[int]:
    """Devuelve todos los PIDs vivos del usuario correspondientes a motores de fondo."""
    _reap_zombies()
    me = os.getpid()
    uid = os.getuid()
    victims: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        comm, argv, st = _read_proc(pid)
        if st == "Z" or not comm:
            continue
        if comm in _STRAY_COMMS or (comm == "mpv" and _is_wallpaper_mpv(argv)):
            victims.append(pid)
    return victims


def _cleanup_stale_locks() -> None:
    """Elimina lockfiles huérfanos de CEF/linux-wallpaperengine para evitar 'Cannot exclusively lock'."""
    home = Path.home()
    candidates = [
        home / ".cache" / "linux-wallpaperengine",
        home / ".config" / "linux-wallpaperengine",
        paths.STATE_DIR,
        paths.RUNTIME_DIR,
    ]
    lock_names = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile", ".lock")
    for base in candidates:
        if not base.is_dir():
            continue
        for name in lock_names:
            for p in base.rglob(name):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass


def stop_current_wallpaper(pgids: list[int] | None = None, grace: float = 0.25) -> int:
    """Detiene de forma atómica todo motor previo antes de iniciar uno nuevo."""
    target_pgids = list(pgids or [])
    for pgid in target_pgids:
        for sig in (signal.SIGCONT, signal.SIGTERM):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    victims = _find_user_engine_pids()
    for pid in victims:
        for sig in (signal.SIGCONT, signal.SIGTERM):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _find_user_engine_pids() and not any(_pgid_alive(g) for g in target_pgids):
            break
        time.sleep(0.05)

    for pgid in target_pgids:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    remaining = _find_user_engine_pids()
    if remaining and paths.which("killall"):
        subprocess.run(
            ["killall", "-q", "-9", "linux-wallpaperengine", "xwinwrap"],
            capture_output=True,
            timeout=2,
            check=False,
        )

    for pid in _find_user_engine_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    kill_deadline = time.monotonic() + 1.5
    while time.monotonic() < kill_deadline:
        alive = _find_user_engine_pids()
        if not alive:
            break
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.05)

    _reap_zombies()
    _cleanup_stale_locks()
    return len(victims)


# ------------------------------------------------------------------ motor
class Engine:
    def __init__(self) -> None:
        paths.ensure_dirs()
        self._lock = threading.RLock()
        self._gen_lock = threading.Lock()
        self._generation_id: int = 0
        self.state = load_state()
        self._paused: bool = False
        self._temp_muted: bool = False

    def _next_generation(self) -> int:
        with self._gen_lock:
            self._generation_id += 1
            return self._generation_id

    def _is_current_generation(self, gen: int) -> bool:
        with self._gen_lock:
            return gen == self._generation_id

    # --------------------------------------------------------- información
    @staticmethod
    def binaries() -> dict[str, str | None]:
        return {name: paths.which(name) for name in ENGINE_BINARIES}

    @staticmethod
    def effective_layer(mode: str) -> str:
        if mode in ("desktop", "root"):
            return mode
        return "root" if current_de() == "other" else "desktop"

    def is_running(self) -> bool:
        with self._lock:
            pgids = self.state.get("pgids") or []
            return any(_pgid_alive(p) for p in pgids)

    def is_paused(self) -> bool:
        return self._paused

    def is_temp_muted(self) -> bool:
        return self._temp_muted

    def _active_engine_pids(self) -> set[int]:
        pgids = list(self.state.get("pgids") or [])
        engine_pids = set(_find_user_engine_pids())
        for g in pgids:
            engine_pids.update(_group_pids(g, alive_only=True))
        return engine_pids

    def active_wallpaper_id(self) -> str | None:
        return self.state.get("wallpaper_id") if self.is_running() else None

    # ------------------------------------------- ahorro inteligente (SIGSTOP/SIGCONT)
    def set_paused(self, paused: bool) -> bool:
        """Envía SIGSTOP (pausar, 0% GPU/CPU) o SIGCONT (reanudar) a los procesos del motor activo."""
        with self._lock:
            pgids = list(self.state.get("pgids") or [])
            pids = _find_user_engine_pids()
            for g in pgids:
                for p in _group_pids(g, alive_only=True):
                    if p not in pids:
                        pids.append(p)
            if not pids and not pgids:
                self._paused = False
                return False

            sig = signal.SIGSTOP if paused else signal.SIGCONT
            for g in pgids:
                try:
                    os.killpg(g, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            for p in pids:
                try:
                    os.kill(p, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            self._paused = bool(paused)
            return self._paused

    def check_smart_pause(self, enabled: bool) -> bool:
        """Pausa con SIGSTOP si hay otra app maximizada/fullscreen; reanuda con SIGCONT al minimizarla."""
        if not enabled:
            if self._paused:
                self.set_paused(False)
            return False
        if not self.is_running():
            self._paused = False
            return False

        pgids = list(self.state.get("pgids") or [])
        engine_pids = set(_find_user_engine_pids())
        for g in pgids:
            engine_pids.update(_group_pids(g, alive_only=True))

        should_pause = x11.has_maximized_or_fullscreen_window(exclude_pids=engine_pids)
        if should_pause != self._paused:
            self.set_paused(should_pause)
        return self._paused

    def _apply_effective_audio(self, user_volume: int, user_muted: bool, temp_muted: bool) -> bool:
        """Aplica el estado efectivo de audio (combinando Mute del usuario y Auto-Mute por foco)
        tanto en ``mpv`` (IPC socket ``mute``/``volume``/``aid``) como en ``linux-wallpaperengine`` (``pactl``).
        """
        vol_clamped = max(0, min(100, int(user_volume)))
        effective_muted = bool(user_muted or temp_muted or vol_clamped == 0)
        effective_vol = 0 if effective_muted else vol_clamped

        ok = False
        sock = self.state.get("audio_socket")
        if sock and self.state.get("kind") == "video":
            if effective_muted:
                ok = _mpv_command(sock, ["set_property", "mute", True])
                _mpv_command(sock, ["set_property", "volume", 0])
                _mpv_command(sock, ["set_property", "aid", "no"])
            else:
                _mpv_command(sock, ["set_property", "aid", "auto"])
                ok = _mpv_command(sock, ["set_property", "volume", effective_vol])
                ok = _mpv_command(sock, ["set_property", "mute", False]) or ok

        pids = self._active_engine_pids()
        pulse_ok = _set_pulse_audio_state(pids, effective_muted, effective_vol)
        return ok or pulse_ok

    def check_auto_mute(self, enabled: bool, user_volume: int, user_muted: bool) -> bool:
        """WindowMonitor: si la ventana activa no es el escritorio (``nemo-desktop``/``xfdesktop``)
        ni ``linwall``, silencia temporalmente el motor para liberar el reloj de PulseAudio/PipeWire.
        Al volver al escritorio o a LinWall, restaura el audio.
        """
        if not self.is_running() or self._paused:
            self._temp_muted = False
            return False

        if not enabled or user_muted or int(user_volume) <= 0:
            if self._temp_muted:
                self._temp_muted = False
                self._apply_effective_audio(user_volume, user_muted, temp_muted=False)
            return False

        engine_pids = self._active_engine_pids()
        desktop_focused = x11.is_desktop_or_linwall_focused(exclude_pids=engine_pids)
        should_temp_mute = not desktop_focused
        if should_temp_mute != self._temp_muted:
            self._temp_muted = should_temp_mute
            self._apply_effective_audio(user_volume, user_muted, temp_muted=should_temp_mute)
        return self._temp_muted

    # ------------------------------------------------------------- control
    def _cleanup_processes_only(self) -> None:
        """Mata los motores activos (SIGTERM -> 0.25s -> SIGKILL) sin tocar los iconos del escritorio."""
        self._paused = False
        self._temp_muted = False
        pgids = list(self.state.get("pgids") or [])
        stop_current_wallpaper(pgids, grace=0.25)
        for sock in self.state.get("ipc_sockets") or []:
            try:
                os.unlink(sock)
            except OSError:
                pass
        self.state["pgids"] = []
        self.state["ipc_sockets"] = []
        self.state.pop("audio_socket", None)

    def stop(self, restore_desktop: bool = True, bump_generation: bool = True) -> list[str]:
        if bump_generation:
            self._next_generation()
        messages: list[str] = []
        with self._lock:
            self._cleanup_processes_only()
            for key in ("pgids", "ipc_sockets", "wallpaper_id", "kind", "audio_socket",
                        "started_at"):
                self.state.pop(key, None)
            if restore_desktop:
                msg = DesktopIconsManager().restore(self.state)
                if msg:
                    messages.append(msg)
            save_state(self.state)
        return messages

    def set_desktop_mode_live(self, hide_icons: bool, monitor_spec: str = "all") -> list[str]:
        """Alterna en TIEMPO REAL entre 'Mostrar Iconos' y 'Modo Inmersivo (Sin Iconos)'."""
        messages: list[str] = []
        with self._lock:
            desk = DesktopIconsManager()
            msg = desk.apply_live(hide_icons, self.state)
            if msg:
                messages.append(msg)
            save_state(self.state)

            pgids = list(self.state.get("pgids") or [])
            all_pids: list[int] = []
            for g in pgids:
                all_pids.extend(_group_pids(g, alive_only=True))
            wids = x11.windows_for_pids(all_pids, wm_class="linux-wallpaperengine") if all_pids else []
            targets = mon.resolve(monitor_spec)
            x11.force_desktop_repaint(lower_wids=wids, trigger_xrefresh=True)
            if all_pids and targets:
                pid_fn = lambda: [p for g in pgids for p in _group_pids(g, alive_only=True)]
                x11.enforce_desktop_layer_async(
                    [(pid_fn, targets[0], set())],
                    duration=3.0,
                    interval=0.2,
                )
        return messages

    def apply(self, wp: Wallpaper, opts: PlaybackOptions, wait_desktop: bool = False) -> list[str]:
        """Aplica un fondo de forma atómica con ``generation_id`` para cancelar intentos previos."""
        if not wp.is_supported:
            raise EngineError(f"Tipo de fondo no soportado: '{wp.type}'.")
        if not Path(wp.path).is_dir():
            raise EngineError(f"La carpeta del fondo no existe: {wp.path}")

        my_gen = self._next_generation()

        warnings: list[str] = []
        if session_type() == "wayland":
            warnings.append("Sesión Wayland detectada: xwinwrap/linux-wallpaperengine requieren X11. "
                            "Inicia sesión en «Cinnamon» (X11) para un resultado correcto.")
        if not os.environ.get("DISPLAY"):
            raise EngineError("No hay $DISPLAY: ejecuta LinWall dentro de una sesión gráfica X11.")

        with self._lock:
            if not self._is_current_generation(my_gen):
                return []

            self._rotate_log()
            self.stop(restore_desktop=False, bump_generation=False)

            targets = mon.resolve(opts.monitor)
            if not targets:
                raise EngineError("No se detectó ningún monitor con xrandr.")

            desk = DesktopIconsManager()
            if opts.hide_desktop_icons:
                if wait_desktop:
                    desk.wait_for_desktop()
                msg = desk.hide(self.state)
                if msg:
                    warnings.append(msg)
            else:
                msg = desk.show(self.state)
                if msg:
                    warnings.append(msg)

            layer = self.effective_layer(opts.layer_mode)
            self.state.update(pgids=[], ipc_sockets=[], wallpaper_id=wp.id, kind=wp.type,
                              started_at=time.time(), layer=layer)
            save_state(self.state)
            try:
                if wp.type == "video":
                    warnings += self._start_video(wp, opts, targets, layer)
                elif opts.engine_mode == "compat":
                    fallback = self._resolve_fallback_media(wp)
                    if fallback and paths.which("mpv"):
                        warnings.append(COMPAT_NOTICE)
                        warnings += self._start_video(wp, opts, targets, "desktop", media_override=fallback)
                    else:
                        warnings += self._start_lwe(wp, opts, targets, layer, my_gen=my_gen)
                else:
                    warnings += self._start_lwe(wp, opts, targets, layer, my_gen=my_gen)
            except EngineError:
                if self._is_current_generation(my_gen):
                    self.stop(restore_desktop=True, bump_generation=False)
                raise
            save_state(self.state)

            if not self._is_current_generation(my_gen):
                return warnings

            time.sleep(0.25)
            if self._is_current_generation(my_gen) and not self.is_running():
                tail = self.log_tail()
                self.stop(restore_desktop=True, bump_generation=False)
                raise EngineError("El motor se cerró inesperadamente.\n\nÚltimas líneas del log:\n" + tail)
        return warnings

    @staticmethod
    def _inspect_media_geometry(media: Path) -> tuple[int, int, int]:
        """Devuelve ``(width, height, n_frames)`` usando Pillow o ``(0, 0, 1)`` si no es imagen/GIF."""
        try:
            from PIL import Image
            with Image.open(media) as img:
                w, h = img.size
                n_frames = int(getattr(img, "n_frames", 1) or 1)
                return int(w), int(h), n_frames
        except Exception:  # noqa: BLE001
            return 0, 0, 1

    @classmethod
    def _is_lowres_or_square_preview(cls, media: Path) -> tuple[bool, int]:
        """Detecta si ``media`` es un ``preview.gif`` / imagen de baja resolución (< 600px) o cuadrada 1:1."""
        ext = media.suffix.lower()
        if ext != ".gif" and ext not in STATIC_IMAGE_EXTENSIONS:
            return False, 1
        w, h, n_frames = cls._inspect_media_geometry(media)
        if w <= 0 or h <= 0:
            return False, n_frames
        is_square = abs(w - h) <= max(w, h) * 0.15
        is_lowres = w < 600 or h < 450
        return (is_lowres or is_square), n_frames

    @classmethod
    def _resolve_fallback_media(cls, wp: Wallpaper) -> Path | None:
        """Antes de usar ``preview.gif`` de baja resolución, intenta extraer la textura HD desde ``scene.pkg``."""
        raw_fb = wp.fallback_media_path
        if raw_fb and raw_fb.is_file():
            lowres_or_sq, _ = cls._is_lowres_or_square_preview(raw_fb)
            if not lowres_or_sq:
                return raw_fb
        hd_tex = pkg.extract_hd_texture_from_pkg(Path(wp.path), wp.id)
        if hd_tex and hd_tex.is_file():
            return hd_tex
        return raw_fb

    def set_volume(self, volume: int, muted: bool) -> bool:
        """Cambio de volumen en caliente (vía IPC para mpv y vía pactl para linux-wallpaperengine)."""
        if self._paused:
            self.set_paused(False)
        self._temp_muted = False
        return self._apply_effective_audio(volume, muted, temp_muted=False)

    def set_fps(self, fps: int) -> bool:
        """Cambio de FPS en caliente para mpv vía IPC (para SCENE se recarga en caliente)."""
        if self._paused:
            self.set_paused(False)
        if self.state.get("kind") != "video":
            return False
        socks = list(self.state.get("ipc_sockets") or [])
        if not socks:
            return False
        vf_val = "fps=30" if int(fps) == 30 else ""
        all_ok = True
        for sock in socks:
            if not _mpv_command(sock, ["set_property", "vf", vf_val]):
                all_ok = False
        return all_ok

    # ----------------------------------------------------------- lanzadores
    def _spawn(self, argv: list[str], cwd: Path | None = None) -> int:
        log.info("Lanzando: %s (cwd=%s)", " ".join(argv), cwd)
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PATH"] = f"{paths.BIN_DIR}:{env.get('PATH', '')}"
        run_cwd = str(cwd) if cwd and cwd.is_dir() else None
        with open(paths.ENGINE_LOG, "ab") as logfh:
            logfh.write(f"\n=== {time.strftime('%F %T')} :: {' '.join(argv)}\n".encode())
            logfh.flush()
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=logfh,
                    stderr=logfh,
                    start_new_session=True,
                    close_fds=True,
                    env=env,
                    cwd=run_cwd,
                )
            except OSError as exc:
                raise EngineError(f"No se pudo ejecutar {argv[0]}: {exc}") from exc
        self.state.setdefault("pgids", []).append(proc.pid)
        threading.Thread(target=proc.wait, daemon=True).start()
        return proc.pid

    def _mpv_args(self, media: Path, opts: PlaybackOptions, audio: bool, ipc: str) -> list[str]:
        ext = media.suffix.lower()
        is_gif_or_img = ext == ".gif" or ext in STATIC_IMAGE_EXTENSIONS
        is_lowres_square, n_frames = (
            self._is_lowres_or_square_preview(media) if is_gif_or_img else (False, 1)
        )
        panscan_val = "0.3" if is_lowres_square else "1.0"
        args = [
            "--profile=fast",
            "--loop", "--loop-file=inf", "--loop-playlist=inf",
            "--no-osc", "--no-osd-bar", "--osd-level=0",
            "--no-input-default-bindings", "--input-vo-keyboard=no", "--no-input-cursor",
            "--cursor-autohide=always", "--stop-screensaver=no", "--no-border",
            "--keepaspect=yes", f"--panscan={panscan_val}",
            "--scale=ewa_lanczossharp", "--cscale=ewa_lanczossharp",
            "--msg-level=all=warn",
            "--title=linwall-wallpaper", MPV_MARKER, f"--input-ipc-server={ipc}",
            "--force-window=yes", "--hr-seek=no",
            "--framedrop=vo", "--audio-pitch-correction=yes", "--audio-stream-silence=no",
        ]
        if is_gif_or_img:
            args += ["--hwdec=no"]
            if n_frames <= 1 and ext != ".gif":
                args += ["--image-display-duration=inf"]
            if is_lowres_square:
                # Filtro Blur-Pad 1080p: fondo desenfocado a pantalla completa + GIF nítido centrado
                args += [
                    "--lavfi-complex=[vid1]split[bg][fg];"
                    "[bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=25:5[bgblur];"
                    "[fg]scale=-1:1080[fgscale];"
                    "[bgblur][fgscale]overlay=(W-w)/2:(H-h)/2[vo]"
                ]
            elif opts.fps == 30 and ext == ".gif":
                args += ["--vf=fps=30"]
        else:
            args += ["--hwdec=auto-safe"]
            if opts.fps == 30:
                args += ["--vf=fps=30"]

        if audio and not is_gif_or_img:
            args += [f"--volume={opts.volume}", f"--mute={'yes' if opts.muted else 'no'}",
                     "--audio-display=no"]
            if opts.muted or opts.volume == 0:
                args += ["--aid=no"]
        else:
            args += ["--no-audio"]
        return args

    def _start_video(
        self,
        wp: Wallpaper,
        opts: PlaybackOptions,
        targets: list[mon.Monitor],
        layer: str,
        media_override: Path | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        mpv = paths.which("mpv")
        if not mpv:
            raise EngineError("mpv no está instalado. Ejecuta ./install.sh o «sudo apt install mpv».")
        video = media_override or wp.file_path or self._resolve_fallback_media(wp)
        if not video or not video.is_file():
            raise EngineError(f"No se encontró el archivo multimedia «{wp.file}» en {wp.path}.")
        xwinwrap = paths.which("xwinwrap")
        if not xwinwrap:
            warnings.append("xwinwrap no encontrado: usando mpv anclado como ventana de escritorio.")

        use_desktop_flag = (layer == "desktop") or (running_desktop_manager() is not None)

        existing = x11.list_all_windows() if not xwinwrap else set()
        fallback_pids: list[tuple[int, mon.Monitor]] = []
        for i, m in enumerate(targets):
            ipc = str(paths.RUNTIME_DIR / f"mpv-{m.name}.sock")
            self.state.setdefault("ipc_sockets", []).append(ipc)
            audio = i == 0
            if audio:
                self.state["audio_socket"] = ipc
            margs = self._mpv_args(video, opts, audio, ipc)
            if xwinwrap:
                wflags = ["-g", m.geometry, "-ni", "-s", "-st", "-sp", "-nf", "-b", "-un"]
                wflags += ["-fdt"] if use_desktop_flag else ["-ov"]
                argv = [xwinwrap, *wflags, "--", mpv, "-wid", "WID", *margs, str(video)]
                self._spawn(argv)
            else:
                argv = [mpv, f"--geometry={m.geometry}", "--ontop=no", "--keepaspect-window=no",
                        *margs, str(video)]
                fallback_pids.append((self._spawn(argv), m))

        for pid, m in fallback_pids:
            wins = x11.wait_for_windows(
                lambda p=pid: _group_pids(p, alive_only=True), 1, 10.0, exclude_wids=existing
            )
            if not wins:
                warnings.append(f"No se encontró la ventana de mpv en {m.name}.")
            for wid in wins:
                x11.pin_to_desktop(wid, m, lower_window=True, remap=True, trigger_xrefresh=True)
        x11.force_desktop_repaint(trigger_xrefresh=False)
        self.state["kind"] = "video"
        return warnings

    def _lwe_common(
        self,
        opts: PlaybackOptions,
        assets_args: list[str] | None = None,
        force_safe: bool = False,
    ) -> list[str]:
        fps = 30 if force_safe else opts.fps
        args: list[str] = []
        if assets_args:
            args.extend(assets_args)
        args += ["--fps", str(fps), "--no-fullscreen-pause", "--scaling", "fill"]
        if force_safe or opts.muted or opts.volume == 0:
            args += ["--silent", "--no-audio-processing"]
        else:
            args += ["--volume", str(round(opts.volume * 128 / 100))]
        return args

    def _start_lwe_window_mode(
        self,
        lwe: str,
        wp: Wallpaper,
        opts: PlaybackOptions,
        targets: list[mon.Monitor],
        common: list[str],
        assets_args: list[str] | None = None,
        cwd: Path | None = None,
        survival_wait: float = 0.45,
        my_gen: int | None = None,
        project_path: str | None = None,
    ) -> bool:
        """Lanza linux-wallpaperengine con --assets-dir y lo sitúa DEBAJO de nemo-desktop/xfdesktop."""
        if my_gen is not None and not self._is_current_generation(my_gen):
            return False

        target_path = project_path or wp.path
        active_wid = x11.get_active_window()
        spawned: list[tuple[int, mon.Monitor, set[int]]] = []
        for i, m in enumerate(targets):
            existing = x11.list_all_windows()
            local = list(common)
            if i > 0 and "--silent" not in local:
                local = list(assets_args or []) + [
                    "--fps", str(opts.fps), "--no-fullscreen-pause", "--scaling", "fill",
                    "--silent", "--no-audio-processing",
                ]
            argv = [lwe, *local, "--window", m.lwe_geometry, target_path]
            spawned.append((self._spawn(argv, cwd=cwd), m, existing))

        all_pinned = True
        async_targets: list[tuple[Any, mon.Monitor, set[int]]] = []
        for pid, m, existing in spawned:
            pid_fn = lambda p=pid: _group_pids(p, alive_only=True)
            wins = x11.wait_for_windows(
                pid_fn,
                expected=1,
                timeout=8.0,
                wm_class="linux-wallpaperengine",
                exclude_wids=existing,
            )
            if not wins or not _pgid_alive(pid):
                all_pinned = False
                continue
            for wid in wins:
                x11.pin_to_desktop(
                    wid,
                    m,
                    lower_window=True,
                    remap=True,
                    restore_focus_wid=active_wid,
                    trigger_xrefresh=True,
                )
            async_targets.append((pid_fn, m, existing))

        if not all_pinned or not self.is_running():
            return False

        if async_targets:
            x11.enforce_desktop_layer_async(
                async_targets,
                restore_focus_wid=active_wid,
                duration=3.0,
                interval=0.2,
            )

        deadline = time.monotonic() + survival_wait
        while time.monotonic() < deadline:
            if my_gen is not None and not self._is_current_generation(my_gen):
                return False
            if not self.is_running():
                return False
            time.sleep(0.08)
        return self.is_running()

    def _start_lwe(
        self,
        wp: Wallpaper,
        opts: PlaybackOptions,
        targets: list[mon.Monitor],
        layer: str,
        my_gen: int | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        lwe = paths.which("linux-wallpaperengine")
        if not lwe:
            fallback = self._resolve_fallback_media(wp)
            if fallback and paths.which("mpv"):
                warnings.append(COMPAT_NOTICE)
                return warnings + self._start_video(wp, opts, targets, layer, media_override=fallback)
            raise EngineError(
                "linux-wallpaperengine no está instalado.\n"
                "Ejecuta ./install.sh para compilarlo automáticamente "
                "(los fondos de tipo VIDEO funcionan sin él)."
            )

        assets_args, lwe_cwd = _prepare_lwe_assets(lwe)
        common = self._lwe_common(opts, assets_args=assets_args, force_safe=False)
        dm = running_desktop_manager()

        # Desempaquetar scene.pkg y limpiar capas invisibles/fantasma en ~/.cache/linwall/unpacked/<id>/
        project_path = wp.path
        use_fix_scene = (opts.engine_mode == "fix_scene") or (str(wp.id) in pkg.AUTO_FIX_SCENE_IDS)
        if wp.type == "scene":
            unpacked_dir = pkg.prepare_unpacked_scene(
                Path(wp.path),
                wp.id,
                force_aggressive=use_fix_scene,
            )
            if unpacked_dir and unpacked_dir.is_dir():
                project_path = str(unpacked_dir)
                if use_fix_scene:
                    warnings.append(FIX_SCENE_NOTICE)

        try_root_first = (layer == "root") and (opts.hide_desktop_icons or dm is None)
        if try_root_first:
            argv = [lwe, *common]
            for m in targets:
                argv += ["--screen-root", m.name, "--bg", project_path]
            self._spawn(argv, cwd=lwe_cwd)
            time.sleep(0.6)
            if self.is_running():
                x11.force_desktop_repaint(trigger_xrefresh=True)
                return warnings
            log.warning("--screen-root se cerró; activando fallback automático a --window DESKTOP")
            self._cleanup_processes_only()

        # Intento 1: Modo ventana sin bordes con --assets-dir anclada DEBAJO de nemo-desktop.
        if self._start_lwe_window_mode(
            lwe, wp, opts, targets, common,
            assets_args=assets_args, cwd=lwe_cwd, survival_wait=0.45,
            my_gen=my_gen, project_path=project_path,
        ):
            return warnings

        if my_gen is not None and not self._is_current_generation(my_gen):
            return warnings

        # Intento 2: Reintento seguro (--silent --no-audio-processing --fps 30 + --assets-dir).
        log.warning(
            "linux-wallpaperengine se cerró en el primer intento para %s; "
            "reintentando en modo seguro (--silent --no-audio-processing --fps 30)",
            wp.id,
        )
        self._cleanup_processes_only()
        safe_common = self._lwe_common(opts, assets_args=assets_args, force_safe=True)
        if self._start_lwe_window_mode(
            lwe, wp, opts, targets, safe_common,
            assets_args=assets_args, cwd=lwe_cwd, survival_wait=1.2,
            my_gen=my_gen, project_path=project_path,
        ):
            return warnings

        # Si habíamos usado la ruta desempaquetada y falló, probar también la ruta original del Workshop
        if project_path != wp.path:
            self._cleanup_processes_only()
            if self._start_lwe_window_mode(
                lwe, wp, opts, targets, safe_common,
                assets_args=assets_args, cwd=lwe_cwd, survival_wait=1.0,
                my_gen=my_gen, project_path=wp.path,
            ):
                return warnings

        if my_gen is not None and not self._is_current_generation(my_gen):
            return warnings

        # Fallback final: Modo de compatibilidad HD / Blur-Pad con xwinwrap + mpv
        self._cleanup_processes_only()
        fallback = self._resolve_fallback_media(wp)
        if fallback and paths.which("mpv"):
            log.info("Activando modo de compatibilidad (mpv) para %s con %s", wp.id, fallback)
            warnings.append(COMPAT_NOTICE)
            warnings += self._start_video(wp, opts, targets, "desktop", media_override=fallback)
            return warnings

        return warnings

    # ----------------------------------------------------------------- log
    @staticmethod
    def _rotate_log() -> None:
        try:
            if paths.ENGINE_LOG.stat().st_size > MAX_LOG_BYTES:
                paths.ENGINE_LOG.replace(paths.ENGINE_LOG.with_suffix(".log.1"))
        except OSError:
            pass

    @staticmethod
    def log_tail(lines: int = 15) -> str:
        try:
            content = paths.ENGINE_LOG.read_text(errors="replace").splitlines()
        except OSError:
            return "(sin log)"
        return "\n".join(content[-lines:])


def _mpv_command(sock_path: str, command: list[Any]) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(sock_path)
            s.sendall((json.dumps({"command": command}) + "\n").encode())
            data = s.recv(4096)
        return b'"error":"success"' in data.replace(b" ", b"")
    except OSError:
        return False


def _set_pulse_audio_state(pids: set[int], muted: bool, volume: int) -> bool:
    """Silencia o ajusta el volumen a nivel PulseAudio/PipeWire vía ``pactl`` para las entradas
    de audio pertenecientes a ``linux-wallpaperengine`` o ``mpv`` (``linwall``).
    """
    pactl = paths.which("pactl")
    if not pactl:
        return False

    env = os.environ.copy()
    env["LC_ALL"] = "C"
    try:
        res = subprocess.run(
            [pactl, "list", "sink-inputs"],
            capture_output=True,
            text=True,
            timeout=1.5,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    if res.returncode != 0 or not res.stdout:
        return False

    pid_tokens = {f'application.process.id = "{p}"' for p in pids}
    matched_ids: list[str] = []

    for block in res.stdout.split("Sink Input #")[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        sink_id = lines[0].strip()
        if not sink_id.isdigit():
            continue
        low = block.lower()
        is_match = (
            any(tok in block for tok in pid_tokens)
            or "linux-wallpaperengine" in low
            or "linwall-wallpaper" in low
        )
        if is_match:
            matched_ids.append(sink_id)

    if not matched_ids:
        return False

    mute_arg = "1" if muted or volume <= 0 else "0"
    vol_arg = "0%" if muted or volume <= 0 else f"{max(0, min(100, int(volume)))}%"
    any_ok = False
    for sid in matched_ids:
        try:
            r1 = subprocess.run(
                [pactl, "set-sink-input-mute", sid, mute_arg],
                capture_output=True,
                timeout=1.0,
                env=env,
                check=False,
            )
            r2 = subprocess.run(
                [pactl, "set-sink-input-volume", sid, vol_arg],
                capture_output=True,
                timeout=1.0,
                env=env,
                check=False,
            )
            if r1.returncode == 0 or r2.returncode == 0:
                any_ok = True
        except (OSError, subprocess.SubprocessError):
            continue
    return any_ok

