# =============================================================================
#  LinWall Engine v1.6.1 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Carga y regeneración de miniaturas v1.6.1 con Pillow (frame intermedio n_frames // 2 en GIFs) y caché ~/.cache/linwall/thumbs/<id>.jpg."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

try:
    from PIL import Image, ImageOps, ImageStat
    _HAS_PIL = True
except Exception:  # noqa: BLE001
    _HAS_PIL = False

from linwall import paths  # noqa: E402

if TYPE_CHECKING:
    from linwall.scanner import Wallpaper

log = logging.getLogger(__name__)

MASTER_W, MASTER_H = 640, 360
VIDEO_LIKE_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v"}
CACHE_SCHEMA_VERSION = "1.5.0"
_CACHE_INIT_LOCK = threading.Lock()
_CACHE_INITIALIZED = False


def _ensure_cache_version() -> None:
    """Al iniciar la v1.6.1, limpia automáticamente miniaturas antiguas en caché para regenerarlas desde project.json."""
    global _CACHE_INITIALIZED
    if _CACHE_INITIALIZED:
        return
    with _CACHE_INIT_LOCK:
        if _CACHE_INITIALIZED:
            return
        _CACHE_INITIALIZED = True
        try:
            paths.THUMB_DIR.mkdir(parents=True, exist_ok=True)
            ver_file = paths.THUMB_DIR / ".version"
            current = ver_file.read_text(encoding="utf-8").strip() if ver_file.is_file() else ""
            if current != CACHE_SCHEMA_VERSION:
                for p in paths.THUMB_DIR.iterdir():
                    if p.is_file() and p.name != ".version":
                        try:
                            p.unlink()
                        except OSError:
                            pass
                ver_file.write_text(CACHE_SCHEMA_VERSION, encoding="utf-8")
        except OSError:
            pass


def cache_path_for(src: Path, wp_id: str | None = None) -> Path:
    """Ruta en caché ``~/.cache/linwall/thumbs/<id>.jpg``."""
    _ensure_cache_version()
    if wp_id:
        safe_id = "".join(c for c in str(wp_id) if c.isalnum() or c in ("-", "_"))
        if safe_id:
            return paths.THUMB_DIR / f"{safe_id}.jpg"
    try:
        st = src.stat()
        key = f"{src}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        key = str(src)
    return paths.THUMB_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".jpg")


def _is_frame_mostly_blank(rgb_img: "Image.Image") -> bool:
    """Detecta si un fotograma es un fundido negro o plano vacío."""
    try:
        stat = ImageStat.Stat(rgb_img)
        return max(stat.stddev[:3]) < 3.5
    except Exception:  # noqa: BLE001
        return False


def _render_gif_frame(img: "Image.Image", target_idx: int) -> "Image.Image":
    """Compone los fotogramas de un GIF/WebP hasta ``target_idx`` para evitar parches transparentes."""
    canvas = Image.new("RGB", img.size, (11, 17, 24))
    start_idx = 0 if target_idx <= 40 else max(0, target_idx - 15)
    if start_idx > 0:
        try:
            img.seek(0)
            f0 = img.convert("RGBA")
            canvas.paste(f0, mask=f0.split()[3])
        except Exception:  # noqa: BLE001
            pass
    for idx in range(start_idx, target_idx + 1):
        try:
            img.seek(idx)
            rgba = img.convert("RGBA")
            canvas.paste(rgba, mask=rgba.split()[3])
        except Exception:  # noqa: BLE001
            break
    return canvas


