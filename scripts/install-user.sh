#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  printf 'error: python interpreter not found: %s\n' "$PYTHON_BIN" >&2
  exit 1
fi

USER_BASE=$("$PYTHON_BIN" -c 'import site; print(site.getuserbase())')
PLATFORM=$(uname -s 2>/dev/null || printf 'unknown')

default_install_home() {
  if [ -n "${XDG_DATA_HOME:-}" ]; then
    printf '%s/gemini-auth-switch\n' "$XDG_DATA_HOME"
    return
  fi

  case "$PLATFORM" in
    Darwin)
      printf '%s/Library/Application Support/gemini-auth-switch\n' "$HOME"
      ;;
    *)
      printf '%s/.local/share/gemini-auth-switch\n' "$HOME"
      ;;
  esac
}

INSTALL_HOME=${GSWITCH_INSTALL_HOME:-$(default_install_home)}
VENV_DIR=${GSWITCH_VENV_DIR:-"$INSTALL_HOME/venv"}
BIN_DIR=${GSWITCH_BIN_DIR:-"$USER_BASE/bin"}
SOURCE_BIN=$VENV_DIR/bin/gswitch
TARGET_BIN=$BIN_DIR/gswitch

mkdir -p "$INSTALL_HOME" "$BIN_DIR"

if ! [ -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade "$REPO_ROOT"
ln -sf "$SOURCE_BIN" "$TARGET_BIN"

if [ ! -x "$TARGET_BIN" ]; then
  printf 'error: expected installed command at %s\n' "$TARGET_BIN" >&2
  exit 1
fi

printf 'installed %s\n' "$TARGET_BIN"

case ":${PATH:-}:" in
  *":$BIN_DIR:"*) ;;
  *)
    printf 'warning: %s is not on PATH\n' "$BIN_DIR" >&2
    ;;
esac
