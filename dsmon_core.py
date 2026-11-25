#!/usr/bin/env python3
"""DSMon Core: Zero-dependency Linux system metrics collector and CSV recorder."""

import configparser
import csv
from datetime import date, datetime, timedelta
import gzip
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Optional

HOSTNAME = socket.gethostname()
TRUSTED_PATH = "/usr/bin:/bin:/usr/local/bin"
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_csv_cell(val):
    """Neutralize CSV formula injection triggers (=, +, -, @, tab, CR)."""
    s = str(val or "")
    if s.startswith(_CSV_FORMULA_PREFIXES):
        return f"'{s}"
    return s

LOG_DIR = os.path.expanduser("~/dsmon/logs")
REPORT_DIR = os.path.expanduser("~/dsmon/reports")
SYSTEM_CONFIG_FILE = "/etc/dsmon/dsmon.conf"
USER_CONFIG_FILE = os.path.expanduser("~/.config/dsmon/dsmon.conf")


_PATH_KEYS = ("log_dir", "report_dir")
_STR_KEYS = ("mount", "net_iface", "metrics_bind")
_NUM_CASTERS = (
    ("interval", float),
    ("flush_every", int),
    ("retention_days", int),
    ("compress_after_days", int),
    ("metrics_port", int),
)


def _apply_ini_settings(settings: dict[str, Any], chosen: str) -> None:
    """Apply settings from an INI config file in-place."""
    cp = configparser.ConfigParser()
    try:
        cp.read(chosen, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        logging.warning("No se pudo leer %s: %s", chosen, exc)
        return
    if not cp.has_section("dsmon"):
        return
    sec = cp["dsmon"]
    for key in _PATH_KEYS:
        raw = sec.get(key, "").strip()
        if raw:
            settings[key] = os.path.expanduser(raw)
    for key in _STR_KEYS:
        raw = sec.get(key, "").strip()
        if raw:
            settings[key] = raw
    for key, caster in _NUM_CASTERS:
        raw = sec.get(key, "").strip()
        if raw:
            try:
                settings[key] = caster(raw)
            except ValueError:
                logging.warning("Valor inválido en %s para %s=%r", chosen, key, raw)


def _apply_env_settings(settings: dict[str, Any]) -> None:
    """Override settings with DSMON_* environment variables in-place."""
    for key in _PATH_KEYS + _STR_KEYS:
        raw = os.environ.get(f"DSMON_{key.upper()}", "").strip()
        if raw:
            settings[key] = os.path.expanduser(raw) if key in _PATH_KEYS else raw
    for key, caster in _NUM_CASTERS:
        raw = os.environ.get(f"DSMON_{key.upper()}", "").strip()
        if raw:
            try:
                settings[key] = caster(raw)
            except ValueError:
                logging.warning("Valor inválido en DSMON_%s=%r", key.upper(), raw)


def load_settings(config_path: Optional[str] = None) -> dict[str, Any]:
    """Load DSMon settings with precedence: env vars > config file > defaults."""
    settings: dict[str, Any] = {
        "log_dir": LOG_DIR,
        "report_dir": REPORT_DIR,
        "interval": 5.0,
        "mount": "/",
        "net_iface": "auto",
        "flush_every": 15,
        "retention_days": 30,
        "compress_after_days": 1,
        "metrics_port": 0,
        "metrics_bind": "127.0.0.1",
    }
    candidates = [config_path] if config_path else [USER_CONFIG_FILE, SYSTEM_CONFIG_FILE]
    chosen = next((p for p in candidates if p and os.path.isfile(p)), None)
    if chosen:
        _apply_ini_settings(settings, chosen)
    _apply_env_settings(settings)
    return settings

CSV_HEADER = [
    "timestamp",
    "cpu_pct",
    "temp_c",
    "ram_used_gb",
    "ram_total_gb",
    "ram_pct",
    "swap_pct",
    "disk_pct",
    "rx_kbps",
    "tx_kbps",
    "load1",
    "top_proc",
    "cpu_user_pct",
    "cpu_sys_pct",
    "cpu_iowait_pct",
    "cpu_steal_pct",
    "ctxt_per_s",
    "intr_per_s",
    "disk_read_kbps",
    "disk_write_kbps",
    "disk_iops",
    "pswpin_per_s",
    "pswpout_per_s",
    "top_ram_proc",
    "gpu_util_pct",
    "gpu_temp_c",
    "gpu_vram_used_mb",
    "gpu_vram_total_mb",
    "hostname",
    "temp_cpu1_c",
    "gpu_count",
    "gpu1_util_pct",
    "gpu1_temp_c",
    "gpu1_vram_used_mb",
    "gpu1_vram_total_mb",
]

CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# Whole physical block devices (exclude partitions like sda1, nvme0n1p1, loop0, dm-0)
_WHOLE_DISK_RE = re.compile(r"^(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")


def fmt_bytes(b):
    """Format bytes as gigabytes."""
    return f"{b / (1024 ** 3):.1f} GB"


def fmt_rate(bps):
    """Format bytes/sec as KB/s or MB/s."""
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


class SystemStats:
    """Reads system metrics directly from /proc and /sys with minimal overhead."""

    def __init__(self):
        now = time.monotonic()
        self._prev_cpu_full = self._read_cpu_stat_full()
        self._prev_cpu_time = now
        self._prev_net = self._read_net_bytes()
        self._prev_net_time = now
        self._prev_disk = self._read_diskstats()
        self._prev_disk_time = now
        self._prev_vmstat = self._read_vmstat()
        self._prev_vmstat_time = now
        self._prev_proc_ticks = {}
        self._prev_proc_time = now
        self._mem_total_kb = self._read_mem_total_kb()

    @staticmethod
    def _read_mem_total_kb():
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return max(1, int(line.split()[1]))
        except (OSError, ValueError, IndexError):
            pass
        return 1

    @staticmethod
    def _read_cpu_stat_full():
        """Read aggregate CPU ticks plus ctxt and intr counters from /proc/stat."""
        vals = []
        ctxt = 0
        intr = 0
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("cpu "):
                        vals = [int(x) for x in line.split()[1:]]
                    elif line.startswith("ctxt "):
                        ctxt = int(line.split()[1])
                    elif line.startswith("intr "):
                        intr = int(line.split()[1])
        except (OSError, ValueError, IndexError):
            pass
        while len(vals) < 8:
            vals.append(0)
        return vals, ctxt, intr

    _VIRTUAL_IFACE_PREFIXES = ("lo", "veth", "docker", "br-", "virbr")

    @staticmethod
    def _bond_slaves(sys_net_dir="/sys/class/net"):
        """Return set of interface names that are slaves of a Linux bond/team device."""
        slaves = set()
        if not os.path.isdir(sys_net_dir):
            return slaves
        for iface in os.listdir(sys_net_dir):
            master_link = os.path.join(sys_net_dir, iface, "master")
            if os.path.islink(master_link):
                try:
                    master = os.path.basename(os.readlink(master_link))
                    if master.startswith(("bond", "team")):
                        slaves.add(iface)
                except OSError:
                    continue
        return slaves

    @classmethod
    def parse_net_dev_lines(cls, lines, target_iface="auto", bond_slaves=None):
        """Parse /proc/net/dev lines, avoiding double-counting bond slaves, VLANs, and virtual bridges."""
        rx = tx = 0
        parsed = []
        for line in list(lines)[2:]:
            if ":" not in line:
                continue
            iface, data = line.split(":", 1)
            name = iface.strip()
            cols = data.split()
            if len(cols) >= 9:
                try:
                    parsed.append((name, int(cols[0]), int(cols[8])))
                except ValueError:
                    continue

        if target_iface and target_iface != "auto":
            for name, r_b, t_b in parsed:
                if name == target_iface:
                    return r_b, t_b
            return 0, 0

        slaves = cls._bond_slaves() if bond_slaves is None else bond_slaves
        all_names = {name for name, _, _ in parsed}
        for name, r_b, t_b in parsed:
            if name == "lo" or name.startswith(cls._VIRTUAL_IFACE_PREFIXES[1:]):
                continue
            if name in slaves:
                continue
            if "." in name and name.split(".", 1)[0] in all_names:
                continue
            rx += r_b
            tx += t_b
        return rx, tx

    @classmethod
    def _read_net_bytes(cls, target_iface="auto"):
        try:
            with open("/proc/net/dev", "r", encoding="utf-8", errors="replace") as f:
                return cls.parse_net_dev_lines(f.readlines(), target_iface=target_iface)
        except OSError:
            return 0, 0

    @staticmethod
    def parse_diskstats_lines(lines):
        """Parse /proc/diskstats lines into (read_sectors, write_sectors, ios) for whole disks."""
        r_sec = w_sec = ios = 0
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            dev = parts[2]
            if not _WHOLE_DISK_RE.match(dev):
                continue
            try:
                r_ios = int(parts[3])
                r_sectors = int(parts[5])
                w_ios = int(parts[7])
                w_sectors = int(parts[9])
            except ValueError:
                continue
            r_sec += r_sectors
            w_sec += w_sectors
            ios += r_ios + w_ios
        return r_sec, w_sec, ios

    @classmethod
    def _read_diskstats(cls):
        try:
            with open("/proc/diskstats", "r", encoding="utf-8") as f:
                return cls.parse_diskstats_lines(f)
        except OSError:
            return 0, 0, 0

    @staticmethod
    def _read_vmstat():
        pswpin = pswpout = 0
        try:
            with open("/proc/vmstat", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("pswpin "):
                        pswpin = int(line.split()[1])
                    elif line.startswith("pswpout "):
                        pswpout = int(line.split()[1])
        except (OSError, ValueError, IndexError):
            pass
        return pswpin, pswpout

    def cpu_breakdown(self):
        """Return dict with total, user, sys, iowait, steal percentages and ctxt/intr rates."""
        now = time.monotonic()
        elapsed = max(0.1, now - self._prev_cpu_time)
        cur_vals, cur_ctxt, cur_intr = self._read_cpu_stat_full()
        prev_vals, prev_ctxt, prev_intr = self._prev_cpu_full
        self._prev_cpu_full = (cur_vals, cur_ctxt, cur_intr)
        self._prev_cpu_time = now

        deltas = [max(0, c - p) for c, p in zip(cur_vals, prev_vals)]
        total_dt = sum(deltas)
        if total_dt <= 0:
            return {
                "total": 0.0, "user": 0.0, "sys": 0.0,
                "iowait": 0.0, "steal": 0.0, "ctxt_per_s": 0.0, "intr_per_s": 0.0,
            }
        # /proc/stat columns: 0:user 1:nice 2:system 3:idle 4:iowait 5:irq 6:softirq 7:steal
        idle = deltas[3] + deltas[4]
        user = deltas[0] + deltas[1]
        sys_t = deltas[2] + deltas[5] + deltas[6]
        iowait = deltas[4]
        steal = deltas[7]
        return {
            "total": max(0.0, min(100.0, 100.0 * (total_dt - idle) / total_dt)),
            "user": 100.0 * user / total_dt,
            "sys": 100.0 * sys_t / total_dt,
            "iowait": 100.0 * iowait / total_dt,
            "steal": 100.0 * steal / total_dt,
            "ctxt_per_s": max(0.0, (cur_ctxt - prev_ctxt) / elapsed),
            "intr_per_s": max(0.0, (cur_intr - prev_intr) / elapsed),
        }

    def cpu_percent(self):
        return self.cpu_breakdown()["total"]

    def net_rates(self, target_iface="auto"):
        now = time.monotonic()
        elapsed = max(0.1, now - self._prev_net_time)
        rx, tx = self._read_net_bytes(target_iface=target_iface)
        prx, ptx = self._prev_net
        self._prev_net = (rx, tx)
        self._prev_net_time = now
        return max(0.0, (rx - prx) / elapsed), max(0.0, (tx - ptx) / elapsed)

    def disk_io_rates(self):
        """Return (read_kbps, write_kbps, iops) across physical block devices."""
        now = time.monotonic()
        elapsed = max(0.1, now - self._prev_disk_time)
        r_sec, w_sec, ios = self._read_diskstats()
        pr_sec, pw_sec, pios = self._prev_disk
        self._prev_disk = (r_sec, w_sec, ios)
        self._prev_disk_time = now
        # Linux /proc/diskstats sectors are 512 bytes
        read_kbps = max(0.0, ((r_sec - pr_sec) * 512.0 / 1024.0) / elapsed)
        write_kbps = max(0.0, ((w_sec - pw_sec) * 512.0 / 1024.0) / elapsed)
        iops = max(0.0, (ios - pios) / elapsed)
        return read_kbps, write_kbps, iops

    def vmstat_rates(self):
        """Return (pswpin_per_s, pswpout_per_s)."""
        now = time.monotonic()
        elapsed = max(0.1, now - self._prev_vmstat_time)
        pin, pout = self._read_vmstat()
        ppin, ppout = self._prev_vmstat
        self._prev_vmstat = (pin, pout)
        self._prev_vmstat_time = now
        return max(0.0, (pin - ppin) / elapsed), max(0.0, (pout - ppout) / elapsed)

    @staticmethod
    def memory():
        info = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0]) * 1024
        total = info.get("MemTotal", 1)
        avail = info.get("MemAvailable", 0)
        used = total - avail
        swap_total = info.get("SwapTotal", 0)
        swap_free = info.get("SwapFree", 0)
        return used, total, max(0, swap_total - swap_free), swap_total

    @staticmethod
    def disk(path="/"):
        try:
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            return used, total, free
        except OSError:
            return 0, 0, 0

    @staticmethod
    def cpu_temps(thermal_dir="/sys/class/thermal", hwmon_dir="/sys/class/hwmon"):
        """Return list of CPU package temperatures (supports multi-socket servers)."""
        pkg_temps = []
        fallback = None
        if os.path.isdir(thermal_dir):
            for entry in sorted(os.listdir(thermal_dir)):
                if not entry.startswith("thermal_zone"):
                    continue
                try:
                    with open(os.path.join(thermal_dir, entry, "type"), encoding="utf-8") as ft:
                        tname = ft.read().strip().lower()
                    with open(os.path.join(thermal_dir, entry, "temp"), encoding="utf-8") as fv:
                        val = int(fv.read().strip()) / 1000.0
                    if "x86_pkg_temp" in tname or "cpu" in tname:
                        pkg_temps.append(val)
                    elif fallback is None and 10 < val < 120:
                        fallback = val
                except (OSError, ValueError):
                    continue
        if pkg_temps:
            return pkg_temps

        hw_temps = []
        if os.path.isdir(hwmon_dir):
            for entry in sorted(os.listdir(hwmon_dir)):
                hpath = os.path.join(hwmon_dir, entry)
                try:
                    with open(os.path.join(hpath, "name"), encoding="utf-8") as fn:
                        hname = fn.read().strip().lower()
                    if hname not in ("k10temp", "zenpower", "coretemp"):
                        continue
                    with open(os.path.join(hpath, "temp1_input"), encoding="utf-8") as ft:
                        hw_temps.append(int(ft.read().strip()) / 1000.0)
                except (OSError, ValueError):
                    continue
        if hw_temps:
            return hw_temps
        return [fallback] if fallback is not None else []

    @classmethod
    def cpu_temp(cls, thermal_dir="/sys/class/thermal", hwmon_dir="/sys/class/hwmon"):
        temps = cls.cpu_temps(thermal_dir=thermal_dir, hwmon_dir=hwmon_dir)
        return temps[0] if temps else None

    @staticmethod
    def battery_status():
        ps_dir = "/sys/class/power_supply"
        if not os.path.isdir(ps_dir):
            return None
        for entry in sorted(os.listdir(ps_dir)):
            if not entry.startswith("BAT"):
                continue
            bdir = os.path.join(ps_dir, entry)
            try:
                with open(os.path.join(bdir, "capacity"), encoding="utf-8") as fc:
                    cap = fc.read().strip()
                with open(os.path.join(bdir, "status"), encoding="utf-8") as fs:
                    st = fs.read().strip()
                icon = "⚡" if st == "Charging" else ("✓" if st == "Full" else "🔋")
                return f"{icon} {cap}% ({st})"
            except OSError:
                continue
        return None

    @staticmethod
    def _read_amd_card(dev_dir: str) -> Optional[dict[str, Any]]:
        """Read a single active AMD GPU sysfs device directory."""
        try:
            with open(os.path.join(dev_dir, "vendor"), encoding="utf-8") as fv:
                if fv.read().strip().lower() != "0x1002":
                    return None
            rpath = os.path.join(dev_dir, "power/runtime_status")
            if os.path.exists(rpath):
                with open(rpath, encoding="utf-8") as fr:
                    if fr.read().strip() != "active":
                        return None
        except OSError:
            return None

        util = temp = vram_u = vram_t = None
        try:
            with open(os.path.join(dev_dir, "gpu_busy_percent"), encoding="utf-8") as f:
                util = float(f.read().strip())
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(dev_dir, "mem_info_vram_used"), encoding="utf-8") as fu:
                vram_u = int(fu.read().strip()) / (1024 * 1024)
            with open(os.path.join(dev_dir, "mem_info_vram_total"), encoding="utf-8") as ft:
                vram_t = int(ft.read().strip()) / (1024 * 1024)
        except (OSError, ValueError):
            pass
        hwmon_root = os.path.join(dev_dir, "hwmon")
        if os.path.isdir(hwmon_root):
            for hw in sorted(os.listdir(hwmon_root)):
                tpath = os.path.join(hwmon_root, hw, "temp1_input")
                if os.path.exists(tpath):
                    try:
                        with open(tpath, encoding="utf-8") as ftemp:
                            temp = int(ftemp.read().strip()) / 1000.0
                        break
                    except (OSError, ValueError):
                        pass

        if util is None and vram_t is None:
            return None
        parts = []
        if util is not None:
            parts.append(f"{util:.0f}%")
        if temp is not None:
            parts.append(f"{temp:.0f}°C")
        if vram_u is not None and vram_t is not None:
            parts.append(f"{vram_u:.0f}/{vram_t:.0f} MiB")
        return {
            "vendor": "amd",
            "util_pct": util,
            "temp_c": temp,
            "vram_used_mb": round(vram_u, 1) if vram_u is not None else None,
            "vram_total_mb": round(vram_t, 1) if vram_t is not None else None,
            "label": " · ".join(parts) or "AMD activa",
        }

    @classmethod
    def read_amd_gpus_sysfs(cls, drm_dir: str = "/sys/class/drm") -> list[dict[str, Any]]:
        """Return list of active AMD GPU (amdgpu) dicts from /sys/class/drm/card*/device."""
        if not os.path.isdir(drm_dir):
            return []
        gpus: list[dict[str, Any]] = []
        for entry in sorted(os.listdir(drm_dir)):
            if not re.fullmatch(r"card\d+", entry):
                continue
            card = cls._read_amd_card(os.path.join(drm_dir, entry, "device"))
            if card is not None:
                gpus.append(card)
        return gpus

    @classmethod
    def read_amd_gpu_sysfs(cls, drm_dir="/sys/class/drm"):
        gpus = cls.read_amd_gpus_sysfs(drm_dir=drm_dir)
        if not gpus:
            return None
        g = gpus[0]
        return g["label"], g["util_pct"], g["temp_c"], g["vram_used_mb"], g["vram_total_mb"]

    @classmethod
    def gpu_list(cls, pci_dir="/sys/bus/pci/devices", drm_dir="/sys/class/drm"):
        """Return list of all active NVIDIA and AMD GPUs without waking D3cold devices."""
        gpus = []
        has_nvidia = False
        active_nvidia = False
        if os.path.isdir(pci_dir):
            for dev in sorted(os.listdir(pci_dir)):
                try:
                    with open(os.path.join(pci_dir, dev, "vendor"), encoding="utf-8") as fv:
                        if fv.read().strip().lower() != "0x10de":
                            continue
                    has_nvidia = True
                    rpath = os.path.join(pci_dir, dev, "power/runtime_status")
                    if not os.path.exists(rpath):
                        active_nvidia = True
                    else:
                        with open(rpath, encoding="utf-8") as fr:
                            if fr.read().strip() == "active":
                                active_nvidia = True
                except OSError:
                    continue

        if active_nvidia:
            nv_smi = shutil.which("nvidia-smi", path=TRUSTED_PATH)
            if nv_smi:
                try:
                    out = subprocess.check_output(
                        [
                            nv_smi,
                            "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits",
                        ],
                        timeout=1.0,
                        text=True,
                        stderr=subprocess.DEVNULL,
                    ).strip()
                    for line in out.splitlines():
                        if not line.strip():
                            continue
                        parts = [x.strip() for x in line.split(",")]
                        if len(parts) < 4:
                            continue

                        def _nv_float(val):
                            try:
                                return float(val)
                            except ValueError:
                                return None

                        util, temp, mem_u, mem_t = (_nv_float(parts[i]) for i in range(4))
                        if util is None and temp is None and mem_u is None:
                            continue
                        lbl_parts = []
                        if util is not None:
                            lbl_parts.append(f"{util:.0f}%")
                        if temp is not None:
                            lbl_parts.append(f"{temp:.0f}°C")
                        if mem_u is not None and mem_t is not None:
                            lbl_parts.append(f"{mem_u:.0f}/{mem_t:.0f} MiB")
                        gpus.append({
                            "vendor": "nvidia",
                            "util_pct": util,
                            "temp_c": temp,
                            "vram_used_mb": mem_u,
                            "vram_total_mb": mem_t,
                            "label": " · ".join(lbl_parts) or "NVIDIA activa",
                        })
                except (OSError, subprocess.SubprocessError) as exc:
                    logging.warning("Error consultando nvidia-smi: %s", exc)

        gpus.extend(cls.read_amd_gpus_sysfs(drm_dir=drm_dir))
        for idx, g in enumerate(gpus):
            g["index"] = idx
        return gpus, has_nvidia

    @staticmethod
    def _summarize_gpus(gpus, has_nvidia):
        if not gpus:
            return ("En reposo (ahorro)" if has_nvidia else "Integrada activa"), None, None
        g0 = gpus[0]
        if len(gpus) == 1:
            return g0["label"], g0, None
        summary = " | ".join(
            f"G{g['index']}:{g['util_pct']:.0f}%·{g['temp_c']:.0f}°C"
            if g["util_pct"] is not None and g["temp_c"] is not None
            else f"G{g['index']}:{g['label']}"
            for g in gpus[:2]
        )
        return summary, g0, gpus[1]

    @classmethod
    def gpu_metrics(cls):
        """Return (label, util_pct, temp_c, vram_used_mb, vram_total_mb) for primary GPU."""
        gpus, has_nvidia = cls.gpu_list()
        label, g0, _ = cls._summarize_gpus(gpus, has_nvidia)
        if g0 is None:
            return label, None, None, None, None
        return label, g0["util_pct"], g0["temp_c"], g0["vram_used_mb"], g0["vram_total_mb"]

    @classmethod
    def gpu_status(cls):
        return cls.gpu_metrics()[0]

    @staticmethod
    def parse_proc_stat(raw):
        """Parse /proc/<pid>/stat into (name, total_ticks, rss_pages, starttime) or None."""
        rparen = raw.rfind(")")
        lparen = raw.find("(")
        if lparen == -1 or rparen <= lparen:
            return None
        name = raw[lparen + 1 : rparen]
        fields = raw[rparen + 2 :].split()
        if len(fields) <= 21:
            return None
        utime = int(fields[11])
        stime = int(fields[12])
        starttime = int(fields[19])
        rss_pages = int(fields[21])
        return name, utime + stime, rss_pages, starttime

    def top_processes_detail(self, limit=3):
        """Read /proc/<pid>/stat directly; return (cpu_text, top_cpu_name, top_ram_name)."""
        now = time.monotonic()
        elapsed = max(0.1, now - self._prev_proc_time)
        cur_ticks = {}
        results = []
        mem_results = []
        mem_total_bytes = self._mem_total_kb * 1024

        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
                    parsed = self.parse_proc_stat(f.read())
                if parsed is None:
                    continue
                name, ticks, rss_pages, starttime = parsed
                key = (pid, starttime)
                cur_ticks[key] = ticks
                mem_pct = 100.0 * (rss_pages * PAGE_SIZE) / mem_total_bytes
                mem_results.append((mem_pct, name))

                if key in self._prev_proc_ticks:
                    dticks = max(0, ticks - self._prev_proc_ticks[key])
                    cpu_pct = 100.0 * (dticks / CLK_TCK) / elapsed
                    results.append((cpu_pct, mem_pct, name))
            except (OSError, ValueError, IndexError):
                continue

        self._prev_proc_ticks = cur_ticks
        self._prev_proc_time = now
        top_ram_name = max(mem_results, key=lambda x: x[0])[1] if mem_results else ""
        if not results:
            return "Calculando...", "", top_ram_name
        results.sort(key=lambda item: item[0], reverse=True)
        text = "\n".join(
            f"{name[:18]:<18} {cpu:5.1f}%  {mem:4.1f}%"
            for cpu, mem, name in results[:limit]
        )
        return text, results[0][2], top_ram_name

    def top_processes(self, limit=3):
        text, top_cpu, _ = self.top_processes_detail(limit=limit)
        return text, top_cpu

    @staticmethod
    def uptime_and_load():
        with open("/proc/uptime", encoding="utf-8") as f:
            secs = int(float(f.read().split()[0]))
        h, rem = divmod(secs, 3600)
        m, _ = divmod(rem, 60)
        load1 = os.getloadavg()[0]
        return f"Up {h}h {m:02d}m · Load {load1:.2f}", load1

    def collect_snapshot(self, mount_path="/", top_limit=3, net_iface="auto"):
        """Collect a complete system snapshot dictionary."""
        now_dt = datetime.now()
        cpu_b = self.cpu_breakdown()
        temps = self.cpu_temps()
        temp = temps[0] if temps else None
        temp1 = temps[1] if len(temps) > 1 else None
        used_m, total_m, used_s, total_s = self.memory()
        ram_pct = (100.0 * used_m / total_m) if total_m else 0.0
        swap_pct = (100.0 * used_s / total_s) if total_s else 0.0
        used_d, total_d, _ = self.disk(mount_path)
        disk_pct = (100.0 * used_d / total_d) if total_d else 0.0
        rx_bps, tx_bps = self.net_rates(target_iface=net_iface)
        disk_r_kbps, disk_w_kbps, disk_iops = self.disk_io_rates()
        pswpin, pswpout = self.vmstat_rates()
        gpus, has_nvidia = self.gpu_list()
        gpu_label, g0, g1 = self._summarize_gpus(gpus, has_nvidia)
        gpu_util = g0["util_pct"] if g0 else None
        gpu_temp = g0["temp_c"] if g0 else None
        gpu_vram_u = g0["vram_used_mb"] if g0 else None
        gpu_vram_t = g0["vram_total_mb"] if g0 else None
        gpu1_util = g1["util_pct"] if g1 else None
        gpu1_temp = g1["temp_c"] if g1 else None
        gpu1_vram_u = g1["vram_used_mb"] if g1 else None
        gpu1_vram_t = g1["vram_total_mb"] if g1 else None
        up_text, load1 = self.uptime_and_load()
        proc_text, top_cpu, top_ram = self.top_processes_detail(top_limit)
        return {
            "timestamp": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now_dt.strftime("%Y-%m-%d"),
            "hostname": HOSTNAME,
            "cpu_pct": round(cpu_b["total"], 2),
            "cpu_user_pct": round(cpu_b["user"], 2),
            "cpu_sys_pct": round(cpu_b["sys"], 2),
            "cpu_iowait_pct": round(cpu_b["iowait"], 2),
            "cpu_steal_pct": round(cpu_b["steal"], 2),
            "ctxt_per_s": round(cpu_b["ctxt_per_s"], 1),
            "intr_per_s": round(cpu_b["intr_per_s"], 1),
            "temp_c": round(temp, 1) if temp is not None else None,
            "temp_cpu1_c": round(temp1, 1) if temp1 is not None else None,
            "ram_used_bytes": used_m,
            "ram_total_bytes": total_m,
            "ram_used_gb": round(used_m / (1024 ** 3), 2),
            "ram_total_gb": round(total_m / (1024 ** 3), 2),
            "ram_pct": round(ram_pct, 1),
            "swap_used_bytes": used_s,
            "swap_total_bytes": total_s,
            "swap_pct": round(swap_pct, 1),
            "disk_used_bytes": used_d,
            "disk_total_bytes": total_d,
            "disk_pct": round(disk_pct, 1),
            "rx_bps": round(rx_bps, 1),
            "tx_bps": round(tx_bps, 1),
            "rx_kbps": round(rx_bps / 1024.0, 1),
            "tx_kbps": round(tx_bps / 1024.0, 1),
            "disk_read_kbps": round(disk_r_kbps, 1),
            "disk_write_kbps": round(disk_w_kbps, 1),
            "disk_iops": round(disk_iops, 1),
            "pswpin_per_s": round(pswpin, 2),
            "pswpout_per_s": round(pswpout, 2),
            "load1": round(load1, 2),
            "uptime_text": up_text,
            "top_proc": top_cpu,
            "top_ram_proc": top_ram,
            "proc_text": proc_text,
            "gpu_label": gpu_label,
            "gpu_count": len(gpus),
            "gpus": gpus,
            "gpu_util_pct": gpu_util,
            "gpu_temp_c": gpu_temp,
            "gpu_vram_used_mb": gpu_vram_u,
            "gpu_vram_total_mb": gpu_vram_t,
            "gpu1_util_pct": gpu1_util,
            "gpu1_temp_c": gpu1_temp,
            "gpu1_vram_used_mb": gpu1_vram_u,
            "gpu1_vram_total_mb": gpu1_vram_t,
            "battery": self.battery_status(),
        }


