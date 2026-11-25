#!/usr/bin/env python3
"""Unit tests for DSMon parsers, CSV handling, and HTML/JSON escaping."""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dsmon
import dsmon_analyzer
import dsmon_core


class TestProcStatParser(unittest.TestCase):
    def test_parses_comm_with_spaces_and_parens(self):
        # 24 fields after ') ': state at 0, utime=11, stime=12, starttime=19, rss=21
        fields = ["S"] + ["0"] * 10 + ["150", "50"] + ["0"] * 6 + ["98765", "0", "4096", "0"]
        raw = f"1234 (Web Content (1)) {' '.join(fields)}"
        parsed = dsmon.SystemStats.parse_proc_stat(raw)
        self.assertEqual(parsed, ("Web Content (1)", 200, 4096, 98765))

    def test_rejects_malformed_stat(self):
        self.assertIsNone(dsmon.SystemStats.parse_proc_stat("bad line"))
        self.assertIsNone(dsmon.SystemStats.parse_proc_stat("123 (foo) S 1 2 3"))


class TestAnalyzerSafety(unittest.TestCase):
    def test_safe_json_escapes_script_breakout(self):
        payload = {"msg": "</script><script>alert(1)</script> & >"}
        encoded = dsmon_analyzer.safe_json_for_html(payload)
        self.assertNotIn("</script>", encoded)
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertNotIn("&", encoded)

    def test_load_all_logs_skips_corrupt_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "2026-11-18.csv")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("timestamp,cpu_pct,temp_c,ram_used_gb,ram_total_gb,ram_pct,swap_pct,disk_pct,rx_kbps,tx_kbps,load1,top_proc\n")
                f.write("2026-11-18 21:00:00,12.5,50.0,4.0,32.0,12.5,0.0,20.0,10.0,2.0,0.50,python3\n")
                f.write("2026-11-18 21:00:02\n")  # truncated row -> None fields
                f.write("bad,row,data\n")
            orig_dir = dsmon_analyzer.LOG_DIR
            try:
                dsmon_analyzer.LOG_DIR = tmp
                data = dsmon_analyzer.load_all_logs()
            finally:
                dsmon_analyzer.LOG_DIR = orig_dir
            self.assertEqual(len(data["2026-11-18"]), 1)
            self.assertEqual(data["2026-11-18"][0]["top"], "python3")

    def test_build_report_single_pass_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig_rep = dsmon_analyzer.REPORT_DIR
            try:
                dsmon_analyzer.REPORT_DIR = tmp
                all_data = {
                    "2026-11-18": [{
                        "t": "21:00:00", "cpu": 5.0, "temp": 45.0, "ram": 20.0,
                        "swap": 0.0, "rx": 1.0, "tx": 1.0, "load": 0.3,
                        "top": "__ALL_LOGS_JSON__</script>",
                    }]
                }
                out = dsmon_analyzer.build_report(all_data, "2026-11-18")
                self.assertEqual(os.stat(out).st_mode & 0o777, 0o644)
                with open(out, encoding="utf-8") as f:
                    html = f.read()
            finally:
                dsmon_analyzer.REPORT_DIR = orig_rep
            self.assertNotIn("__ALL_LOGS_JSON__</script>", html)


