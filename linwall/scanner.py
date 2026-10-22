# =============================================================================
#  LinWall Engine v1.6.0 (Official Public Release)
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾) — walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
# =============================================================================
"""Escáner de la carpeta de Steam Workshop de Wallpaper Engine (AppID 431960)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from linwall import paths

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mkv", ".mov", ".m4v", ".gif"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
PREVIEW_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".mp4", ".webm")
PREVIEW_CANDIDATES = (
    "preview.jpg", "preview.jpeg", "preview.png", "preview.gif", "preview.webp",
    "preview.bmp", "preview.mp4", "preview.webm",
    "thumb.jpg", "thumb.jpeg", "thumb.png", "thumb.gif", "thumb.webp",
    "thumbnail.jpg", "thumbnail.jpeg", "thumbnail.png", "thumbnail.gif",
    "cover.jpg", "cover.jpeg", "cover.png",
)
KNOWN_TYPES = ("video", "scene", "web", "application", "preset")


@dataclass
class Wallpaper:
    id: str
    title: str
    type: str
    file: str
    preview: str | None
    path: str
    tags: list[str] = field(default_factory=list)
    content_rating: str = ""

    # ------------------------------------------------------------ helpers
    @property
    def folder(self) -> Path:
        return Path(self.path)

    @property
    def file_path(self) -> Path | None:
        if not self.file:
            return None
        p = self.folder / self.file
        return p if p.is_file() else None

    @property
    def preview_path(self) -> Path | None:
        if not self.preview:
            return None
        p = self.folder / self.preview
        if p.is_file():
            return p
        found = _match_case_insensitive(self.folder, self.preview)
        if found:
            cand = self.folder / found
            if cand.is_file():
                return cand
        return None

    @property
    def fallback_media_path(self) -> Path | None:
        """Archivo multimedia o GIF animado de respaldo (para mpv o Modo Compatibilidad)."""
        fp = self.file_path
        if fp and fp.is_file() and fp.suffix.lower() in VIDEO_EXTENSIONS:
            return fp
        pp = self.preview_path
        if pp and pp.is_file() and pp.suffix.lower() in VIDEO_EXTENSIONS:
            return pp
        try:
            for child in sorted(self.folder.iterdir()):
                if (child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS
                        and not child.name.lower().startswith("preview")):
                    return child
            for child in sorted(self.folder.iterdir()):
                if child.is_file() and child.suffix.lower() == ".gif":
                    return child
        except OSError:
            pass
        return pp

    @property
    def is_supported(self) -> bool:
        return self.type in ("video", "scene", "web")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Wallpaper":
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            type=str(data.get("type", "")),
            file=str(data.get("file", "") or ""),
            preview=data.get("preview"),
            path=str(data.get("path", "")),
            tags=list(data.get("tags") or []),
            content_rating=str(data.get("content_rating", "") or ""),
        )


# --------------------------------------------------------------- lectura json
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def read_json_tolerant(path: Path) -> dict[str, Any] | None:
    """Lee un JSON aguantando BOM, UTF-16, Latin-1, caracteres de control y comas finales."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.debug("No se pudo leer %s: %s", path, exc)
        return None

    text: str | None = None
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ("utf-16",)
    else:
        candidates = ("utf-8-sig", "cp1252", "latin-1")
    for enc in candidates:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    cleaned = _TRAILING_COMMA.sub(r"\1", text)
    cleaned = _TRAILING_COMMA.sub(r"\1", cleaned)
    for attempt in (text, cleaned):
        try:
            data = json.loads(attempt, strict=False)
            return data if isinstance(data, dict) else None
        except ValueError:
            continue
    log.debug("JSON inválido: %s", path)
    return None


# ----------------------------------------------------------------- detección
def _infer_type(declared: str, file: str, folder: Path) -> str:
    t = (declared or "").strip().lower()
    if t in KNOWN_TYPES:
        return t
    ext = Path(file).suffix.lower() if file else ""
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in (".html", ".htm"):
        return "web"
    if file.endswith("scene.json") or (folder / "scene.pkg").exists() or (folder / "scene.json").exists():
        return "scene"
    for child in folder.iterdir() if folder.is_dir() else ():
        if child.suffix.lower() in VIDEO_EXTENSIONS and not child.name.startswith("preview"):
            return "video"
    return t or "unknown"


def _match_case_insensitive(folder: Path, name: str) -> str | None:
    """Busca ``name`` dentro de ``folder`` de forma case-insensitive (soporta subrutas y ``\\``)."""
    clean = (name or "").strip().replace("\\", "/").lstrip("./")
    if not clean:
        return None
    direct = folder / clean
    if direct.is_file():
        return clean

    parts = [p for p in clean.split("/") if p and p != "."]
    if not parts:
        return None

    cur = folder
    rel_parts: list[str] = []
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        target = part.casefold()
        matched_child: Path | None = None
        try:
            for child in cur.iterdir():
                if child.name.casefold() == target:
                    if (is_last and child.is_file()) or (not is_last and child.is_dir()):
                        matched_child = child
                        break
        except OSError:
            return None
        if matched_child is None:
            return None
        rel_parts.append(matched_child.name)
        cur = matched_child

    return "/".join(rel_parts)


