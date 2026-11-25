#!/usr/bin/env bash
set -euo pipefail

# Headless server installer for DSMon (requires root / sudo)
INSTALL_DIR="/opt/dsmon"
CONF_DIR="/etc/dsmon"
CONF_FILE="$CONF_DIR/dsmon.conf"
SERVICE_FILE="/etc/systemd/system/dsmon.service"

LOG_DIR="/var/log/dsmon"
REPORT_DIR=""
INTERVAL="5"
MOUNT_PATH="/"
FLUSH_EVERY="15"
RUN_USER="dsmon"

usage() {
    cat << EOF
Uso: sudo ./install-server.sh [opciones]
  --log-dir DIR       Directorio/FS para los CSV (default: /var/log/dsmon)
  --report-dir DIR    Directorio para reportes HTML (default: <log-dir>/reports)
  --interval SEC      Intervalo de muestreo en segundos (default: 5)
  --mount PATH        Punto de montaje para uso de disco (default: /)
  --flush-every N     Muestras por escritura a disco (default: 15)
  --user NAME         Usuario sin privilegios para el servicio (default: dsmon)
  -h, --help          Muestra esta ayuda
EOF
}

require_arg() {
    if [[ $# -lt 2 || -z "$2" ]]; then
        echo "Falta valor para la opción $1" >&2
        usage
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --log-dir) require_arg "$@"; LOG_DIR="$2"; shift 2 ;;
        --report-dir) require_arg "$@"; REPORT_DIR="$2"; shift 2 ;;
        --interval) require_arg "$@"; INTERVAL="$2"; shift 2 ;;
        --mount) require_arg "$@"; MOUNT_PATH="$2"; shift 2 ;;
        --flush-every) require_arg "$@"; FLUSH_EVERY="$2"; shift 2 ;;
        --user) require_arg "$@"; RUN_USER="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Opción desconocida: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ "$RUN_USER" == "root" ]]; then
    echo "Por seguridad no se permite ejecutar el servicio como root (--user $RUN_USER)." >&2
    exit 1
fi

for val in "$LOG_DIR" "$REPORT_DIR" "$MOUNT_PATH" "$RUN_USER"; do
    if [[ "$val" =~ [[:cntrl:]] ]]; then
        echo "Parámetro inválido con caracteres de control." >&2
        exit 1
    fi
done

if [[ -z "$REPORT_DIR" ]]; then
    REPORT_DIR="${LOG_DIR%/}/reports"
fi

if [[ $EUID -ne 0 ]]; then
    echo "Este instalador requiere root (ej: sudo ./install-server.sh --log-dir /mnt/nmon)." >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$INSTALL_DIR" "$CONF_DIR" "$LOG_DIR" "$REPORT_DIR"

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    NOLOGIN_SHELL="$(command -v nologin || echo /usr/sbin/nologin)"
    useradd --system --user-group --no-create-home --shell "$NOLOGIN_SHELL" "$RUN_USER"
    echo "Usuario de sistema '$RUN_USER' creado."
fi
RUN_GROUP="$(id -gn "$RUN_USER")"

install -m 755 "$SRC_DIR/dsmon_core.py" "$INSTALL_DIR/dsmon_core.py"
install -m 755 "$SRC_DIR/dsmon.py" "$INSTALL_DIR/dsmon.py"
install -m 755 "$SRC_DIR/dsmon_analyzer.py" "$INSTALL_DIR/dsmon_analyzer.py"
chown -hR root:root "$INSTALL_DIR"
chown -hR root:"$RUN_GROUP" "$CONF_DIR"
chown -hR "$RUN_USER:$RUN_GROUP" "$LOG_DIR" "$REPORT_DIR"
chmod 750 "$CONF_DIR"
ln -sf "$INSTALL_DIR/dsmon.py" /usr/local/bin/dsmon

if [[ ! -f "$CONF_FILE" ]]; then
    cat > "$CONF_FILE" << EOF
[dsmon]
log_dir = $LOG_DIR
report_dir = $REPORT_DIR
interval = $INTERVAL
mount = $MOUNT_PATH
flush_every = $FLUSH_EVERY
compress_after_days = 1
retention_days = 30
EOF
    echo "Configuración creada en $CONF_FILE"
else
    echo "Configuración existente preservada en $CONF_FILE"
fi
chown root:"$RUN_GROUP" "$CONF_FILE"
chmod 640 "$CONF_FILE"

PROTECT_HOME="yes"
if [[ "$LOG_DIR" == /home/* || "$REPORT_DIR" == /home/* ]]; then
    PROTECT_HOME="no"
fi

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=DSMon Headless System Recorder
After=network.target local-fs.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
ExecStart=/usr/bin/python3 $INSTALL_DIR/dsmon.py --headless --config $CONF_FILE
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=$PROTECT_HOME
ReadWritePaths=$LOG_DIR
CapabilityBoundingSet=
AmbientCapabilities=
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictRealtime=yes
UMask=0027
SystemCallFilter=@system-service

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
echo "DSMon instalado en $INSTALL_DIR (comando: dsmon, usuario: $RUN_USER)"
echo "Para iniciar y habilitar en el arranque:"
echo "  sudo systemctl enable --now dsmon.service"
