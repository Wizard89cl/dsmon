# DSMon

[![CI](https://github.com/Wizard89cl/dsmon/actions/workflows/ci.yml/badge.svg)](https://github.com/Wizard89cl/dsmon/actions/workflows/ci.yml)

**English** | [Español](README.es.md)

A **zero-external-dependency** Linux resource monitor (Python 3 stdlib core) built for two main workflows:

1. **Headless servers (a modern `nmon` replacement without Excel):** a lightweight daemon that records daily CSV metrics with automatic compression and retention, paired with an analyzer that generates a **self-contained interactive HTML report**.
2. **Linux desktop (GTK3):** a minimal floating widget (~0.1% CPU) pinned to the desktop.

> **Supported scope:** Linux (`x86_64` and `ARM64`) on bare-metal servers (Intel Xeon, AMD EPYC/Ryzen), KVM/Proxmox hosts, VMs (VMware, KVM, Hyper-V, Cloud), and Linux desktops/laptops. *(Not intended for AIX, IBM i/AS400, or VMware ESXi VMkernel.)*
>
> **Note:** The HTML report UI and desktop widget are currently in Spanish; full `en`/`es` multi-language support (i18n) is on the roadmap.

---

## Architecture

* **`dsmon_core.py`** — Pure Python stdlib core (`/proc` and `/sys`, no GTK). Includes `SystemStats`, `CsvRecorder` (`.csv.gz` compression and automatic retention), and `load_settings()`.
* **`dsmon.py`** — Unified entry point:
  * No flags: launches the **GTK3** desktop widget.
  * `--headless`: runs the continuous recorder for headless servers.
  * `--snapshot [--json]`: prints a single system reading (ideal for scripts or agents).
  * `--report`: builds the HTML report from CSV / CSV.gz logs.
* **`dsmon_analyzer.py`** — On-demand analyzer (`0 MB` idle). Reads `.csv` and `.csv.gz`, correlates `journalctl` errors/warnings, and outputs a self-contained HTML report with tabs (*CPU & Kernel*, *Memory & Swap*, *Disk I/O & Network*, *GPU*, *Processes & Logs*), **browser-side CSV/GZ import**, **Return to local**, and **PDF export** (`@media print`).

---

## Captured Metrics

* **CPU & Kernel:** Total CPU, `nmon`-style breakdown (`user`, `sys`, `iowait`, `steal` for VMs), **multi-socket** temperatures (`/sys/class/thermal` + `hwmon` `coretemp`/`k10temp`/`zenpower`), context switches (`ctxt/s`), interrupts (`intr/s`), load average, and `hostname`.
* **Memory & Swap:** Used/total RAM, Swap, and active paging (`pswpin/s`, `pswpout/s` from `/proc/vmstat`).
* **Disk:** Configured mount usage (gracefully degrades if a mount is missing) + real physical I/O throughput (`disk_read_kbps`, `disk_write_kbps`, `disk_iops` from `/proc/diskstats`, filtering out partitions and `loop` devices).
* **Network:** `RX` / `TX` rates (`/proc/net/dev`), excluding by default (`--iface auto`) loopback, virtual bridges (`docker*`, `veth*`, `br-*`, `virbr*`), bonding slaves (`bond*`/`team*`), and VLAN subinterfaces to prevent double counting.
* **Processes:** Top CPU and Top RAM by reading `/proc/<pid>/stat` without spawning subprocesses.
* **Multi-GPU (NVIDIA and AMD, including mixed setups):**
  * NVIDIA via `nvidia-smi` (multi-GPU aware, handles `[N/A]` fields).
  * AMD via `/sys/class/drm/card*/device` (`amdgpu`, no external dependencies).
  * Respects per-device `D3cold` runtime suspend so sleeping GPUs are not woken up.
* **Battery (laptops):** Status and charge percentage (`/sys/class/power_supply`).

### Screenshots (Qwen3.5-9B Benchmark)

<img width="1922" height="1000" alt="Screenshot 2026-11-25 18-54-14" src="https://github.com/user-attachments/assets/c46ad83b-5af5-4a56-bb91-3911a159591b" />

<img width="1922" height="958" alt="Screenshot 2026-11-25 18-51-38" src="https://github.com/user-attachments/assets/f645eb5b-7f9c-4128-89bd-e0e6ba888e5f" />

---

## Headless Server Installation (`systemd`)

Designed so sysadmins can choose their own log filesystem (e.g. `/mnt/nmon`, `/var/log/dsmon`) without relying on `cron` or `logrotate`:

```bash
sudo ./install-server.sh --log-dir /mnt/nmon/logs --report-dir /mnt/nmon/reports --interval 5
sudo systemctl enable --now dsmon.service
```

What `install-server.sh` configures:
* Creates an unprivileged `dsmon` system user and group (customizable via `--user`, rejects `root`).
* Installs into `/opt/dsmon` and symlinks `/usr/local/bin/dsmon`.
* Creates `/etc/dsmon/dsmon.conf` (see [dsmon.conf.example](dsmon.conf.example)).
* Installs `/etc/systemd/system/dsmon.service` with systemd hardening (`NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict`, `CapabilityBoundingSet=`, `MemoryDenyWriteExecute=yes`, `SystemCallFilter=@system-service`, and `ReadWritePaths` scoped to `$LOG_DIR`).

---

## Desktop Installation (GTK3)

Requirements: Python 3 and `python3-gi` (included by default on Ubuntu / GNOME).

```bash
chmod +x install.sh
./install.sh
```

To start it without logging out:

```bash
python3 ~/.local/bin/dsmon-widget.py &
```

Uninstall: `./uninstall.sh`

---

## CLI Usage & Configuration

### Configuration Precedence
1. CLI flags (`--log-dir`, `--report-dir`, `--interval`, `--mount`, `--iface`, `--flush-every`, `--retention-days`, `--compress-after-days`, `--metrics-port`, `--metrics-bind`, `--days`, `--config`)
2. Environment variables (`DSMON_LOG_DIR`, `DSMON_REPORT_DIR`, `DSMON_INTERVAL`, `DSMON_MOUNT`, `DSMON_NET_IFACE`, `DSMON_FLUSH_EVERY`, `DSMON_RETENTION_DAYS`, `DSMON_COMPRESS_AFTER_DAYS`, `DSMON_METRICS_PORT`, `DSMON_METRICS_BIND`)
3. INI file: `~/.config/dsmon/dsmon.conf` or `/etc/dsmon/dsmon.conf`
4. Defaults (`~/dsmon/logs`, `~/dsmon/reports`, `5s` interval, `30`-day retention, compression after `1` day)

### Quick Commands

```bash
# Instant JSON snapshot (includes multi-GPU and multi-socket arrays)
dsmon --snapshot --json

# Manual headless recorder (optionally exposing /metrics for Prometheus)
dsmon --headless --interval 5 --log-dir /var/log/dsmon --iface auto --metrics-port 9109

# Generate HTML report (last 7 days by default, --days 0 for all)
dsmon --report --log-dir /var/log/dsmon --report-dir /var/log/dsmon/reports
```

---

## Security & Tests

* Sanitization against CSV formula injection (`=`, `+`, `-`, `@`, `\t`, `\r`).
* Atomic writes via `tempfile.NamedTemporaryFile` + `os.replace`, rejecting symlinks on log rotation.
* External binary resolution (`nvidia-smi`, `journalctl`, `xdg-open`) restricted to trusted paths (`/usr/bin:/bin:/usr/local/bin`).
* Unit tests + CI in `.github/workflows/ci.yml`:

```bash
python3 -m unittest discover -s tests -v
```

---

## Roadmap

* [x] Optional `--metrics-port` endpoint in Prometheus format (`/metrics`) using stdlib `http.server` for direct Prometheus / Grafana scraping.
* [ ] Support for multiple configurable mount points.
* [ ] Multi-language support (`en` / `es`) for HTML reports, CLI, and GTK3 widget.