class TestCoreAndCli(unittest.TestCase):
    def test_parse_diskstats_filters_partitions(self):
        lines = [
            " 259       0 nvme0n1 100 0 2000 10 50 0 1000 5 0 0 0",
            " 259       1 nvme0n1p1 100 0 2000 10 50 0 1000 5 0 0 0",
            "   7       0 loop0 10 0 200 0 0 0 0 0 0 0 0",
            "   8       0 sda 20 0 400 2 10 0 200 1 0 0 0",
        ]
        r_sec, w_sec, ios = dsmon_core.SystemStats.parse_diskstats_lines(lines)
        self.assertEqual(r_sec, 2400)
        self.assertEqual(w_sec, 1200)
        self.assertEqual(ios, 180)

    def test_csv_recorder_writes_header_and_flushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = dsmon_core.CsvRecorder(log_dir=tmp, flush_every=2)
            snap = {
                "timestamp": "2026-11-25 15:00:00", "date": "2026-11-25",
                "cpu_pct": 12.3, "temp_c": 48.0, "ram_used_gb": 4.5, "ram_total_gb": 16.0,
                "ram_pct": 28.1, "swap_pct": 0.0, "disk_pct": 40.0,
                "rx_kbps": 10.5, "tx_kbps": 2.1, "load1": 0.45, "top_proc": "python3",
                "cpu_user_pct": 10.0, "cpu_sys_pct": 2.0, "cpu_iowait_pct": 0.3, "cpu_steal_pct": 0.0,
                "ctxt_per_s": 1200.0, "intr_per_s": 800.0,
                "disk_read_kbps": 50.0, "disk_write_kbps": 25.0, "disk_iops": 12.0,
                "pswpin_per_s": 0.0, "pswpout_per_s": 0.0, "top_ram_proc": "chrome",
                "gpu_util_pct": None, "gpu_temp_c": None, "gpu_vram_used_mb": None, "gpu_vram_total_mb": None,
            }
            rec.record(snap)
            csv_path = os.path.join(tmp, "2026-11-25.csv")
            self.assertFalse(os.path.exists(csv_path))  # buffered until 2
            rec.record(snap)
            self.assertTrue(os.path.exists(csv_path))
            with open(csv_path, encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], dsmon_core.CSV_HEADER)
            self.assertEqual(len(rows), 3)

    def test_cli_parser_modes(self):
        args = dsmon.build_parser().parse_args(["--headless", "--interval", "2.5", "--flush-every", "10"])
        self.assertTrue(args.headless)
        self.assertEqual(args.interval, 2.5)
        self.assertEqual(args.flush_every, 10)

    def test_load_settings_config_and_env_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf_path = os.path.join(tmp, "dsmon.conf")
            with open(conf_path, "w", encoding="utf-8") as f:
                f.write("[dsmon]\nlog_dir = /mnt/nmon/logs\ninterval = 10\nmount = /srv\n")
            orig_env = os.environ.get("DSMON_LOG_DIR")
            try:
                os.environ.pop("DSMON_LOG_DIR", None)
                cfg = dsmon_core.load_settings(conf_path)
                self.assertEqual(cfg["log_dir"], "/mnt/nmon/logs")
                self.assertEqual(cfg["interval"], 10.0)
                self.assertEqual(cfg["mount"], "/srv")

                os.environ["DSMON_LOG_DIR"] = "/var/log/dsmon-override"
                cfg2 = dsmon_core.load_settings(conf_path)
                self.assertEqual(cfg2["log_dir"], "/var/log/dsmon-override")
            finally:
                if orig_env is None:
                    os.environ.pop("DSMON_LOG_DIR", None)
                else:
                    os.environ["DSMON_LOG_DIR"] = orig_env

    def test_rotate_logs_compresses_and_prunes_and_analyzer_reads_gz(self):
        with tempfile.TemporaryDirectory() as tmp:
            today_csv = os.path.join(tmp, "2026-11-25.csv")
            yest_csv = os.path.join(tmp, "2026-11-24.csv")
            old_csv = os.path.join(tmp, "2026-10-01.csv")
            header = ",".join(dsmon_core.CSV_HEADER) + "\n"
            row = "2026-11-24 12:00:00,10.0,45.0,4.0,16.0,25.0,0.0,30.0,1.0,1.0,0.20,python3\n"
            for p in (today_csv, yest_csv, old_csv):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(header + row)

            rec = dsmon_core.CsvRecorder(log_dir=tmp, retention_days=30, compress_after_days=1)
            rec.rotate_logs("2026-11-25")

            self.assertTrue(os.path.exists(today_csv))
            self.assertFalse(os.path.exists(yest_csv))
            self.assertTrue(os.path.exists(f"{yest_csv}.gz"))
            self.assertFalse(os.path.exists(old_csv))
            self.assertFalse(os.path.exists(f"{old_csv}.gz"))

            orig_dir = dsmon_analyzer.LOG_DIR
            try:
                dsmon_analyzer.LOG_DIR = tmp
                data = dsmon_analyzer.load_all_logs()
            finally:
                dsmon_analyzer.LOG_DIR = orig_dir
            self.assertIn("2026-11-24", data)
            self.assertIn("2026-11-25", data)
            self.assertEqual(data["2026-11-24"][0]["top"], "python3")

    def test_missing_mount_returns_zero_and_downsampling_caps_points(self):
        self.assertEqual(dsmon_core.SystemStats.disk("/__nonexistent_mount_dsmon__"), (0, 0, 0))
        with tempfile.TemporaryDirectory() as tmp:
            header = ",".join(dsmon_core.CSV_HEADER) + "\n"
            for day in ("2026-11-23", "2026-11-24", "2026-11-25"):
                with open(os.path.join(tmp, f"{day}.csv"), "w", encoding="utf-8") as f:
                    f.write(header)
                    for sec in range(20):
                        f.write(f"{day} 12:00:{sec:02d},10.0,45.0,4.0,16.0,25.0,0.0,30.0,1.0,1.0,0.20,python3\n")
            orig_dir = dsmon_analyzer.LOG_DIR
            try:
                dsmon_analyzer.LOG_DIR = tmp
                data = dsmon_analyzer.load_all_logs(max_days=2, max_points_per_day=5)
            finally:
                dsmon_analyzer.LOG_DIR = orig_dir
            self.assertEqual(sorted(data.keys()), ["2026-11-24", "2026-11-25"])
            self.assertEqual(len(data["2026-11-25"]), 5)
            self.assertEqual(data["2026-11-25"][0]["t"], "12:00:00")
            self.assertEqual(data["2026-11-25"][-1]["t"], "12:00:19")

    def test_amd_gpu_sysfs_and_hwmon_cpu_temp_and_net_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            # AMD GPU sysfs mock
            dev = os.path.join(tmp, "drm", "card0", "device")
            hw = os.path.join(dev, "hwmon", "hwmon1")
            os.makedirs(hw)
            with open(os.path.join(dev, "vendor"), "w", encoding="utf-8") as f:
                f.write("0x1002\n")
            with open(os.path.join(dev, "gpu_busy_percent"), "w", encoding="utf-8") as f:
                f.write("42\n")
            with open(os.path.join(dev, "mem_info_vram_used"), "w", encoding="utf-8") as f:
                f.write(str(512 * 1024 * 1024))
            with open(os.path.join(dev, "mem_info_vram_total"), "w", encoding="utf-8") as f:
                f.write(str(4096 * 1024 * 1024))
            with open(os.path.join(hw, "temp1_input"), "w", encoding="utf-8") as f:
                f.write("61000\n")

            res = dsmon_core.SystemStats.read_amd_gpu_sysfs(os.path.join(tmp, "drm"))
            self.assertEqual(res, ("42% · 61°C · 512/4096 MiB", 42.0, 61.0, 512.0, 4096.0))

            # AMD k10temp hwmon fallback
            cpu_hw = os.path.join(tmp, "hwmon", "hwmon0")
            os.makedirs(cpu_hw)
            with open(os.path.join(cpu_hw, "name"), "w", encoding="utf-8") as f:
                f.write("k10temp\n")
            with open(os.path.join(cpu_hw, "temp1_input"), "w", encoding="utf-8") as f:
                f.write("54500\n")
            temp = dsmon_core.SystemStats.cpu_temp(
                thermal_dir=os.path.join(tmp, "no_thermal"),
                hwmon_dir=os.path.join(tmp, "hwmon"),
            )
            self.assertEqual(temp, 54.5)

        # Virtual bridge/veth, bond slaves, and VLAN subinterface filtering in /proc/net/dev
        lines = [
            "Inter-|   Receive ...",
            " face |bytes ...",
            "    lo: 1000 0 0 0 0 0 0 0 1000 0 0 0 0 0 0 0",
            "docker0: 5000 0 0 0 0 0 0 0 5000 0 0 0 0 0 0 0",
            "veth12ab: 5000 0 0 0 0 0 0 0 5000 0 0 0 0 0 0 0",
            "  eth0: 2000 0 0 0 0 0 0 0 1000 0 0 0 0 0 0 0",
            "eth0.100: 2000 0 0 0 0 0 0 0 1000 0 0 0 0 0 0 0",
            " bond0: 3000 0 0 0 0 0 0 0 1500 0 0 0 0 0 0 0",
            "  eth1: 3000 0 0 0 0 0 0 0 1500 0 0 0 0 0 0 0",
        ]
        self.assertEqual(
            dsmon_core.SystemStats.parse_net_dev_lines(lines, bond_slaves={"eth1"}),
            (5000, 2500),
        )
        self.assertEqual(
            dsmon_core.SystemStats.parse_net_dev_lines(lines, target_iface="bond0", bond_slaves={"eth1"}),
            (3000, 1500),
        )

    def test_multi_socket_cpu_temps(self):
        with tempfile.TemporaryDirectory() as tmp:
            for idx, temp_mc in ((0, "49000"), (1, "39000")):
                z = os.path.join(tmp, f"thermal_zone{idx}")
                os.makedirs(z)
                with open(os.path.join(z, "type"), "w", encoding="utf-8") as ft:
                    ft.write("x86_pkg_temp\n")
                with open(os.path.join(z, "temp"), "w", encoding="utf-8") as fv:
                    fv.write(f"{temp_mc}\n")
            temps = dsmon_core.SystemStats.cpu_temps(thermal_dir=tmp, hwmon_dir=os.path.join(tmp, "none"))
            self.assertEqual(temps, [49.0, 39.0])

    def test_multi_gpu_mixed_nvidia_and_amd(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            # Mock NVIDIA PCI device (active) + 2 AMD cards (one active, one suspended)
            nv_dev = os.path.join(tmp, "pci", "0000:01:00.0")
            os.makedirs(os.path.join(nv_dev, "power"))
            with open(os.path.join(nv_dev, "vendor"), "w", encoding="utf-8") as f:
                f.write("0x10de\n")
            with open(os.path.join(nv_dev, "power", "runtime_status"), "w", encoding="utf-8") as f:
                f.write("active\n")

            amd0 = os.path.join(tmp, "drm", "card0", "device")
            os.makedirs(amd0)
            with open(os.path.join(amd0, "vendor"), "w", encoding="utf-8") as f:
                f.write("0x1002\n")
            with open(os.path.join(amd0, "gpu_busy_percent"), "w", encoding="utf-8") as f:
                f.write("35\n")
            with open(os.path.join(amd0, "mem_info_vram_used"), "w", encoding="utf-8") as f:
                f.write(str(1024 * 1024 * 1024))
            with open(os.path.join(amd0, "mem_info_vram_total"), "w", encoding="utf-8") as f:
                f.write(str(8192 * 1024 * 1024))

            with (
                mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"),
                mock.patch("subprocess.check_output", return_value="80, 68, 12000, 24576\n"),
            ):
                gpus, has_nv = dsmon_core.SystemStats.gpu_list(
                    pci_dir=os.path.join(tmp, "pci"),
                    drm_dir=os.path.join(tmp, "drm"),
                )
            self.assertTrue(has_nv)
            self.assertEqual(len(gpus), 2)
            self.assertEqual(gpus[0]["vendor"], "nvidia")
            self.assertEqual(gpus[0]["vram_total_mb"], 24576.0)
            self.assertEqual(gpus[1]["vendor"], "amd")
            self.assertEqual(gpus[1]["util_pct"], 35.0)
            self.assertEqual(gpus[1]["vram_used_mb"], 1024.0)

    def test_security_hardening_csv_formula_symlink_and_partial_config(self):
        self.assertEqual(dsmon_core.sanitize_csv_cell("=cmd|'/C calc'!A0"), "'=cmd|'/C calc'!A0")
        self.assertEqual(dsmon_core.sanitize_csv_cell("+1"), "'+1")
        self.assertEqual(dsmon_core.sanitize_csv_cell("-1"), "'-1")
        self.assertEqual(dsmon_core.sanitize_csv_cell("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(dsmon_core.sanitize_csv_cell("python3"), "python3")

        with tempfile.TemporaryDirectory() as tmp:
            # Blank integer key must not discard valid log_dir
            conf_path = os.path.join(tmp, "dsmon.conf")
            with open(conf_path, "w", encoding="utf-8") as f:
                f.write("[dsmon]\nlog_dir = /mnt/custom\nretention_days =\n")
            cfg = dsmon_core.load_settings(conf_path)
            self.assertEqual(cfg["log_dir"], "/mnt/custom")
            self.assertEqual(cfg["retention_days"], 30)

            # Symlinked CSV must be skipped by rotate_logs
            target = os.path.join(tmp, "secret.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("keep")
            link_csv = os.path.join(tmp, "2026-11-24.csv")
            os.symlink(target, link_csv)
            rec = dsmon_core.CsvRecorder(log_dir=tmp, retention_days=30, compress_after_days=1)
            rec.rotate_logs("2026-11-25")
            self.assertTrue(os.path.islink(link_csv))
            self.assertFalse(os.path.exists(f"{link_csv}.gz"))

    def test_prometheus_metrics_formatting_and_http_endpoint(self):
        import threading
        import urllib.request

        snap = {
            "hostname": "node-01",
            "cpu_pct": 14.5, "cpu_user_pct": 10.0, "cpu_sys_pct": 3.0,
            "cpu_iowait_pct": 1.5, "cpu_steal_pct": 0.0,
            "ctxt_per_s": 500.0, "intr_per_s": 300.0, "load1": 0.42,
            "ram_used_bytes": 4096, "ram_total_bytes": 16384, "ram_pct": 25.0, "swap_pct": 0.0,
            "disk_used_bytes": 1000, "disk_total_bytes": 2000, "disk_pct": 50.0,
            "disk_read_kbps": 12.0, "disk_write_kbps": 4.0, "disk_iops": 3.0,
            "rx_kbps": 1.2, "tx_kbps": 0.4,
            "pswpin_per_s": 0.0, "pswpout_per_s": 0.0,
            "temp_c": 49.0, "temp_cpu1_c": 39.0,
            "gpu_count": 1,
            "gpus": [{"index": 0, "vendor": "nvidia", "util_pct": 55.0, "temp_c": 65.0, "vram_used_mb": 2048.0, "vram_total_mb": 8192.0}],
        }
        text = dsmon_core.format_prometheus_metrics(snap)
        self.assertIn('dsmon_cpu_percent{host="node-01"} 14.5', text)
        self.assertIn('dsmon_cpu_temp_celsius{host="node-01",socket="1"} 39.0', text)
        self.assertIn('dsmon_gpu_vram_used_mb{host="node-01",gpu="0",vendor="nvidia"} 2048.0', text)

        state = {"lock": threading.Lock(), "snap": snap}
        srv = dsmon.start_metrics_server("127.0.0.1", 0, state)
        try:
            port = srv.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertIn("dsmon_cpu_percent", body)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
