# =============================================================================
#  LinWall Engine v1.6.0 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Desempaquetador de archivos ``scene.pkg`` (formato PKGV de Wallpaper Engine) y
sanitizador de ``scene.json`` para eliminar capas fantasma / objetos invisibles
(como en *Yuta Okkotsu* ID ``2867422409``).

Cómo funciona:
1. Lee el contenedor binario ``scene.pkg`` (cabecera ``PKGV0001``..``PKGV0025``, tabla de
   archivos con offsets/tamaños y bloque de datos).
2. Extrae ``scene.json``, ``materials/``, ``models/``, ``effects/``, ``shaders/``,
   ``particles/`` y ``sounds/`` en ``~/.cache/linwall/unpacked/<id>/``.
3. Limpia ``scene.json`` eliminando:
   * Objetos con ``"visible": false`` o ``"visible": {"user": ..., "value": false}`` o
     ``"visible": {"script": ..., "value": false}``.
   * Objetos con ``"alpha": 0`` (o ``{"value": 0}``) o ``"scale": "0 0 0"``.
   * **Propagación jerárquica de padres (``"parent"``)**: en Wallpaper Engine (Windows),
     ocultar una carpeta/grupo oculta todos sus hijos (que conservan ``"visible": true``).
     ``linux-wallpaperengine`` no propaga la visibilidad del padre y dibuja las capas
     hijas ocultas (cabezas de repuesto, expresiones alternativas, mechones sueltos).
     Este sanitizador elimina recursivamente todo objeto cuyo ancestro esté oculto.
   * Capas duplicadas o sprites de repuesto desplazados hacia el margen derecho fuera del
     personaje principal (caso *Yuta Okkotsu* ``2867422409``).
   * Efectos desactivados (``"visible": false``) dentro de ``obj["effects"]``.
4. Re-empaqueta ``scene.pkg`` limpio y guarda ``scene.json`` y ``project.json`` en
   ``~/.cache/linwall/unpacked/<id>/`` para que ``linux-wallpaperengine`` renderice la
   escena en máxima calidad OpenGL sin capas fantasma.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import struct
from pathlib import Path
from typing import Any

from linwall import paths
from linwall.scanner import read_json_tolerant

log = logging.getLogger(__name__)

UNPACKED_CACHE_VERSION = "1.6.2"
AUTO_FIX_SCENE_IDS = {"2867422409"}  # Yuta Okkotsu y escenas con capas de repuesto


def _parse_vec(val: Any, default: tuple[float, ...] = (0.0, 0.0, 0.0)) -> tuple[float, ...]:
    if isinstance(val, dict):
        val = val.get("value", "")
    if isinstance(val, (list, tuple)):
        try:
            return tuple(float(x) for x in val)
        except (TypeError, ValueError):
            return default
    if isinstance(val, str):
        parts = val.strip().split()
        try:
            return tuple(float(x) for x in parts) if parts else default
        except ValueError:
            return default
    return default


def _is_prop_false(prop: Any) -> bool:
    """Devuelve True si una propiedad booleana (o dict user/script de WE) evalúa a False."""
    if prop is False or prop == 0:
        return True
    if isinstance(prop, str) and prop.strip().lower() in ("false", "0", "no"):
        return True
    if isinstance(prop, dict):
        if "value" in prop:
            return _is_prop_false(prop["value"])
        # Si tiene script pero no value explícito y el script oculta por defecto
        return False
    return False


def _is_alpha_zero(prop: Any) -> bool:
    """Devuelve True si la propiedad ``alpha`` es 0 (totalmente transparente)."""
    if prop is None:
        return False
    if isinstance(prop, dict):
        if "value" not in prop:
            return False
        prop = prop["value"]
    try:
        return float(prop) <= 0.005
    except (TypeError, ValueError):
        return False


def _is_scale_zero(prop: Any) -> bool:
    if prop is None:
        return False
    vec = _parse_vec(prop, (1.0, 1.0, 1.0))
    if len(vec) >= 2 and (abs(vec[0]) <= 0.001 or abs(vec[1]) <= 0.001):
        return True
    return False


def _is_spare_or_backup_name(name: str) -> bool:
    low = (name or "").strip().casefold()
    if not low:
        return False
    spare_tokens = (
        "backup", "unused", "hidden", "delete", "temp_layer", "test_layer",
        " reference", "ref_only", "copia", "oculto",
    )
    return any(tok in low for tok in spare_tokens)