_LOG_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.csv(\.gz)?$")


class CsvRecorder:
    """Buffered daily CSV writer with automatic gzip compression and retention."""

    def __init__(self, log_dir=LOG_DIR, flush_every=15, retention_days=30, compress_after_days=1):
        self.log_dir = log_dir
        self.flush_every = max(1, int(flush_every))
        self.retention_days = max(0, int(retention_days))
        self.compress_after_days = max(0, int(compress_after_days))
        self._buffer = []
        self._buffer_date = None
        self._last_rotated_date = None

    @staticmethod
    def snapshot_to_row(snap):
        """Convert a snapshot dict to a CSV row matching CSV_HEADER."""
        return [
            snap["timestamp"],
            f"{snap['cpu_pct']:.1f}",
            f"{snap['temp_c']:.1f}" if snap["temp_c"] is not None else "",
            f"{snap['ram_used_gb']:.2f}",
            f"{snap['ram_total_gb']:.2f}",
            f"{snap['ram_pct']:.1f}",
            f"{snap['swap_pct']:.1f}",
            f"{snap['disk_pct']:.1f}",
            f"{snap['rx_kbps']:.1f}",
            f"{snap['tx_kbps']:.1f}",
            f"{snap['load1']:.2f}",
            sanitize_csv_cell(snap["top_proc"]),
            f"{snap['cpu_user_pct']:.1f}",
            f"{snap['cpu_sys_pct']:.1f}",
            f"{snap['cpu_iowait_pct']:.1f}",
            f"{snap['cpu_steal_pct']:.1f}",
            f"{snap['ctxt_per_s']:.1f}",
            f"{snap['intr_per_s']:.1f}",
            f"{snap['disk_read_kbps']:.1f}",
            f"{snap['disk_write_kbps']:.1f}",
            f"{snap['disk_iops']:.1f}",
            f"{snap['pswpin_per_s']:.2f}",
            f"{snap['pswpout_per_s']:.2f}",
            sanitize_csv_cell(snap["top_ram_proc"]),
            f"{snap['gpu_util_pct']:.1f}" if snap["gpu_util_pct"] is not None else "",
            f"{snap['gpu_temp_c']:.1f}" if snap["gpu_temp_c"] is not None else "",
            f"{snap['gpu_vram_used_mb']:.0f}" if snap["gpu_vram_used_mb"] is not None else "",
            f"{snap['gpu_vram_total_mb']:.0f}" if snap["gpu_vram_total_mb"] is not None else "",
            sanitize_csv_cell(snap.get("hostname", HOSTNAME)),
            f"{snap['temp_cpu1_c']:.1f}" if snap.get("temp_cpu1_c") is not None else "",
            str(snap.get("gpu_count", 1 if snap["gpu_util_pct"] is not None else 0)),
            f"{snap['gpu1_util_pct']:.1f}" if snap.get("gpu1_util_pct") is not None else "",
            f"{snap['gpu1_temp_c']:.1f}" if snap.get("gpu1_temp_c") is not None else "",
            f"{snap['gpu1_vram_used_mb']:.0f}" if snap.get("gpu1_vram_used_mb") is not None else "",
            f"{snap['gpu1_vram_total_mb']:.0f}" if snap.get("gpu1_vram_total_mb") is not None else "",
        ]

    def rotate_logs(self, today_str=None):
        """Compress old daily CSVs to .csv.gz and prune logs older than retention_days."""
        if not os.path.isdir(self.log_dir):
            return
        try:
            today = date.fromisoformat(today_str) if today_str else date.today()
        except ValueError:
            today = date.today()

        for fname in sorted(os.listdir(self.log_dir)):
            m = _LOG_FILE_RE.match(fname)
            if not m:
                continue
            try:
                file_date = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            age_days = (today - file_date).days
            if age_days <= 0:
                continue
            fpath = os.path.join(self.log_dir, fname)
            if os.path.islink(fpath):
                continue
            is_gz = bool(m.group(2))

            if self.retention_days > 0 and age_days > self.retention_days:
                try:
                    os.remove(fpath)
                except OSError as exc:
                    logging.warning("No se pudo eliminar log antiguo %s: %s", fpath, exc)
                continue

            if not is_gz and self.compress_after_days > 0 and age_days >= self.compress_after_days:
                gz_path = f"{fpath}.gz"
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=self.log_dir, prefix=f".{fname}.", suffix=".gz.tmp", delete=False
                    ) as tf:
                        tmp_path = tf.name
                    with open(fpath, "rb") as src, gzip.open(tmp_path, "wb", compresslevel=6) as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(tmp_path, gz_path)
                    os.remove(fpath)
                except OSError as exc:
                    logging.warning("No se pudo comprimir %s: %s", fpath, exc)
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass

    def record(self, snap):
        today = snap["date"]
        if self._buffer_date and self._buffer_date != today:
            self.flush()
        self._buffer_date = today
        if self._last_rotated_date != today:
            self.rotate_logs(today)
            self._last_rotated_date = today
        self._buffer.append(self.snapshot_to_row(snap))
        if len(self._buffer) >= self.flush_every:
            self.flush()

    def flush(self):
        if not self._buffer:
            return
        date_str = self._buffer_date or datetime.now().strftime("%Y-%m-%d")
        csv_path = os.path.join(self.log_dir, f"{date_str}.csv")
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(CSV_HEADER)
                writer.writerows(self._buffer)
            self._buffer.clear()
        except OSError as exc:
            logging.warning("No se pudo escribir CSV %s: %s", csv_path, exc)
            if len(self._buffer) > 500:
                self._buffer = self._buffer[-500:]


