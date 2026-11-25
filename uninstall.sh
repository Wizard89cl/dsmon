#!/usr/bin/env bash
set -euo pipefail

pkill -u "$UID" -f "^/usr/bin/python3.*(dsmon-widget|desktop-resource-widget)\\.py" 2>/dev/null || true
rm -f "$HOME/.local/bin/dsmon-widget.py"
rm -f "$HOME/.local/bin/desktop-resource-widget.py"
rm -f "$HOME/.local/bin/dsmon_core.py"
rm -f "$HOME/.local/bin/dsmon_analyzer.py"
rm -f "$HOME/.config/autostart/dsmon-widget.desktop"
rm -f "$HOME/.config/autostart/desktop-resource-widget.desktop"
if [[ "${1:-}" == "--purge" ]]; then
    rm -f "$HOME/.config/dsmon/widget.json"
    rm -f "$HOME/.config/desktop-resource-widget.json"
    echo "DSMon y preferencias de usuario eliminados."
else
    echo "DSMon desinstalado (preferencias preservadas; usa --purge para borrarlas)."
fi