def unpack_pkg_to_memory(pkg_path: Path) -> tuple[bytes, list[tuple[str, bytes]]] | None:
    """Lee un archivo ``.pkg`` (PKGV) y devuelve ``(magic_bytes, [(rel_path, data_bytes), ...])``."""
    try:
        raw = pkg_path.read_bytes()
    except OSError as exc:
        log.debug("No se pudo leer %s: %s", pkg_path, exc)
        return None

    total = len(raw)
    if total < 12:
        return None

    pos = 0
    try:
        (magic_len,) = struct.unpack_from("<I", raw, pos)
        pos += 4
        if magic_len < 4 or magic_len > 32 or pos + magic_len + 4 > total:
            return None
        magic = raw[pos : pos + magic_len]
        pos += magic_len
        if not magic.startswith(b"PKGV"):
            return None

        (entry_count,) = struct.unpack_from("<I", raw, pos)
        pos += 4
        if entry_count > 100000:
            return None

        table: list[tuple[str, int, int]] = []
        for _ in range(entry_count):
            if pos + 4 > total:
                return None
            (name_len,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            if name_len <= 0 or name_len > 4096 or pos + name_len + 8 > total:
                return None
            name = raw[pos : pos + name_len].decode("utf-8", errors="replace")
            pos += name_len
            offset, size = struct.unpack_from("<II", raw, pos)
            pos += 8
            table.append((name, offset, size))

        data_base = pos
        files: list[tuple[str, bytes]] = []
        for name, offset, size in table:
            start = data_base + offset
            end = start + size
            if start < data_base or end > total:
                log.debug("Entrada corrupta en %s: %s (%d..%d > %d)", pkg_path, name, start, end, total)
                continue
            files.append((name, raw[start:end]))
        return magic, files
    except struct.error as exc:
        log.debug("Error desempaquetando %s: %s", pkg_path, exc)
        return None


def pack_pkg_from_memory(dest_pkg: Path, magic: bytes, files: list[tuple[str, bytes]]) -> bool:
    """Empaqueta ``[(rel_path, data_bytes), ...]`` en formato ``PKGV`` estándar."""
    try:
        header_parts: list[bytes] = [
            struct.pack("<I", len(magic)),
            magic,
            struct.pack("<I", len(files)),
        ]
        offset = 0
        blobs: list[bytes] = []
        for name, data in files:
            name_bytes = name.encode("utf-8")
            header_parts.append(struct.pack("<I", len(name_bytes)))
            header_parts.append(name_bytes)
            header_parts.append(struct.pack("<II", offset, len(data)))
            blobs.append(data)
            offset += len(data)

        dest_pkg.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_pkg.with_name(f".{dest_pkg.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            for part in header_parts:
                fh.write(part)
            for blob in blobs:
                fh.write(blob)
        os.replace(tmp, dest_pkg)
        return True
    except OSError as exc:
        log.debug("No se pudo escribir %s: %s", dest_pkg, exc)
        return False


def sanitize_scene_dict(
    scene: dict[str, Any],
    wp_id: str = "",
    aggressive: bool = False,
) -> tuple[dict[str, Any], int]:
    """Limpia ``scene.json`` eliminando capas invisibles, hijos de grupos ocultos y capas fantasma.

    Devuelve ``(scene_limpio, num_cambios)``.
    """
    objects = scene.get("objects")
    if not isinstance(objects, list) or not objects:
        return scene, 0

    general = scene.get("general") if isinstance(scene.get("general"), dict) else {}
    ortho = general.get("orthogonalprojection") if isinstance(general.get("orthogonalprojection"), dict) else {}
    canvas_w = float(ortho.get("width") or 1920.0)
    canvas_h = float(ortho.get("height") or 1080.0)

    # 1) Identificar objetos directamente ocultos (visible=false, alpha=0, scale=0, nombre backup)
    hidden_ids: set[Any] = set()
    removed_indices: set[int] = set()

    for idx, obj in enumerate(objects):
        if not isinstance(obj, dict):
            continue
        oid = obj.get("id")
        name = str(obj.get("name") or "")
        if (
            _is_prop_false(obj.get("visible"))
            or _is_alpha_zero(obj.get("alpha"))
            or _is_scale_zero(obj.get("scale"))
            or _is_spare_or_backup_name(name)
        ):
            removed_indices.add(idx)
            if oid is not None:
                hidden_ids.add(oid)

    # 2) Propagar invisibilidad a todos los hijos/descendientes ("parent" y "copyof")
    changed = True
    while changed:
        changed = False
        for idx, obj in enumerate(objects):
            if idx in removed_indices or not isinstance(obj, dict):
                continue
            parent_id = obj.get("parent")
            copyof_id = obj.get("copyof")
            if (parent_id is not None and parent_id in hidden_ids) or (
                copyof_id is not None and copyof_id in hidden_ids
            ):
                removed_indices.add(idx)
                oid = obj.get("id")
                if oid is not None and oid not in hidden_ids:
                    hidden_ids.add(oid)
                    changed = True

    # 3) Detectar capas fuera del lienzo o sprites de repuesto flotando en el margen derecho
    #    (como la segunda cabeza y mechones sueltos en Yuta Okkotsu 2867422409).
    is_yuta_or_aggressive = aggressive or (str(wp_id) in AUTO_FIX_SCENE_IDS)

    # Agrupar objetos visibles por recurso de imagen/modelo para detectar duplicados desplazados
    image_groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for idx, obj in enumerate(objects):
        if idx in removed_indices or not isinstance(obj, dict):
            continue
        img_key = str(obj.get("image") or obj.get("model") or "").strip()
        if img_key:
            image_groups.setdefault(img_key, []).append((idx, obj))

    for img_key, group in image_groups.items():
        if len(group) >= 2:
            # Si hay varias instancias del mismo sprite y una está cerca del centro mientras otra
            # está desplazada hacia el borde derecho (x > 0.68 * canvas_w), eliminar la copia desplazada.
            xs = [(_parse_vec(o.get("origin"), (canvas_w / 2, canvas_h / 2, 0.0))[0], i, o) for i, o in group]
            min_x = min(x for x, _, _ in xs)
            for x, idx, obj in xs:
                if x > canvas_w * 0.66 and (x - min_x) > canvas_w * 0.20:
                    removed_indices.add(idx)
                    oid = obj.get("id")
                    if oid is not None:
                        hidden_ids.add(oid)

    for idx, obj in enumerate(objects):
        if idx in removed_indices or not isinstance(obj, dict):
            continue
        origin = _parse_vec(obj.get("origin"), (canvas_w * 0.5, canvas_h * 0.5, 0.0))
        size = _parse_vec(obj.get("size"), (canvas_w, canvas_h))
        ox = origin[0] if len(origin) >= 1 else canvas_w * 0.5
        oy = origin[1] if len(origin) >= 2 else canvas_h * 0.5
        sw = size[0] if len(size) >= 1 else canvas_w
        sh = size[1] if len(size) >= 2 else canvas_h

        # Capas completamente fuera del lienzo visible
        if ox < -canvas_w * 0.25 or ox > canvas_w * 1.15 or oy < -canvas_h * 0.25 or oy > canvas_h * 1.15:
            removed_indices.add(idx)
            oid = obj.get("id")
            if oid is not None:
                hidden_ids.add(oid)
            continue

        if is_yuta_or_aggressive:
            name_low = str(obj.get("name") or "").casefold()
            img_low = str(obj.get("image") or obj.get("model") or "").casefold()
            # En Yuta Okkotsu (2867422409) y escenas similares con puppet-warp / piezas sueltas:
            # Las piezas de repuesto (segunda cabeza, cara, pelo/mechones) son sprites pequeños/medianos
            # (< 45% del ancho del lienzo) colocados flotando a la derecha (ox > 64% del ancho)
            # o con nombres de piezas de cabeza/pelo duplicadas.
            is_small_piece = (0 < sw < canvas_w * 0.48) and (0 < sh < canvas_h * 0.65)
            is_far_right = ox > canvas_w * 0.64
            has_part_keyword = any(
                k in name_low or k in img_low
                for k in ("head", "cabeza", "face", "cara", "hair", "pelo", "eye", "mouth", "part", "spare", "copy")
            )
            if is_small_piece and is_far_right and (has_part_keyword or str(wp_id) == "2867422409"):
                log.info(
                    "Eliminando capa fantasma en %s: id=%s name=%r origin=(%.1f, %.1f) size=(%.1f, %.1f)",
                    wp_id, obj.get("id"), obj.get("name"), ox, oy, sw, sh,
                )
                removed_indices.add(idx)
                oid = obj.get("id")
                if oid is not None:
                    hidden_ids.add(oid)

    # Propagar una vez más por si alguna capa eliminada tenía sub-capas hijas
    changed = True
    while changed:
        changed = False
        for idx, obj in enumerate(objects):
            if idx in removed_indices or not isinstance(obj, dict):
                continue
            if obj.get("parent") in hidden_ids or obj.get("copyof") in hidden_ids:
                removed_indices.add(idx)
                oid = obj.get("id")
                if oid is not None and oid not in hidden_ids:
                    hidden_ids.add(oid)
                    changed = True

    # 4) Construir la lista final de objetos y limpiar efectos invisibles dentro de cada objeto
    cleaned_objects: list[dict[str, Any]] = []
    modifications = len(removed_indices)

    for idx, obj in enumerate(objects):
        if idx in removed_indices or not isinstance(obj, dict):
            continue
        new_obj = dict(obj)
        # Si "visible" era un diccionario con value=True, simplificarlo a booleano True
        if isinstance(new_obj.get("visible"), dict):
            new_obj["visible"] = True
            modifications += 1
        if isinstance(new_obj.get("alpha"), dict) and "value" in new_obj["alpha"]:
            try:
                new_obj["alpha"] = float(new_obj["alpha"]["value"])
                modifications += 1
            except (TypeError, ValueError):
                pass

        effects = new_obj.get("effects")
        if isinstance(effects, list) and effects:
            clean_fx = [
                fx for fx in effects
                if isinstance(fx, dict) and not _is_prop_false(fx.get("visible"))
            ]
            if len(clean_fx) != len(effects):
                modifications += len(effects) - len(clean_fx)
                new_obj["effects"] = clean_fx

        cleaned_objects.append(new_obj)

    new_scene = dict(scene)
    new_scene["objects"] = cleaned_objects
    return new_scene, modifications


def prepare_unpacked_scene(
    wp_folder: Path,
    wp_id: str,
    force_aggressive: bool = False,
) -> Path | None:
    """Desempaqueta ``scene.pkg`` (o copia ``scene.json``) en ``~/.cache/linwall/unpacked/<id>/``,
    limpia las capas fantasma/invisibles y devuelve la ruta lista para ``linux-wallpaperengine``.
    """
    out_dir = paths.CACHE_DIR / "unpacked" / str(wp_id)
    stamp_file = out_dir / ".linwall_unpacked_version"
    mode_tag = f"{UNPACKED_CACHE_VERSION}:{'agg' if force_aggressive else 'std'}"

    try:
        if (
            stamp_file.is_file()
            and stamp_file.read_text(encoding="utf-8").strip() == mode_tag
            and (out_dir / "project.json").is_file()
        ):
            return out_dir
    except OSError:
        pass

    pkg_file: Path | None = None
    for cand in ("scene.pkg", "gifscene.pkg"):
        p = wp_folder / cand
        if p.is_file():
            pkg_file = p
            break

    unpacked = unpack_pkg_to_memory(pkg_file) if pkg_file else None
    loose_scene_file = wp_folder / "scene.json"

    if unpacked is None and not loose_scene_file.is_file():
        return None

    try:
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.debug("No se pudo crear %s: %s", out_dir, exc)
        return None

    modifications = 0
    if unpacked is not None and pkg_file is not None:
        magic, files = unpacked
        updated_files: list[tuple[str, bytes]] = []
        for rel_name, data in files:
            clean_rel = rel_name.replace("\\", "/").lstrip("/")
            parts = [p for p in clean_rel.split("/") if p and p not in (".", "..")]
            if not parts:
                continue
            safe_rel = "/".join(parts)

            if safe_rel.lower() == "scene.json":
                try:
                    raw_text = data.decode("utf-8-sig", errors="replace")
                    scene_obj = json.loads(raw_text, strict=False)
                    if isinstance(scene_obj, dict):
                        cleaned_obj, modifications = sanitize_scene_dict(
                            scene_obj, wp_id=wp_id, aggressive=force_aggressive
                        )
                        data = json.dumps(cleaned_obj, ensure_ascii=False).encode("utf-8")
                except Exception as exc:  # noqa: BLE001
                    log.debug("No se pudo sanitizar scene.json de %s: %s", wp_id, exc)

            dest_file = out_dir / safe_rel
            try:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                dest_file.write_bytes(data)
            except OSError:
                pass
            updated_files.append((rel_name, data))

        # Re-escribir también scene.pkg limpio dentro de out_dir por si linux-wallpaperengine
        # prioriza scene.pkg sobre los archivos sueltos del directorio.
        pack_pkg_from_memory(out_dir / pkg_file.name, magic, updated_files)

    elif loose_scene_file.is_file():
        scene_obj = read_json_tolerant(loose_scene_file)
        if isinstance(scene_obj, dict):
            cleaned_obj, modifications = sanitize_scene_dict(
                scene_obj, wp_id=wp_id, aggressive=force_aggressive
            )
            (out_dir / "scene.json").write_text(
                json.dumps(cleaned_obj, ensure_ascii=False), encoding="utf-8"
            )

    # Enlazar o copiar el resto de archivos del proyecto (project.json, sonidos, vídeos, etc.)
    try:
        for item in wp_folder.iterdir():
            if item.name in ("scene.pkg", "gifscene.pkg", "scene.json"):
                continue
            target = out_dir / item.name
            if target.exists():
                continue
            if item.name.lower() == "project.json":
                shutil.copy2(item, target)
            else:
                try:
                    target.symlink_to(item.resolve(), target_is_directory=item.is_dir())
                except OSError:
                    if item.is_file():
                        shutil.copy2(item, target)
    except OSError as exc:
        log.debug("Error enlazando archivos de %s: %s", wp_folder, exc)

    try:
        stamp_file.write_text(mode_tag, encoding="utf-8")
    except OSError:
        pass

    log.info(
        "Escena %s desempaquetada y sanitizada en %s (%d ajustes de capas)",
        wp_id, out_dir, modifications,
    )
    return out_dir


def extract_hd_texture_from_pkg(wp_folder: Path, wp_id: str) -> Path | None:
    """Extrae la textura JPEG/PNG/MP4 de mayor resolución embebida dentro de ``scene.pkg``
    (útil para escenas panorámicas como *Subway 360°* ``2852229628`` donde ``preview.gif`` es 256x256).
    """
    out_dir = paths.CACHE_DIR / "unpacked" / str(wp_id)
    cached_jpg = out_dir / "_linwall_hd_fallback.jpg"
    if cached_jpg.is_file() and cached_jpg.stat().st_size > 10000:
        return cached_jpg

    pkg_file = wp_folder / "scene.pkg"
    if not pkg_file.is_file():
        return None

    unpacked = unpack_pkg_to_memory(pkg_file)
    if not unpacked:
        return None
    _, files = unpacked

    # Ordenar los archivos .tex más grandes primero (la textura de fondo principal suele ser la mayor)
    tex_files = [
        (name, data) for name, data in files
        if name.lower().endswith((".tex", ".jpg", ".jpeg", ".png", ".webp")) and len(data) > 40000
    ]
    tex_files.sort(key=lambda item: len(item[1]), reverse=True)

    try:
        import io
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None

    for _name, data in tex_files[:8]:
        # Buscar firmas JPEG (\xff\xd8\xff) o PNG (\x89PNG\r\n\x1a\n) dentro del contenedor .tex (TEXB)
        candidates: list[bytes] = []
        png_pos = data.find(b"\x89PNG\r\n\x1a\n")
        if png_pos >= 0:
            iend = data.find(b"IEND", png_pos)
            if iend > png_pos:
                candidates.append(data[png_pos : iend + 8])
            else:
                candidates.append(data[png_pos:])

        for sig in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb"):
            jpg_pos = data.find(sig)
            if jpg_pos >= 0:
                candidates.append(data[jpg_pos:])
                break

        for payload in candidates:
            try:
                with Image.open(io.BytesIO(payload)) as img:
                    w, h = img.size
                    if w >= 1000 and h >= 500:
                        rgb = img.convert("RGB")
                        out_dir.mkdir(parents=True, exist_ok=True)
                        rgb.save(cached_jpg, "JPEG", quality=94)
                        log.info("Textura HD (%dx%d) extraída de scene.pkg para %s", w, h, wp_id)
                        return cached_jpg
            except Exception:  # noqa: BLE001
                continue

    return None