def _prom_escape(val: str) -> str:
    return str(val).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_prometheus_metrics(snap: dict[str, Any]) -> str:
    """Render a collect_snapshot() dict into Prometheus text exposition format."""
    host = _prom_escape(snap.get("hostname", HOSTNAME))
    lines = [
        "# HELP dsmon_cpu_percent Total CPU utilization percentage.",
        "# TYPE dsmon_cpu_percent gauge",
        f'dsmon_cpu_percent{{host="{host}"}} {snap["cpu_pct"]}',
        f'dsmon_cpu_user_percent{{host="{host}"}} {snap["cpu_user_pct"]}',
        f'dsmon_cpu_sys_percent{{host="{host}"}} {snap["cpu_sys_pct"]}',
        f'dsmon_cpu_iowait_percent{{host="{host}"}} {snap["cpu_iowait_pct"]}',
        f'dsmon_cpu_steal_percent{{host="{host}"}} {snap["cpu_steal_pct"]}',
        f'dsmon_context_switches_per_second{{host="{host}"}} {snap["ctxt_per_s"]}',
        f'dsmon_interrupts_per_second{{host="{host}"}} {snap["intr_per_s"]}',
        f'dsmon_load1{{host="{host}"}} {snap["load1"]}',
        f'dsmon_memory_used_bytes{{host="{host}"}} {snap["ram_used_bytes"]}',
        f'dsmon_memory_total_bytes{{host="{host}"}} {snap["ram_total_bytes"]}',
        f'dsmon_memory_percent{{host="{host}"}} {snap["ram_pct"]}',
        f'dsmon_swap_percent{{host="{host}"}} {snap["swap_pct"]}',
        f'dsmon_disk_used_bytes{{host="{host}"}} {snap["disk_used_bytes"]}',
        f'dsmon_disk_total_bytes{{host="{host}"}} {snap["disk_total_bytes"]}',
        f'dsmon_disk_percent{{host="{host}"}} {snap["disk_pct"]}',
        f'dsmon_disk_read_kbps{{host="{host}"}} {snap["disk_read_kbps"]}',
        f'dsmon_disk_write_kbps{{host="{host}"}} {snap["disk_write_kbps"]}',
        f'dsmon_disk_iops{{host="{host}"}} {snap["disk_iops"]}',
        f'dsmon_net_rx_kbps{{host="{host}"}} {snap["rx_kbps"]}',
        f'dsmon_net_tx_kbps{{host="{host}"}} {snap["tx_kbps"]}',
        f'dsmon_vmstat_pswpin_per_second{{host="{host}"}} {snap["pswpin_per_s"]}',
        f'dsmon_vmstat_pswpout_per_second{{host="{host}"}} {snap["pswpout_per_s"]}',
        f'dsmon_gpu_count{{host="{host}"}} {snap.get("gpu_count", 0)}',
    ]
    if snap.get("temp_c") is not None:
        lines.append(f'dsmon_cpu_temp_celsius{{host="{host}",socket="0"}} {snap["temp_c"]}')
    if snap.get("temp_cpu1_c") is not None:
        lines.append(f'dsmon_cpu_temp_celsius{{host="{host}",socket="1"}} {snap["temp_cpu1_c"]}')
    for gpu in snap.get("gpus", []):
        idx = gpu.get("index", 0)
        vendor = _prom_escape(gpu.get("vendor", "unknown"))
        lbls = f'host="{host}",gpu="{idx}",vendor="{vendor}"'
        if gpu.get("util_pct") is not None:
            lines.append(f"dsmon_gpu_utilization_percent{{{lbls}}} {gpu['util_pct']}")
        if gpu.get("temp_c") is not None:
            lines.append(f"dsmon_gpu_temp_celsius{{{lbls}}} {gpu['temp_c']}")
        if gpu.get("vram_used_mb") is not None:
            lines.append(f"dsmon_gpu_vram_used_mb{{{lbls}}} {gpu['vram_used_mb']}")
        if gpu.get("vram_total_mb") is not None:
            lines.append(f"dsmon_gpu_vram_total_mb{{{lbls}}} {gpu['vram_total_mb']}")
    return "\n".join(lines) + "\n"
