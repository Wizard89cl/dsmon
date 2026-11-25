#!/usr/bin/env python3
"""DSMon: Lightweight Linux system monitor (GTK3 desktop HUD + headless CLI recorder)."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from dsmon_core import (
    LOG_DIR,
    REPORT_DIR,
    TRUSTED_PATH,
    CsvRecorder,
    SystemStats,
    fmt_bytes,
    fmt_rate,
    format_prometheus_metrics,
    load_settings,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

CONFIG_FILE = os.path.expanduser("~/.config/dsmon/widget.json")

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
except (ImportError, ValueError):
    Gdk = GLib = Gtk = None

SCALES = {
    "small": {"width": 300, "title": 13, "body": 11, "mono": 10},
    "medium": {"width": 336, "title": 14, "body": 12, "mono": 11},
    "large": {"width": 380, "title": 16, "body": 13.5, "mono": 12},
}


def build_css(scale_key):
    """Build GTK CSS for the selected font/width scale."""
    s = SCALES.get(scale_key, SCALES["medium"])
    return f"""
window#desktop-widget {{
    background-color: transparent;
}}
.card {{
    background-color: rgba(18, 20, 26, 0.86);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 14px;
    padding: 18px 20px;
}}
.title {{
    color: #f3f4f6;
    font-family: "Inter", sans-serif;
    font-size: {s["title"]}px;
    font-weight: 700;
}}
.compact-btn {{
    background: transparent;
    border: none;
    box-shadow: none;
    color: #9ca3af;
    padding: 0 4px;
    min-width: 18px;
    min-height: 18px;
    font-size: {s["body"]}px;
}}
.compact-btn:hover {{
    color: #f3f4f6;
}}
.subtitle {{
    color: #9ca3af;
    font-family: "Inter", sans-serif;
    font-size: {s["body"]}px;
}}
.section-label {{
    color: #d1d5db;
    font-family: "Inter", sans-serif;
    font-size: {s["body"]}px;
    font-weight: 600;
}}
.value-label {{
    color: #93c5fd;
    font-family: "Inter", sans-serif;
    font-size: {s["body"]}px;
    font-weight: 600;
}}
.mono-label {{
    color: #cbd5e1;
    font-family: monospace;
    font-size: {s["mono"]}px;
}}
progressbar trough {{
    background-color: rgba(255, 255, 255, 0.10);
    border-radius: 4px;
    min-height: 7px;
}}
progressbar progress {{
    background-color: #3689e6;
    border-radius: 4px;
    min-height: 7px;
}}
progressbar.warn progress {{
    background-color: #f59e0b;
}}
progressbar.crit progress {{
    background-color: #ef4444;
}}
.sep {{
    background-color: rgba(255, 255, 255, 0.08);
    min-height: 1px;
    margin: 4px 0;
}}
""".encode("utf-8")


_GtkWindow = Gtk.Window if Gtk is not None else object


class DesktopResourceWidget(_GtkWindow):
    """Desktop-anchored resource monitor window with right-click settings."""

    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_name("desktop-widget")
        self.set_title("Monitor de Recursos")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_below(True)
        self.set_accept_focus(False)
        self.stick()

        self.config = self._load_config()
        self.timer_id = None
        self._save_timer_id = None
        self._last_pos = (self.config.get("x"), self.config.get("y"))
        _cfg = load_settings()
        self.recorder = CsvRecorder(
            _cfg["log_dir"],
            retention_days=_cfg["retention_days"],
            compress_after_days=_cfg["compress_after_days"],
        )

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        self.set_app_paintable(True)

        self.css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            screen, self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_button_press)
        self.connect("configure-event", self._on_configure)
        screen.connect("monitors-changed", lambda _s: self._restore_position())

        self.stats = SystemStats()
        self._build_ui()
        self._apply_scale(self.config["scale"])
        self._restore_position()
        self._refresh()
        self._reset_timer()
        if self.config.get("compact", False):
            GLib.idle_add(lambda: self._set_compact(True) or False)

    @staticmethod
    def _load_config():
        defaults = {"scale": "medium", "interval": 2, "locked": False, "recording": False, "compact": False}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if data.get("scale") in SCALES:
                        defaults["scale"] = data["scale"]
                    if isinstance(data.get("interval"), int) and data["interval"] in (1, 2, 5):
                        defaults["interval"] = data["interval"]
                    if isinstance(data.get("locked"), bool):
                        defaults["locked"] = data["locked"]
                    if isinstance(data.get("recording"), bool):
                        defaults["recording"] = data["recording"]
                    if isinstance(data.get("compact"), bool):
                        defaults["compact"] = data["compact"]
                    if isinstance(data.get("x"), (int, float)) and isinstance(data.get("y"), (int, float)):
                        defaults["x"] = int(data["x"])
                        defaults["y"] = int(data["y"])
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        return defaults

    def _save_config(self):
        tmp_path = None
        try:
            cfg_dir = os.path.dirname(CONFIG_FILE)
            os.makedirs(cfg_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=cfg_dir, prefix=".dsmon-cfg.", suffix=".tmp", delete=False
            ) as f:
                tmp_path = f.name
                json.dump(self.config, f, indent=2)
            os.replace(tmp_path, CONFIG_FILE)
        except OSError as exc:
            logging.warning("No se pudo guardar config: %s", exc)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _build_ui(self):
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.card.get_style_context().add_class("card")
        self.add(self.card)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.title_lbl = Gtk.Label(label=self._title_text(), xalign=0)
        self.title_lbl.get_style_context().add_class("title")
        self.uptime_lbl = Gtk.Label(label="", xalign=1)
        self.uptime_lbl.get_style_context().add_class("subtitle")
        self.compact_btn = Gtk.Button(label="▸" if self.config.get("compact", False) else "▾")
        self.compact_btn.get_style_context().add_class("compact-btn")
        self.compact_btn.set_focus_on_click(False)
        self.compact_btn.connect("clicked", lambda _b: self._set_compact(not self.config.get("compact", False)))
        header.pack_start(self.title_lbl, True, True, 0)
        header.pack_end(self.compact_btn, False, False, 0)
        header.pack_end(self.uptime_lbl, False, False, 0)
        self.card.pack_start(header, False, False, 0)

        self.body_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.card.pack_start(self.body_box, False, False, 0)

        self.cpu_val, self.cpu_bar = self._add_metric(self.body_box, "CPU")
        self.ram_val, self.ram_bar = self._add_metric(self.body_box, "Memoria RAM")
        self.swap_val, self.swap_bar = self._add_metric(self.body_box, "Swap")
        self.disk_val, self.disk_bar = self._add_metric(self.body_box, "Disco (/)")

        sep1 = Gtk.Box()
        sep1.get_style_context().add_class("sep")
        self.body_box.pack_start(sep1, False, False, 2)

        self.gpu_val = self._add_kv(self.body_box, "GPU", "—")
        self.net_val = self._add_kv(self.body_box, "Red", "—")
        self.bat_row, self.bat_val = self._add_kv_with_row(self.body_box, "Batería", "—")
        self.bat_row.set_no_show_all(True)

        sep2 = Gtk.Box()
        sep2.get_style_context().add_class("sep")
        self.body_box.pack_start(sep2, False, False, 2)

        top_hdr = Gtk.Label(label="Top Procesos (CPU / RAM)", xalign=0)
        top_hdr.get_style_context().add_class("section-label")
        self.body_box.pack_start(top_hdr, False, False, 0)

        self.proc_lbl = Gtk.Label(label="—", xalign=0)
        self.proc_lbl.get_style_context().add_class("mono-label")
        self.body_box.pack_start(self.proc_lbl, False, False, 0)

    def _add_metric(self, parent, label_text):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label=label_text, xalign=0)
        lbl.get_style_context().add_class("section-label")
        val = Gtk.Label(label="0%", xalign=1)
        val.get_style_context().add_class("value-label")
        row.pack_start(lbl, True, True, 0)
        row.pack_end(val, False, False, 0)
        parent.pack_start(row, False, False, 0)

        bar = Gtk.ProgressBar()
        parent.pack_start(bar, False, False, 0)
        return val, bar

    def _add_kv(self, parent, key_text, val_text):
        _, val = self._add_kv_with_row(parent, key_text, val_text)
        return val

    def _add_kv_with_row(self, parent, key_text, val_text):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label=key_text, xalign=0)
        lbl.get_style_context().add_class("section-label")
        val = Gtk.Label(label=val_text, xalign=1)
        val.get_style_context().add_class("subtitle")
        row.pack_start(lbl, True, True, 0)
        row.pack_end(val, False, False, 0)
        parent.pack_start(row, False, False, 0)
        return row, val

    def _apply_scale(self, scale_key):
        self.config["scale"] = scale_key
        self.css_provider.load_from_data(build_css(scale_key))
        self.card.set_size_request(SCALES[scale_key]["width"], -1)
        self._save_config()

    def _reset_timer(self):
        if self.timer_id is not None:
            GLib.source_remove(self.timer_id)
        self.timer_id = GLib.timeout_add_seconds(self.config["interval"], self._refresh)

    @staticmethod
    def _point_on_any_monitor(x, y):
        display = Gdk.Display.get_default()
        for i in range(display.get_n_monitors()):
            geo = display.get_monitor(i).get_geometry()
            if geo.x <= x < geo.x + geo.width and geo.y <= y < geo.y + geo.height:
                return True
        return False

    def _restore_position(self):
        if "x" in self.config and "y" in self.config:
            x, y = int(self.config["x"]), int(self.config["y"])
            if self._point_on_any_monitor(x + 20, y + 20):
                self.move(x, y)
                return
        display = Gdk.Display.get_default()
        mon = None
        for i in range(display.get_n_monitors()):
            candidate = display.get_monitor(i)
            if not candidate.is_primary():
                mon = candidate
                break
        if mon is None:
            mon = display.get_primary_monitor() or display.get_monitor(0)
        geo = mon.get_geometry()
        width = SCALES[self.config["scale"]]["width"]
        self.move(geo.x + geo.width - width - 28, geo.y + 40)

    def _debounced_save_config(self):
        self._save_timer_id = None
        self._save_config()
        return False

    def _on_configure(self, _widget, event):
        pos = (int(event.x), int(event.y))
        if pos == self._last_pos:
            return False
        self._last_pos = pos
        self.config["x"], self.config["y"] = pos
        if self._save_timer_id is not None:
            GLib.source_remove(self._save_timer_id)
        self._save_timer_id = GLib.timeout_add(500, self._debounced_save_config)
        return False

    def _on_button_press(self, _widget, event):
        if event.button == 1 and not self.config.get("locked", False):
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        if event.button == 3:
            self._show_context_menu(event)
            return True
        return False

    def _show_context_menu(self, event):
        menu = Gtk.Menu()

        scale_group = None
        for label, key in [("Tamaño: Pequeño", "small"), ("Tamaño: Mediano", "medium"), ("Tamaño: Grande", "large")]:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(scale_group, label)
            scale_group = scale_group or item
            item.set_active(self.config["scale"] == key)
            item.connect("toggled", lambda w, k=key: w.get_active() and self._apply_scale(k))
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        interval_group = None
        for sec in (1, 2, 5):
            item = Gtk.RadioMenuItem.new_with_label_from_widget(interval_group, f"Actualizar cada {sec}s")
            interval_group = interval_group or item
            item.set_active(self.config["interval"] == sec)
            item.connect("toggled", lambda w, s=sec: w.get_active() and self._set_interval(s))
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        lock_item = Gtk.CheckMenuItem(label="Bloquear posición")
        lock_item.set_active(self.config.get("locked", False))
        lock_item.connect("toggled", self._toggle_lock)
        menu.append(lock_item)

        rec_item = Gtk.CheckMenuItem(label="Grabar historial (CSV)")
        rec_item.set_active(self.config.get("recording", False))
        rec_item.connect("toggled", self._toggle_recording)
        menu.append(rec_item)

        analyzer_item = Gtk.MenuItem(label="Abrir analizador / gráficos")
        analyzer_item.connect("activate", self._open_analyzer)
        menu.append(analyzer_item)

        folder_item = Gtk.MenuItem(label="Abrir carpeta ~/dsmon")
        folder_item.connect("activate", self._open_folder)
        menu.append(folder_item)

        menu.append(Gtk.SeparatorMenuItem())

        view_group = None
        for label, is_compact in [("Vista: Expandida", False), ("Vista: Compacta", True)]:
            c_item = Gtk.RadioMenuItem.new_with_label_from_widget(view_group, label)
            view_group = view_group or c_item
            c_item.set_active(self.config.get("compact", False) == is_compact)
            c_item.connect("toggled", lambda w, c=is_compact: w.get_active() and self._set_compact(c))
            menu.append(c_item)

        min_item = Gtk.MenuItem(label="Minimizar")
        min_item.connect("activate", lambda _w: self.iconify())
        menu.append(min_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Cerrar widget")
        quit_item.connect("activate", lambda _w: self._quit())
        menu.append(quit_item)

        menu.show_all()
        menu.popup_at_pointer(event)

    def _title_text(self):
        return "Recursos ● REC" if self.config.get("recording", False) else "Recursos del Sistema"

    def _set_interval(self, sec):
        self.config["interval"] = sec
        self._save_config()
        self._reset_timer()

    def _resize_to_preferred(self):
        width = SCALES[self.config["scale"]]["width"]
        _, nat_h = self.get_preferred_height()
        self.resize(width, max(1, nat_h))
        return False

    def _set_compact(self, compact):
        self.config["compact"] = bool(compact)
        self.compact_btn.set_label("▸" if self.config["compact"] else "▾")
        if self.config["compact"]:
            self.body_box.hide()
        else:
            self.body_box.show_all()
            self._refresh()
        self._save_config()
        GLib.idle_add(self._resize_to_preferred)

    def _toggle_lock(self, widget):
        self.config["locked"] = widget.get_active()
        self._save_config()

    def _toggle_recording(self, widget):
        active = widget.get_active()
        self.config["recording"] = active
        self.title_lbl.set_text(self._title_text())
        if not active:
            self.flush_csv()
        self._save_config()

    @staticmethod
    def _spawn_detached(cmd):
        try:
            proc = subprocess.Popen(cmd, start_new_session=True)
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, proc.pid, lambda _pid, _status: None)
        except OSError as exc:
            logging.warning("Error al lanzar %s: %s", cmd[0], exc)

    def _open_analyzer(self, _widget):
        self.flush_csv()
        analyzer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dsmon_analyzer.py")
        if os.path.exists(analyzer_path):
            self._spawn_detached([sys.executable, analyzer_path])

    def _open_folder(self, _widget):
        base_dir = os.path.dirname(self.recorder.log_dir)
        xdg_open = shutil.which("xdg-open", path=TRUSTED_PATH)
        if not xdg_open:
            return
        try:
            os.makedirs(self.recorder.log_dir, exist_ok=True)
            self._spawn_detached([xdg_open, base_dir])
        except OSError as exc:
            logging.warning("No se pudo abrir carpeta %s: %s", base_dir, exc)

    def _quit(self):
        if self._save_timer_id is not None:
            GLib.source_remove(self._save_timer_id)
            self._save_timer_id = None
            self._save_config()
        self.flush_csv()
        Gtk.main_quit()

    def flush_csv(self):
        self.recorder.flush()

    @staticmethod
    def _set_bar(bar, frac):
        frac = max(0.0, min(1.0, frac))
        bar.set_fraction(frac)
        ctx = bar.get_style_context()
        ctx.remove_class("warn")
        ctx.remove_class("crit")
        if frac >= 0.85:
            ctx.add_class("crit")
        elif frac >= 0.65:
            ctx.add_class("warn")

    def _refresh(self):
        try:
            snap = self.stats.collect_snapshot()
            cpu = snap["cpu_pct"]
            temp = snap["temp_c"]
            temp_txt = f" · {temp:.0f}°C" if temp is not None else ""
            self.cpu_val.set_text(f"{cpu:.0f}%{temp_txt}")
            self._set_bar(self.cpu_bar, cpu / 100.0)

            used_m, total_m = snap["ram_used_bytes"], snap["ram_total_bytes"]
            ram_frac = (used_m / total_m) if total_m else 0.0
            self.ram_val.set_text(f"{fmt_bytes(used_m)} / {fmt_bytes(total_m)} ({ram_frac*100:.0f}%)")
            self._set_bar(self.ram_bar, ram_frac)

            used_s, total_s = snap["swap_used_bytes"], snap["swap_total_bytes"]
            swap_frac = (used_s / total_s) if total_s else 0.0
            self.swap_val.set_text(f"{fmt_bytes(used_s)} / {fmt_bytes(total_s)}")
            self._set_bar(self.swap_bar, swap_frac)

            used_d, total_d = snap["disk_used_bytes"], snap["disk_total_bytes"]
            disk_frac = (used_d / total_d) if total_d else 0.0
            self.disk_val.set_text(f"{fmt_bytes(used_d)} / {fmt_bytes(total_d)} ({disk_frac*100:.0f}%)")
            self._set_bar(self.disk_bar, disk_frac)

            self.gpu_val.set_text(snap["gpu_label"])
            self.net_val.set_text(f"↓ {fmt_rate(snap['rx_bps'])}  ↑ {fmt_rate(snap['tx_bps'])}")

            bat = snap["battery"]
            if bat is None:
                self.bat_row.hide()
            else:
                self.bat_row.show_all()
                self.bat_val.set_text(bat)

            if self.config.get("compact", False):
                self.uptime_lbl.set_text(f"CPU {cpu:.0f}% · RAM {ram_frac*100:.0f}%")
            else:
                self.uptime_lbl.set_text(snap["uptime_text"])
            self.proc_lbl.set_text(snap["proc_text"])

            if self.config.get("recording", False):
                self.recorder.record(snap)
        except (OSError, ValueError, IndexError) as exc:
            logging.warning("Error leyendo métricas del sistema: %s", exc)
        return True


def run_gui():
    if Gtk is None:
        raise RuntimeError("GTK3 (python3-gi) no está disponible. Usa --headless, --snapshot o --report.")
    win = DesktopResourceWidget()
    win.connect("destroy", lambda _w: win._quit())
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: win._quit() or False)
    win.show_all()
    Gtk.main()


def start_metrics_server(bind_addr, port, state_holder):
    """Start a lightweight Prometheus /metrics HTTP server in a daemon thread."""
    lock = state_holder["lock"]

    class _MetricsHandler(BaseHTTPRequestHandler):
        timeout = 5

        def do_GET(self):
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            with lock:
                snap = state_holder.get("snap")
            if snap is None:
                self.send_response(503)
                self.end_headers()
                return
            body = format_prometheus_metrics(snap).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer((bind_addr, int(port)), _MetricsHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_headless(
    interval=5.0,
    log_dir=LOG_DIR,
    mount_path="/",
    flush_every=15,
    retention_days=30,
    compress_after_days=1,
    net_iface="auto",
    metrics_port=0,
    metrics_bind="127.0.0.1",
):
    interval = max(0.5, float(interval))
    stats = SystemStats()
    recorder = CsvRecorder(
        log_dir=log_dir,
        flush_every=flush_every,
        retention_days=retention_days,
        compress_after_days=compress_after_days,
    )
    state_holder = {"lock": threading.Lock(), "snap": None}
    metrics_srv = None
    if int(metrics_port) > 0:
        metrics_srv = start_metrics_server(metrics_bind, int(metrics_port), state_holder)
    running = True

    def _stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Warm-up delay so first delta sample (CPU/net/disk/procs) is meaningful
    time.sleep(min(0.5, interval))
    while running:
        t0 = time.monotonic()
        try:
            snap = stats.collect_snapshot(mount_path=mount_path, net_iface=net_iface)
            with state_holder["lock"]:
                state_holder["snap"] = snap
            recorder.record(snap)
        except (OSError, ValueError, IndexError) as exc:
            logging.warning("Error leyendo métricas del sistema: %s", exc)
        elapsed = time.monotonic() - t0
        sleep_s = max(0.1, interval - elapsed)
        deadline = time.monotonic() + sleep_s
        while running and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
    if metrics_srv is not None:
        metrics_srv.shutdown()
        metrics_srv.server_close()
    recorder.flush()


def run_snapshot(mount_path="/", as_json=False, net_iface="auto"):
    stats = SystemStats()
    stats.top_processes_detail()
    time.sleep(0.5)
    snap = stats.collect_snapshot(mount_path=mount_path, net_iface=net_iface)
    if as_json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return
    print(f"Timestamp : {snap['timestamp']}")
    print(
        f"CPU       : {snap['cpu_pct']:.1f}% "
        f"(user {snap['cpu_user_pct']:.1f}%, sys {snap['cpu_sys_pct']:.1f}%, "
        f"iowait {snap['cpu_iowait_pct']:.1f}%, steal {snap['cpu_steal_pct']:.1f}%)"
    )
    print(f"RAM       : {snap['ram_used_gb']:.2f} / {snap['ram_total_gb']:.2f} GB ({snap['ram_pct']:.1f}%)")
    print(f"Disk ({mount_path}) : {snap['disk_pct']:.1f}% | I/O R {snap['disk_read_kbps']:.1f} KB/s · W {snap['disk_write_kbps']:.1f} KB/s · {snap['disk_iops']:.1f} IOPS")
    print(f"Net       : ↓ {snap['rx_kbps']:.1f} KB/s · ↑ {snap['tx_kbps']:.1f} KB/s")
    print(f"GPU       : {snap['gpu_label']}")
    print(f"Uptime    : {snap['uptime_text']}")


def run_report(log_dir=LOG_DIR, report_dir=REPORT_DIR, max_days=7):
    import dsmon_analyzer

    all_data = dsmon_analyzer.load_all_logs(log_dir=log_dir, max_days=max_days)
    if not all_data:
        print(f"No se encontraron archivos CSV en {log_dir}", file=sys.stderr)
        return 1
    out = dsmon_analyzer.build_report(all_data, sorted(all_data.keys())[-1], report_dir=report_dir)
    print(out)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="DSMon: Linux resource monitor (GUI + headless nmon-style recorder)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true", help="Ejecuta el grabador CSV en segundo plano sin GUI")
    mode.add_argument("--snapshot", action="store_true", help="Muestra una lectura única del sistema y termina")
    mode.add_argument("--report", action="store_true", help="Genera el reporte HTML desde los logs CSV")
    parser.add_argument("--config", default=None, help="Ruta a dsmon.conf (default: ~/.config/dsmon/dsmon.conf o /etc/dsmon/dsmon.conf)")
    parser.add_argument("--interval", type=float, default=None, help="Intervalo en segundos para --headless (default: 5.0)")
    parser.add_argument("--log-dir", default=None, help=f"Directorio de CSVs (default: {LOG_DIR})")
    parser.add_argument("--report-dir", default=None, help=f"Directorio de reportes HTML (default: {REPORT_DIR})")
    parser.add_argument("--mount", default=None, help="Punto de montaje para medir uso de disco (default: /)")
    parser.add_argument("--iface", default=None, help="Interfaz de red a monitorear (default: auto)")
    parser.add_argument("--flush-every", type=int, default=None, help="Muestras por escritura a disco (default: 15)")
    parser.add_argument("--retention-days", type=int, default=None, help="Días a conservar logs (0=ilimitado, default: 30)")
    parser.add_argument("--compress-after-days", type=int, default=None, help="Comprimir CSVs tras N días (0=desactivar, default: 1)")
    parser.add_argument("--metrics-port", type=int, default=None, help="Puerto HTTP opcional para exponer /metrics en formato Prometheus (0=desactivado)")
    parser.add_argument("--metrics-bind", default=None, help="Dirección bind para --metrics-port (default: 127.0.0.1)")
    parser.add_argument("--days", type=int, default=7, help="Últimos N días a incluir en --report (0=todos, default: 7)")
    parser.add_argument("--json", action="store_true", help="Salida JSON (para --snapshot)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = load_settings(args.config)
    interval = args.interval if args.interval is not None else cfg["interval"]
    log_dir = os.path.expanduser(args.log_dir) if args.log_dir else cfg["log_dir"]
    report_dir = os.path.expanduser(args.report_dir) if args.report_dir else cfg["report_dir"]
    mount_path = args.mount if args.mount else cfg["mount"]
    net_iface = args.iface if args.iface else cfg["net_iface"]
    flush_every = args.flush_every if args.flush_every is not None else cfg["flush_every"]
    retention_days = args.retention_days if args.retention_days is not None else cfg["retention_days"]
    compress_after_days = (
        args.compress_after_days if args.compress_after_days is not None else cfg["compress_after_days"]
    )
    metrics_port = args.metrics_port if args.metrics_port is not None else cfg["metrics_port"]
    metrics_bind = args.metrics_bind if args.metrics_bind else cfg["metrics_bind"]

    if args.headless:
        run_headless(
            interval=interval,
            log_dir=log_dir,
            mount_path=mount_path,
            flush_every=flush_every,
            retention_days=retention_days,
            compress_after_days=compress_after_days,
            net_iface=net_iface,
            metrics_port=metrics_port,
            metrics_bind=metrics_bind,
        )
        return 0
    if args.snapshot:
        run_snapshot(mount_path=mount_path, as_json=args.json, net_iface=net_iface)
        return 0
    if args.report:
        return run_report(log_dir=log_dir, report_dir=report_dir, max_days=args.days)
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