def _extract_with_pillow(src: Path, dest_jpg: Path) -> bool:
    """Extrae un frame representativo con Pillow (en GIFs animados usa ``n_frames // 2``)."""
    if not _HAS_PIL:
        return False
    try:
        with Image.open(src) as img:
            n_frames = int(getattr(img, "n_frames", 1) or 1)
            if n_frames > 1:
                # En muchos GIFs de Workshop el frame 0 está en negro; extraer el frame intermedio n_frames // 2.
                mid_idx = max(0, n_frames // 2)
                bg = _render_gif_frame(img, mid_idx)
                if _is_frame_mostly_blank(bg):
                    for alt_idx in (n_frames // 3, (2 * n_frames) // 3, min(5, n_frames - 1), 0):
                        cand = _render_gif_frame(img, alt_idx)
                        if not _is_frame_mostly_blank(cand):
                            bg = cand
                            break
            else:
                img.seek(0)
                frame = img.convert("RGBA")
                bg = Image.new("RGB", frame.size, (11, 17, 24))
                bg.paste(frame, mask=frame.split()[3])

            fitted = ImageOps.fit(bg, (MASTER_W, MASTER_H), method=Image.Resampling.LANCZOS)
            paths.THUMB_DIR.mkdir(parents=True, exist_ok=True)
            tmp = dest_jpg.with_name(f".{dest_jpg.name}.{os.getpid()}.tmp")
            fitted.save(tmp, "JPEG", quality=90, optimize=True)
            os.replace(tmp, dest_jpg)
            return dest_jpg.is_file() and dest_jpg.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        log.debug("Pillow no pudo extraer %s: %s", src, exc)
        return False


def _extract_with_video_tool(src: Path, dest_jpg: Path) -> bool:
    """Extrae un fotograma representativo de un vídeo o GIF mediante ffmpeg o mpv."""
    paths.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg = paths.which("ffmpeg")
    if ffmpeg:
        tmp = dest_jpg.with_name(f".{dest_jpg.name}.{os.getpid()}.ff.jpg")
        try:
            res = subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(src),
                    "-frames:v", "1",
                    "-vf", f"scale={MASTER_W}:{MASTER_H}:force_original_aspect_ratio=increase,crop={MASTER_W}:{MASTER_H}",
                    str(tmp),
                ],
                capture_output=True,
                timeout=8,
                check=False,
            )
            if res.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, dest_jpg)
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    mpv = paths.which("mpv")
    if mpv:
        try:
            with tempfile.TemporaryDirectory(prefix="linwall-thumb-") as tmpdir:
                subprocess.run(
                    [
                        mpv, "--no-config", "--no-audio", "--no-sub", "--aid=no", "--sid=no",
                        "--start=50%", "--frames=1", "--vo=image", "--vo-image-format=jpg",
                        f"--vo-image-outdir={tmpdir}", str(src),
                    ],
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                frames = sorted(Path(tmpdir).glob("*.jpg"))
                if frames:
                    if _extract_with_pillow(frames[0], dest_jpg):
                        return True
                    os.replace(frames[0], dest_jpg)
                    return dest_jpg.is_file()
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("mpv no pudo extraer miniatura de %s: %s", src, exc)
    return False


def _load_raw_pixbuf(src: Path, width: int, height: int) -> GdkPixbuf.Pixbuf | None:
    """Respaldo con GdkPixbuf para imágenes y animaciones."""
    path_str = str(src)
    try:
        anim = GdkPixbuf.PixbufAnimation.new_from_file(path_str)
        if anim:
            static = anim.get_static_image()
            if static:
                return static
    except GLib.Error:
        pass

    try:
        return GdkPixbuf.Pixbuf.new_from_file(path_str)
    except GLib.Error:
        pass

    try:
        loader = GdkPixbuf.PixbufLoader()
        try:
            loader.write(src.read_bytes())
        except GLib.Error:
            pass
        try:
            loader.close()
        except GLib.Error:
            pass
        return loader.get_pixbuf()
    except OSError:
        return None


def _ensure_cached_jpg(src: Path, cache: Path) -> bool:
    """Garantiza que exista ``cache`` (~/.cache/linwall/thumbs/<id>.jpg) actualizado."""
    try:
        if cache.is_file() and cache.stat().st_size > 0:
            if not src.is_file() or cache.stat().st_mtime >= src.stat().st_mtime:
                return True
    except OSError:
        pass

    if not src or not src.is_file():
        return False

    ext = src.suffix.lower()
    if ext in VIDEO_LIKE_EXTS:
        if _extract_with_video_tool(src, cache):
            return True
        if _extract_with_pillow(src, cache):
            return True
    else:
        if _extract_with_pillow(src, cache):
            return True
        if _extract_with_video_tool(src, cache):
            return True

    pb = _load_raw_pixbuf(src, MASTER_W, MASTER_H)
    if pb is not None:
        try:
            paths.THUMB_DIR.mkdir(parents=True, exist_ok=True)
            pb.savev(str(cache), "jpeg", ["quality"], ["90"])
            return cache.is_file()
        except GLib.Error:
            pass
    return False


def _scale_crop_pixbuf(pb: GdkPixbuf.Pixbuf, width: int, height: int) -> GdkPixbuf.Pixbuf | None:
    sw, sh = pb.get_width(), pb.get_height()
    if sw <= 0 or sh <= 0:
        return None
    if (sw, sh) != (width, height):
        scale = max(width / sw, height / sh)
        tw, th = max(width, round(sw * scale)), max(height, round(sh * scale))
        if (tw, th) != (sw, sh):
            scaled = pb.scale_simple(tw, th, GdkPixbuf.InterpType.BILINEAR)
            if scaled is not None:
                pb = scaled
    x = max(0, (pb.get_width() - width) // 2)
    y = max(0, (pb.get_height() - height) // 2)
    w = min(width, pb.get_width())
    h = min(height, pb.get_height())
    return pb.new_subpixbuf(x, y, w, h).copy()


def load_cover(
    src: Path | None,
    width: int,
    height: int,
    use_cache: bool = True,
    wp_id: str | None = None,
    fallback_sources: list[Path] | None = None,
) -> GdkPixbuf.Pixbuf | None:
    """Devuelve un pixbuf ``width``×``height`` cacheado en ``~/.cache/linwall/thumbs/<id>.jpg``."""
    _ensure_cache_version()
    candidates: list[Path] = []
    if src is not None:
        candidates.append(src)
    if fallback_sources:
        for fb in fallback_sources:
            if fb is not None and fb not in candidates:
                candidates.append(fb)

    for candidate in candidates:
        try:
            cache = cache_path_for(candidate, wp_id=wp_id) if use_cache else None
            if cache and _ensure_cached_jpg(candidate, cache):
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(cache), width, height, False)
                if pb is not None:
                    return pb
                pb_raw = GdkPixbuf.Pixbuf.new_from_file(str(cache))
                if pb_raw is not None:
                    return _scale_crop_pixbuf(pb_raw, width, height)

            pb_direct = _load_raw_pixbuf(candidate, width, height)
            if pb_direct is not None:
                return _scale_crop_pixbuf(pb_direct, width, height)
        except (GLib.Error, OSError, TypeError) as exc:
            log.debug("Miniatura falló con %s: %s", candidate, exc)
            continue
    return None


def load_wallpaper_cover(wp: "Wallpaper", width: int, height: int) -> GdkPixbuf.Pixbuf | None:
    """Carga la miniatura usando estrictamente ``preview_path`` de ``project.json``."""
    fallbacks: list[Path] = []
    if wp.preview_path is None and wp.fallback_media_path is not None:
        fallbacks.append(wp.fallback_media_path)
    return load_cover(
        wp.preview_path,
        width,
        height,
        use_cache=True,
        wp_id=wp.id,
        fallback_sources=fallbacks,
    )
