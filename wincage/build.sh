#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"
OUT_NAME="${OUT_NAME:-sandbox_host.exe}"
OUT="$SCRIPT_DIR/$OUT_NAME"

SOURCES=(
    "$SRC_DIR/main.cpp"
    "$SRC_DIR/container.cpp"
    "$SRC_DIR/job.cpp"
    "$SRC_DIR/watchdog.cpp"
    "$SRC_DIR/event.cpp"
)

echo "Building $OUT_NAME ..."

g++ \
    -std=c++20 \
    -D_WIN32_WINNT=0x0602 -DWINVER=0x0602 \
    -Wall -Wextra -Werror \
    -fstack-protector-strong \
    -O2 \
    -static-libgcc -static-libstdc++ -static \
    -I"$SRC_DIR" \
    "${SOURCES[@]}" \
    -o "$OUT" \
    -luserenv \
    -lole32 \
    -ladvapi32 \
    -lkernel32

echo "Built: $OUT"