def _find_preview(folder: Path, declared: str | None, main_file: str = "") -> str | None:
    """Resuelve estrictamente la portada oficial del fondo:

    1. Archivo definido en ``"preview"`` dentro de ``project.json`` (exacto o case-insensitive).
    2. Mismo nombre base (stem) de ``"preview"`` con otra extensión de portada (ej. ``preview.gif``).
    3. Candidatos estándar ``preview.*`` / ``thumb.*`` / ``cover.*`` en la raíz del proyecto.
    4. Archivo principal de vídeo/GIF (jamás toma ``.png`` aleatorios de sprites/texturas).
    """
    if declared:
        found = _match_case_insensitive(folder, declared)
        if found:
            return found
        stem = Path(declared.replace("\\", "/")).stem.casefold()
        if stem:
            for ext in PREVIEW_EXTENSIONS:
                alt = _match_case_insensitive(folder, f"{stem}{ext}")
                if alt:
                    return alt

    for name in PREVIEW_CANDIDATES:
        found = _match_case_insensitive(folder, name)
        if found:
            return found

    if main_file:
        mf = _match_case_insensitive(folder, main_file)
        if mf and Path(mf).suffix.lower() in VIDEO_EXTENSIONS:
            return mf

    return None


def _find_main_file(folder: Path, declared: str, wtype: str) -> str:
    if declared:
        found = _match_case_insensitive(folder, declared)
        if found:
            return found
    if wtype == "video":
        for child in sorted(folder.iterdir()):
            if child.suffix.lower() in VIDEO_EXTENSIONS and not child.name.lower().startswith("preview"):
                return child.name
        for child in sorted(folder.iterdir()):
            if child.suffix.lower() in VIDEO_EXTENSIONS:
                return child.name
    if wtype == "scene":
        for name in ("scene.pkg", "gifscene.pkg", "scene.json"):
            found = _match_case_insensitive(folder, name)
            if found:
                return found
    if wtype == "web":
        found = _match_case_insensitive(folder, "index.html")
        if found:
            return found
    return declared or ""


def parse_project(folder: Path) -> Wallpaper | None:
    """Convierte una carpeta del workshop en un :class:`Wallpaper` (o ``None``)."""
    project = folder / "project.json"
    if not project.is_file():
        alt_proj = _match_case_insensitive(folder, "project.json")
        if alt_proj:
            project = folder / alt_proj
    data = read_json_tolerant(project) if project.is_file() else None
    if data is None:
        # Sin project.json válido: aceptar si al menos hay un vídeo o un scene.pkg.
        if not ((folder / "scene.pkg").exists() or any(
                c.suffix.lower() in VIDEO_EXTENSIONS for c in folder.iterdir())):
            return None
        data = {}

    declared_file = str(data.get("file") or "").strip()
    wtype = _infer_type(str(data.get("type") or ""), declared_file, folder)
    main_file = _find_main_file(folder, declared_file, wtype)
    title = str(data.get("title") or "").strip() or folder.name
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    declared_preview = str(data.get("preview") or "").strip() or None

    return Wallpaper(
        id=folder.name,
        title=title,
        type=wtype,
        file=main_file,
        preview=_find_preview(folder, declared_preview, main_file=main_file),
        path=str(folder.resolve()),
        tags=[str(t) for t in tags],
        content_rating=str(data.get("contentrating") or ""),
    )


def find_workshop_dirs(extra: Iterable[str | Path] = ()) -> list[Path]:
    """Devuelve las carpetas 431960 existentes (sin duplicados por enlace simbólico)."""
    candidates: list[Path] = list(paths.DEFAULT_WORKSHOP_DIRS)
    candidates += [
        lib / "steamapps" / "workshop" / "content" / paths.WORKSHOP_APPID
        for lib in paths.steam_library_roots()
    ]
    candidates += [Path(p).expanduser() for p in extra]

    found: list[Path] = []
    seen: set[Path] = set()
    for c in candidates:
        try:
            if not c.is_dir():
                continue
            real = c.resolve()
        except OSError:
            continue
        if real not in seen:
            seen.add(real)
            found.append(c)
    return found


def scan(extra_dirs: Iterable[str | Path] = ()) -> list[Wallpaper]:
    """Escanea todas las carpetas del workshop y devuelve los fondos ordenados por título."""
    result: dict[str, Wallpaper] = {}
    for base in find_workshop_dirs(extra_dirs):
        try:
            children = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError as exc:
            log.warning("No se pudo listar %s: %s", base, exc)
            continue
        # Una carpeta extra puede ser directamente un proyecto.
        if (base / "project.json").is_file():
            children.insert(0, base)
        for folder in children:
            try:
                wp = parse_project(folder)
            except OSError as exc:
                log.debug("Error leyendo %s: %s", folder, exc)
                continue
            if wp and wp.id not in result:
                result[wp.id] = wp
    return sorted(result.values(), key=lambda w: w.title.casefold())


def find_by_id(wallpaper_id: str, extra_dirs: Iterable[str | Path] = ()) -> Wallpaper | None:
    for base in find_workshop_dirs(extra_dirs):
        folder = base / wallpaper_id
        if folder.is_dir():
            return parse_project(folder)
    return None
