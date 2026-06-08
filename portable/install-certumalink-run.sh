#!/usr/bin/env bash
set -euo pipefail

DEFAULT_SCRIPT_URL="https://raw.githubusercontent.com/joescanlin/certumalink-importer/main/portable/certumalink-doctor-import.py"
SCRIPT_URL="${CERTUMALINK_IMPORTER_URL:-$DEFAULT_SCRIPT_URL}"
INSTALL_DIR="${CERTUMALINK_INSTALL_DIR:-$HOME/.certumalink}"
SCRIPT_PATH="$INSTALL_DIR/certumalink-doctor-import.py"

path_contains() {
  case ":$PATH:" in
    *":$1:"*) return 0 ;;
    *) return 1 ;;
  esac
}

choose_bin_dir() {
  if [[ -n "${CERTUMALINK_BIN_DIR:-}" ]]; then
    printf '%s\n' "$CERTUMALINK_BIN_DIR"
    return
  fi

  for candidate in /usr/local/bin /opt/homebrew/bin; do
    if path_contains "$candidate" && [[ -d "$candidate" && -w "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  printf '%s\n' "$HOME/.local/bin"
}

BIN_DIR="$(choose_bin_dir)"
BIN_PATH="$BIN_DIR/certumalink_run"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required but was not found" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl is required but was not found" >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" "$BIN_DIR"

echo "Downloading Certumalink doctor importer..."
curl -fsSL "$SCRIPT_URL" -o "$SCRIPT_PATH"
chmod +x "$SCRIPT_PATH"

cat > "$BIN_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$SCRIPT_PATH" "\$@"
EOF

chmod +x "$BIN_PATH"

echo "Installed certumalink_run to $BIN_PATH"

case ":$PATH:" in
  *":$BIN_DIR:"*)
    echo
    echo "Try it:"
    echo "  certumalink_run --zip"
    ;;
  *)
    echo
    echo "certumalink_run was installed outside your current PATH."
    echo "Run it now with:"
    echo "  $BIN_PATH --zip"
    echo
    echo "Or add it to your shell profile:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac
