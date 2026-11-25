# DSMon

[![CI](https://github.com/Wizard89cl/dsmon/actions/workflows/ci.yml/badge.svg)](https://github.com/Wizard89cl/dsmon/actions/workflows/ci.yml)

Monitor de recursos para **Linux** de **cero dependencias externas en el núcleo** (Python 3 stdlib), pensado para dos escenarios:

1. **Servidores *headless* (reemplazo moderno de `nmon` sin Excel):** demonio ligero que graba métricas en CSV diarios con compresión y retención automática, más un analizador que genera un reporte **HTML interactivo autocontenido**.
2. **Escritorio Linux (GTK3):** widget flotante minimalista (~0.1% CPU) anclado al escritorio.

> **Alcance soportado:** Linux (`x86_64` y `ARM64`) en servidores físicos (Intel Xeon, AMD EPYC/Ryzen), hosts KVM/Proxmox, VMs (VMware, KVM, Hyper-V, Cloud) y escritorios/laptops Linux. *(No aplica a AIX, IBM i/AS400 ni VMware ESXi VMkernel).*

---

## Arquitectura

* **`dsmon_core.py`** — Núcleo puro en Python stdlib (`/proc` y `/sys`, sin GTK). Incluye `SystemStats`, `CsvRecorder` (compresión `.csv.gz` y retención automática) y `load_settings()`.
* **`dsmon.py`** — Punto de entrada unificado:
  * Sin flags: lanza el widget **GTK3** de escritorio.
  * `--headless`: ejecuta el grabador continuo para servidores sin entorno gráfico.
  * `--snapshot [--json]`: imprime una lectura única del sistema (ideal para scripts o agentes).
  * `--report`: genera el reporte HTML desde los logs CSV / CSV.gz.
* **`dsmon_analyzer.py`** — Analizador bajo demanda (`0 MB` en reposo). Lee `.csv` y `.csv.gz`, cruza errores/warnings de `journalctl` y genera un HTML autocontenido con pestañas (*CPU & Kernel*, *Memoria & Swap*, *Disco I/O & Red*, *GPU*, *Procesos & Logs*), **Importar CSV/GZ** directo en el navegador, **Volver a local** y **Exportar PDF** ejecutivo (`@media print`).

---

## Métricas capturadas

* **CPU & Kernel:** CPU total, desglose `nmon` (`user`, `sys`, `iowait`, `steal` para VMs), temperaturas **multi-socket** (`/sys/class/thermal` + `hwmon` `coretemp`/`k10temp`/`zenpower`), *context switches* (`ctxt/s`), interrupciones (`intr/s`), *load average* y `hostname`.
* **Memoria & Swap:** RAM usada/total, Swap y paginación activa (`pswpin/s`, `pswpout/s` desde `/proc/vmstat`).
* **Disco:** Uso del montaje configurado (con degradación segura si falta un montaje) + tráfico I/O físico real (`disk_read_kbps`, `disk_write_kbps`, `disk_iops` desde `/proc/diskstats`, filtrando particiones y `loop`).
* **Red:** Tasas `RX` / `TX` (`/proc/net/dev`), excluyendo por defecto (`--iface auto`) *loopback*, *bridges* virtuales (`docker*`, `veth*`, `br-*`, `virbr*`), esclavas de *bonding* (`bond*`/`team*`) y subinterfaces VLAN para evitar doble conteo.
* **Procesos:** Top CPU y Top RAM leyendo `/proc/<pid>/stat` sin lanzar subprocesos.
* **Multi-GPU (NVIDIA y AMD, incluso mezcladas):**
  * NVIDIA vía `nvidia-smi` (soporta múltiples GPUs y maneja campos `[N/A]`).
  * AMD vía `/sys/class/drm/card*/device` (`amdgpu`, sin dependencias externas).
  * Respeta suspensión `D3cold` por dispositivo para no despertar GPUs en reposo.
* **Batería (laptops):** Estado y porcentaje (`/sys/class/power_supply`).

---

## Instalación en Servidor *Headless* (`systemd`)

Pensado para que el sysadmin elija su propio filesystem de logs (ej. `/mnt/nmon`, `/var/log/dsmon`) sin depender de `cron` ni `logrotate`:

```bash
sudo ./install-server.sh --log-dir /mnt/nmon/logs --report-dir /mnt/nmon/reports --interval 5
sudo systemctl enable --now dsmon.service
```

Qué configura `install-server.sh`:
* Crea el usuario y grupo de sistema sin privilegios `dsmon` (personalizable con `--user`, rechaza `root`).
* Instala en `/opt/dsmon` y enlaza `/usr/local/bin/dsmon`.
* Crea `/etc/dsmon/dsmon.conf` (ver [dsmon.conf.example](dsmon.conf.example)).
* Instala `/etc/systemd/system/dsmon.service` con *hardening* (`NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict`, `CapabilityBoundingSet=`, `MemoryDenyWriteExecute=yes`, `SystemCallFilter=@system-service`, `ReadWritePaths` acotado a `$LOG_DIR`).

---

## Instalación en Escritorio (GTK3)

Requisitos: Python 3 y `python3-gi` (incluido por defecto en Ubuntu / GNOME).

```bash
chmod +x install.sh
./install.sh
```

Para iniciarlo sin reiniciar sesión:

```bash
python3 ~/.local/bin/dsmon-widget.py &
```

Desinstalación: `./uninstall.sh`

---

## Uso CLI y Configuración

### Precedencia de configuración
1. Flags CLI (`--log-dir`, `--report-dir`, `--interval`, `--mount`, `--iface`, `--flush-every`, `--retention-days`, `--compress-after-days`, `--metrics-port`, `--metrics-bind`, `--days`, `--config`)
2. Variables de entorno (`DSMON_LOG_DIR`, `DSMON_REPORT_DIR`, `DSMON_INTERVAL`, `DSMON_MOUNT`, `DSMON_NET_IFACE`, `DSMON_FLUSH_EVERY`, `DSMON_RETENTION_DAYS`, `DSMON_COMPRESS_AFTER_DAYS`, `DSMON_METRICS_PORT`, `DSMON_METRICS_BIND`)
3. Archivo INI: `~/.config/dsmon/dsmon.conf` o `/etc/dsmon/dsmon.conf`
4. Valores por defecto (`~/dsmon/logs`, `~/dsmon/reports`, `5s`, retención `30` días, compresión tras `1` día)

### Comandos rápidos

```bash
# Snapshot instantáneo en JSON (incluye array multi-GPU y multi-socket)
dsmon --snapshot --json

# Grabador headless manual (opcionalmente exponiendo /metrics para Prometheus)
dsmon --headless --interval 5 --log-dir /var/log/dsmon --iface auto --metrics-port 9109

# Generar reporte HTML (últimos 7 días por defecto, --days 0 para todos)
dsmon --report --log-dir /var/log/dsmon --report-dir /var/log/dsmon/reports
```

---

## Seguridad y Tests

* Sanitización contra *CSV formula injection* (`=`, `+`, `-`, `@`, `\t`, `\r`).
* Escritura atómica con `tempfile.NamedTemporaryFile` + `os.replace` y rechazo de *symlinks* al rotar logs.
* Resolución de binarios externos (`nvidia-smi`, `journalctl`, `xdg-open`) restringida a rutas confiables (`/usr/bin:/bin:/usr/local/bin`).
* Tests unitarios + CI en `.github/workflows/ci.yml`:

```bash
python3 -m unittest discover -s tests -v
```

---

## Roadmap

* [x] Endpoint opcional `--metrics-port` en formato Prometheus (`/metrics`) usando `http.server` de stdlib para scraping directo desde Prometheus / Grafana.
* [ ] Soporte para múltiples puntos de montaje configurables.
