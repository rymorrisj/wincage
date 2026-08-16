#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Verify required tools ────────────────────────────────────────────────────

if ! command -v sdl2-config &>/dev/null; then
    echo "ERROR: sdl2-config not found."
    echo "Install SDL2 from MSYS2 UCRT64:"
    echo "  pacman -S mingw-w64-ucrt-x86_64-SDL2"
    exit 1
fi

if ! command -v pkg-config &>/dev/null; then
    echo "ERROR: pkg-config not found."
    echo "Install pkg-config from MSYS2 UCRT64:"
    echo "  pacman -S mingw-w64-ucrt-x86_64-pkg-config"
    exit 1
fi

# ── SDL2 flags ───────────────────────────────────────────────────────────────
# SDL_MAIN_HANDLED keeps the standard main() entry point, so -lSDL2main
# and -mwindows (which expect SDL_main/WinMain) are stripped below.

SDL_CFLAGS=$(sdl2-config --cflags | sed 's/-Dmain=SDL_main//g')
SDL_LIBS=$(sdl2-config --libs | sed 's/-lSDL2main//g' | sed 's/-mwindows//g')

# ── Qt (optional, skip test_qt_qpa if not available) ───────────────────────

BUILD_QT=1
if ! pkg-config --exists Qt5Widgets 2>/dev/null; then
    echo "WARNING: Qt5Widgets not found via pkg-config, test_qt_qpa.exe will not be built."
    echo "To enable: pacman -S mingw-w64-ucrt-x86_64-qt5-base"
    BUILD_QT=0
fi

# ── Build ────────────────────────────────────────────────────────────────────

# ── Runtime linking ──────────────────────────────────────────────────────────
# End users run the packaged app without MSYS2's ucrt64/bin on PATH, so the
# MinGW C++ runtime must not be a runtime DLL dependency. SDL2 itself has no
# static build in the MSYS2 package, so SDL2.dll is copied next to each exe
# below instead.
RUNTIME_FLAGS="-static-libgcc -static-libstdc++"

echo "Building test_sdl2_d3d11.exe ..."
g++ -std=c++20 -Wall -Wextra -O2 ${RUNTIME_FLAGS} \
    ${SDL_CFLAGS} \
    "$SCRIPT_DIR/test_sdl2_d3d11.cpp" \
    ${SDL_LIBS} -ld3d11 -ldxgi \
    -o "$SCRIPT_DIR/test_sdl2_d3d11.exe"

echo "Building test_sdl2_opengl.exe ..."
g++ -std=c++20 -Wall -Wextra -O2 ${RUNTIME_FLAGS} \
    ${SDL_CFLAGS} \
    "$SCRIPT_DIR/test_sdl2_opengl.cpp" \
    ${SDL_LIBS} \
    -o "$SCRIPT_DIR/test_sdl2_opengl.exe"

SDL_BINDIR="$(sdl2-config --prefix)/bin"
echo "Copying SDL2.dll from $SDL_BINDIR ..."
cp "$SDL_BINDIR/SDL2.dll" "$SCRIPT_DIR/SDL2.dll"

echo "Copying libwinpthread-1.dll from $SDL_BINDIR ..."
cp "$SDL_BINDIR/libwinpthread-1.dll" "$SCRIPT_DIR/libwinpthread-1.dll"

if [[ "$BUILD_QT" -eq 1 ]]; then
    QT_FLAGS=$(pkg-config --cflags --libs Qt5Widgets)
    echo "Building test_qt_qpa.exe ..."
    g++ -std=c++20 -Wall -Wextra -O2 ${RUNTIME_FLAGS} \
        $(pkg-config --cflags Qt5Widgets) \
        "$SCRIPT_DIR/test_qt_qpa.cpp" \
        $(pkg-config --libs Qt5Widgets) \
        -o "$SCRIPT_DIR/test_qt_qpa.exe"
else
    echo "SKIP: test_qt_qpa.exe"
fi

echo ""
echo "Done. Run from Python:"
echo "  from wincage.checker import run_checks"
echo "  results = run_checks()"
