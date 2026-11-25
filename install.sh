#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="$HOME/.local/bin"
AUTOSTART_DIR="$HOME/.config/autostart"

# Migrar datos/config si existía una versión local previa (DeskMon / DSMon)
for old_dir in "$HOME/DeskMon" "$HOME/DSMon"; do
    if [[ -d "$old_dir" && ! -d "$HOME/dsmon" ]]; then
        mv "$old_dir" "$HOME/dsmon"
        echo "Logs migrados desde $old_dir a $HOME/dsmon"
    fi
done
if [[ -f "$HOME/.config/desktop-resource-widget.json" && ! -f "$HOME/.config/dsmon/widget.json" ]]; then
    mkdir -p "$HOME/.config/dsmon"
    mv "$HOME/.config/desktop-resource-widget.json" "$HOME/.config/dsmon/widget.json"
fi

mkdir -p "$BIN_DIR" "$AUTOSTART_DIR"
install -m 755 "$(dirname "$0")/dsmon_core.py" "$BIN_DIR/dsmon_core.py"
install -m 755 "$(dirname "$0")/dsmon.py" "$BIN_DIR/dsmon-widget.py"
install -m 755 "$(dirname "$0")/dsmon_analyzer.py" "$BIN_DIR/dsmon_analyzer.py"

cat > "$AUTOSTART_DIR/dsmon-widget.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DSMon - Monitor de Recursos
Comment=Widget flotante solo visible en el escritorio
Exec=/usr/bin/python3 "$BIN_DIR/dsmon-widget.py"
Icon=utilities-system-monitor
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo "DSMon instalado en $BIN_DIR/dsmon-widget.py"
echo "Autostart configurado en $AUTOSTART_DIR/dsmon-widget.desktop"
