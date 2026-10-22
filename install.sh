#!/usr/bin/env bash
# =============================================================================
#  LinWall Engine v1.6.0 (Official Public Release)
#  Instalador todo-en-uno para Debian / Ubuntu / Linux Mint
#
#  Autor / Creador : Walter Guerrero (Paraguay 🇵🇾)
#  Contacto        : walteguerrerob@gmail.com
#  Colaboración IA : Gemini (Google AI) & Claude
#  Licencia        : GNU General Public License v3.0 (GPL-3.0)
#
#  Uso:  ./install.sh [--skip-lwe] [--skip-deps] [--force-build] [--help]
#
#   --skip-lwe     no compilar linux-wallpaperengine (solo fondos VIDEO)
#   --skip-deps    no instalar paquetes con apt
#   --force-build  recompilar xwinwrap / linux-wallpaperengine aunque existan
# =============================================================================
set -Eeuo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DATA_DIR="$DATA_HOME/linwall"
BIN_DIR="$DATA_DIR/bin"
APP_DIR="$DATA_DIR/app"
BUILD_DIR="$DATA_DIR/build"
LWE_DIR="$DATA_DIR/linux-wallpaperengine"
LOCAL_BIN="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
PYTHON_BIN="/usr/bin/python3"

XWINWRAP_REPO="https://github.com/ujjwal96/xwinwrap.git"
LWE_REPO="https://github.com/Almamu/linux-wallpaperengine.git"

SKIP_LWE=0
SKIP_DEPS=0
FORCE_BUILD=0

# ----------------------------------------------------------------- utilidades
if [[ -t 1 ]]; then
  C_B=$'\e[1m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_C=$'\e[36m'; C_0=$'\e[0m'
else
  C_B=""; C_G=""; C_Y=""; C_R=""; C_C=""; C_0=""
fi
step() { printf '\n%s==> %s%s\n' "$C_B$C_C" "$*" "$C_0"; }
ok()   { printf '%s  ✔ %s%s\n' "$C_G" "$*" "$C_0"; }
warn() { printf '%s  ! %s%s\n' "$C_Y" "$*" "$C_0" >&2; }
die()  { printf '%s  ✘ %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }
trap 'die "Fallo inesperado en la línea $LINENO (comando: $BASH_COMMAND)"' ERR

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

for arg in "$@"; do
  case "$arg" in
    --skip-lwe)    SKIP_LWE=1 ;;
    --skip-deps)   SKIP_DEPS=1 ;;
    --force-build) FORCE_BUILD=1 ;;
    -h|--help)     usage ;;
    *) die "Opción desconocida: $arg (usa --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] && die "No ejecutes este script como root: se pedirá sudo solo para apt."
[[ -d "$SRC_DIR/linwall" ]] || die "No se encontró el paquete 'linwall' junto a install.sh."

have() { command -v "$1" >/dev/null 2>&1; }

# Paquete disponible en los repositorios configurados (LC_ALL=C evita traducción de Candidate:)
pkg_available() {
  local cand
  cand="$(LC_ALL=C apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
  [[ -n "$cand" && "$cand" != "(none)" ]]
}

pkg_installed() { LC_ALL=C dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

apt_install() {
  local wanted=() pkg
  for pkg in "$@"; do
    pkg_installed "$pkg" && continue
    if pkg_available "$pkg"; then wanted+=("$pkg"); fi
  done
  if ((${#wanted[@]})); then
    echo "  Instalando (${#wanted[@]} paquetes): ${wanted[*]}"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${wanted[@]}"
  else
    ok "Todos los paquetes ya estaban instalados"
  fi
}

# ------------------------------------------------------------- dependencias
BASE_PKGS=(
  python3 python3-gi python3-gi-cairo python3-pil gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0
  mpv x11-xserver-utils x11-utils wmctrl xdotool xdg-utils procps
  git build-essential pkg-config ca-certificates
)
XWINWRAP_PKGS=( libx11-dev libxext-dev libxrender-dev x11proto-xext-dev x11proto-dev )
LWE_PKGS=(
  cmake libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev libxxf86vm-dev
  libgl-dev libgl1-mesa-dev libglew-dev libglu1-mesa-dev freeglut3-dev libglut-dev
  libsdl2-dev liblz4-dev libzstd-dev libpng-dev zlib1g-dev libdbus-1-dev libfreetype-dev
  libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libswresample-dev
  libglm-dev libglfw3-dev libmpv-dev libmpv2 libmpv1 libpulse-dev libpulse0 libfftw3-dev
  libwayland-dev wayland-protocols libxkbcommon-dev libegl-dev
  libnss3 libatk1.0-0 libatk1.0-0t64 libatk-bridge2.0-0 libatk-bridge2.0-0t64
  libcups2 libcups2t64 libxcomposite1 libxdamage1 libgbm1 libasound2 libasound2t64
  libdrm2 libxshmfence1
)

install_deps() {
  step "Instalando dependencias del sistema (apt)"
  have apt-get || die "Este instalador requiere una distribución basada en Debian (apt)."
  if ! sudo apt-get update -qq; then
    warn "Algunos repositorios de terceros (ej. Spotify/Google Earth) dieron error de firma GPG; continuando con los repositorios oficiales…"
  fi
  local pkgs=("${BASE_PKGS[@]}" "${XWINWRAP_PKGS[@]}")
  ((SKIP_LWE)) || pkgs+=("${LWE_PKGS[@]}")
  local clean=() p
  for p in "${pkgs[@]}"; do [[ "$p" == \#* ]] || clean+=("$p"); done
  apt_install "${clean[@]}"
  ok "Dependencias listas"
}

# ------------------------------------------------------------------ xwinwrap
build_xwinwrap() {
  step "xwinwrap"
  if ((FORCE_BUILD == 0)); then
    if have xwinwrap; then ok "Ya disponible en PATH: $(command -v xwinwrap)"; return 0; fi
    if [[ -x "$BIN_DIR/xwinwrap" ]]; then ok "Ya compilado: $BIN_DIR/xwinwrap"; return 0; fi
  fi
  mkdir -p "$BUILD_DIR" "$BIN_DIR"
  local src="$BUILD_DIR/xwinwrap"
  if [[ -d "$src/.git" ]]; then
    git -C "$src" pull --ff-only --quiet || warn "No se pudo actualizar xwinwrap; se usa la copia local"
  else
    rm -rf "$src"
    git clone --depth 1 "$XWINWRAP_REPO" "$src" || return 1
  fi
  make -C "$src" clean >/dev/null 2>&1 || true
  make -C "$src" || return 1
  install -m 0755 "$src/xwinwrap" "$BIN_DIR/xwinwrap" || return 1
  ok "xwinwrap instalado en $BIN_DIR/xwinwrap"
}

# ----------------------------------------------------- linux-wallpaperengine
write_lwe_wrapper() {
  cat > "$BIN_DIR/linux-wallpaperengine" <<EOF
#!/bin/sh
# Lanzador generado por LinWall Engine: ejecuta el binario desde su carpeta
# para que encuentre sus bibliotecas (CEF) y recursos.
export LD_LIBRARY_PATH="$LWE_DIR:$LWE_DIR/lib:\${LD_LIBRARY_PATH:-}"
cd "$LWE_DIR" || exit 1
exec "$LWE_DIR/linux-wallpaperengine" "\$@"
EOF
  chmod 0755 "$BIN_DIR/linux-wallpaperengine"
}

build_lwe() {
  step "linux-wallpaperengine (Almamu)"
  if ((FORCE_BUILD == 0)); then
    if have linux-wallpaperengine; then ok "Ya disponible en PATH: $(command -v linux-wallpaperengine)"; return 0; fi
    if [[ -x "$LWE_DIR/linux-wallpaperengine" ]]; then
      write_lwe_wrapper; ok "Ya compilado: $LWE_DIR"; return 0
    fi
  fi
  have cmake || { warn "cmake no está instalado"; return 1; }
  mkdir -p "$BUILD_DIR" "$BIN_DIR"
  local src="$BUILD_DIR/linux-wallpaperengine"
  if [[ -d "$src/.git" ]]; then
    git -C "$src" pull --ff-only --quiet || warn "No se pudo actualizar; se usa la copia local"
    git -C "$src" submodule update --init --recursive || return 1
  else
    rm -rf "$src"
    git clone --recursive --depth 1 --shallow-submodules "$LWE_REPO" "$src" || return 1
  fi
  echo "  Compilando (puede tardar varios minutos; CMake descargará CEF la primera vez)…"
  cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$LWE_DIR" || return 1
  cmake --build "$src/build" -j "$(nproc)" || return 1

  local out="$src/build/output"
  if [[ ! -x "$out/linux-wallpaperengine" ]]; then
    local bin
    bin="$(find "$src/build" -type f -name linux-wallpaperengine -perm -u+x | head -n1)"
    [[ -n "$bin" ]] || { warn "No se encontró el binario compilado"; return 1; }
    out="$(dirname "$bin")"
  fi
  rm -rf "$LWE_DIR"
  cp -a "$out" "$LWE_DIR" || return 1
  if [[ -d "$src/build/lib" ]]; then
    mkdir -p "$LWE_DIR/lib"
    cp -a "$src/build/lib/." "$LWE_DIR/lib/" || true
  fi
  write_lwe_wrapper
  ok "linux-wallpaperengine instalado en $LWE_DIR"
}

# --------------------------------------------------------------- aplicación
install_app() {
  step "Instalando LinWall Engine"
  mkdir -p "$APP_DIR" "$LOCAL_BIN" "$APPS_DIR" "$ICON_DIR" "$BIN_DIR"
  rm -rf "$APP_DIR/linwall"
  cp -r "$SRC_DIR/linwall" "$APP_DIR/"
  find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
  "$PYTHON_BIN" -m compileall -q "$APP_DIR/linwall" >/dev/null 2>&1 || true

  # Limpiar caché antigua de miniaturas para regenerarlas en v1.5.0 desde project.json (frame intermedio en GIFs)
  rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/linwall/thumbs/"* 2>/dev/null || true

  # Enlazar automáticamente la carpeta oficial 'assets' de Wallpaper Engine (Steam) dentro de $LWE_DIR
  if [[ -d "$LWE_DIR" && ! -e "$LWE_DIR/assets" ]]; then
    local root
    for root in "$HOME/.steam/steam" "$HOME/.local/share/Steam" \
                "$HOME/.var/app/com.valvesoftware.Steam/.local/share/Steam"; do
      if [[ -d "$root/steamapps/common/wallpaper_engine/assets" ]]; then
        ln -s "$root/steamapps/common/wallpaper_engine/assets" "$LWE_DIR/assets" 2>/dev/null || true
        break
      fi
    done
  fi

  cat > "$LOCAL_BIN/linwall" <<EOF
#!/bin/sh
# Lanzador de LinWall Engine (generado por install.sh)
export PATH="$BIN_DIR:\$PATH"
export PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec $PYTHON_BIN -m linwall "\$@"
EOF
  chmod 0755 "$LOCAL_BIN/linwall"
  ok "Lanzador: $LOCAL_BIN/linwall"

  rm -f "$ICON_DIR/linwall.svg" 2>/dev/null || true
  "$PYTHON_BIN" - <<PY || true
from pathlib import Path
from PIL import Image

src = Path("$SRC_DIR/linwall/logo.jpeg")
if src.is_file():
    with Image.open(src) as im:
        rgba = im.convert("RGBA")
        app_png = Path("$APP_DIR/linwall/gui/linwall.png")
        app_png.parent.mkdir(parents=True, exist_ok=True)
        rgba.resize((256, 256), Image.Resampling.LANCZOS).save(app_png, "PNG")
        for sz in (256, 128, 64, 48):
            d = Path("$DATA_HOME/icons/hicolor") / f"{sz}x{sz}" / "apps"
            d.mkdir(parents=True, exist_ok=True)
            rgba.resize((sz, sz), Image.Resampling.LANCZOS).save(d / "linwall.png", "PNG")
        pix = Path("$DATA_HOME/pixmaps")
        pix.mkdir(parents=True, exist_ok=True)
        rgba.resize((256, 256), Image.Resampling.LANCZOS).save(pix / "linwall.png", "PNG")
PY
  ok "Icono oficial instalado desde linwall/logo.jpeg"

  cat > "$APPS_DIR/linwall.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=LinWall Engine
GenericName=Fondos animados
Comment=Usa los fondos de Wallpaper Engine (Steam Workshop) en Linux
Comment[en]=Use Wallpaper Engine (Steam Workshop) wallpapers on Linux
Exec=$LOCAL_BIN/linwall
TryExec=$LOCAL_BIN/linwall
Icon=$APP_DIR/linwall/gui/linwall.png
Terminal=false
Categories=GTK;Utility;Settings;DesktopSettings;
Keywords=wallpaper;fondo;animado;steam;workshop;video;live;
StartupNotify=true
StartupWMClass=linwall
Actions=Stop;Restore;

[Desktop Action Stop]
Name=Detener fondo animado
Exec=$LOCAL_BIN/linwall --stop

[Desktop Action Restore]
Name=Restaurar último fondo
Exec=$LOCAL_BIN/linwall --restore
EOF
  chmod 0644 "$APPS_DIR/linwall.desktop"
  have update-desktop-database && update-desktop-database -q "$APPS_DIR" 2>/dev/null || true
  have gtk-update-icon-cache && gtk-update-icon-cache -q -t -f "$DATA_HOME/icons/hicolor" 2>/dev/null || true
  ok "Acceso directo en el menú: $APPS_DIR/linwall.desktop"

  # Regenerar el autostart (si existe) para que apunte al lanzador instalado
  "$LOCAL_BIN/linwall" --refresh-autostart >/dev/null 2>&1 || true
}

# ------------------------------------------------------------- comprobaciones
post_checks() {
  step "Comprobaciones finales"
  local found_ws=0 found_assets=0 root
  for root in "$HOME/.steam/steam" "$HOME/.local/share/Steam" \
              "$HOME/.var/app/com.valvesoftware.Steam/.local/share/Steam"; do
    [[ -d "$root/steamapps/workshop/content/431960" ]] && found_ws=1
    [[ -d "$root/steamapps/common/wallpaper_engine/assets" ]] && found_assets=1
  done
  ((found_ws)) && ok "Carpeta del Workshop 431960 encontrada" \
               || warn "No se encontró steamapps/workshop/content/431960 (suscríbete a fondos en Steam)"
  if ((SKIP_LWE == 0)); then
    ((found_assets)) && ok "Assets de Wallpaper Engine encontrados (necesarios para SCENE)" \
                     || warn "Instala Wallpaper Engine en Steam (Proton) para tener los assets que usan los fondos SCENE"
  fi
  case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *) warn "$LOCAL_BIN no está en tu PATH en esta terminal; abre una nueva sesión o usa la ruta completa." ;;
  esac
  [[ "${XDG_SESSION_TYPE:-x11}" == "wayland" ]] && warn "Sesión Wayland detectada: LinWall necesita una sesión X11."
  "$LOCAL_BIN/linwall" --status || true
}

# ===================================================================== main
printf '%sLinWall Engine v1.6.0 – Instalador Oficial%s\n' "$C_B" "$C_0"
printf '  Autor: Walter Guerrero (Paraguay 🇵🇾) <walteguerrerob@gmail.com>\n'
printf '  Colaboración de Arquitectura e IA: Gemini (Google AI) & Claude · Licencia: GPL-3.0\n'
((SKIP_DEPS)) || install_deps

if ! build_xwinwrap; then
  warn "No se pudo compilar xwinwrap: los vídeos se mostrarán con mpv anclado al escritorio."
fi
if ((SKIP_LWE == 0)); then
  if ! build_lwe; then
    warn "No se pudo compilar linux-wallpaperengine: los fondos SCENE/WEB no estarán disponibles."
    warn "Puedes reintentarlo con: ./install.sh --skip-deps --force-build"
  fi
fi

install_app
post_checks

printf '\n%s¡Listo!%s Abre «LinWall Engine» desde el menú o ejecuta: %s\n' "$C_G$C_B" "$C_0" "$LOCAL_BIN/linwall"
