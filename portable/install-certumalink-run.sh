#!/usr/bin/env bash
set -euo pipefail

DEFAULT_SCRIPT_URL="https://raw.githubusercontent.com/joescanlin/certumalink-importer/main/portable/certumalink-doctor-import.py"
SCRIPT_URL="${CERTUMALINK_IMPORTER_URL:-$DEFAULT_SCRIPT_URL}"
INSTALL_DIR="${CERTUMALINK_INSTALL_DIR:-$HOME/.certumalink}"
BIN_DIR="${CERTUMALINK_BIN_DIR:-$HOME/.local/bin}"
SCRIPT_PATH="$INSTALL_DIR/certumalink-doctor-import.py"
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
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "Add this to your shell profile if certumalink_run is not found:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo
echo "Try it:"
echo "  certumalink_run --zip"
