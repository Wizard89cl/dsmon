#!/usr/bin/env python3
"""DSMon Analyzer: Generates a self-contained interactive HTML report from DSMon CSV logs."""

import csv
import glob
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
import zlib
from datetime import datetime
from typing import Any, Optional


try:
    from dsmon_core import load_settings as _load_settings
    _cfg = _load_settings()
    LOG_DIR = _cfg["log_dir"]
    REPORT_DIR = _cfg["report_dir"]
except ImportError:
    LOG_DIR = os.path.expanduser("~/dsmon/logs")
    REPORT_DIR = os.path.expanduser("~/dsmon/reports")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DSMon Analyzer</title>
<style>
  :root {
    --bg: #090c10;
    --card: #11161d;
    --elevated: #161c26;
    --border: rgba(148, 163, 184, 0.14);
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #38bdf8;
    --font-ui: "IBM Plex Sans", system-ui, -apple-system, sans-serif;
    --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px 28px 48px;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 14px;
    line-height: 1.45;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 14px;
    margin-bottom: 18px;
  }
  h1 { margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; margin-top: 4px; font-family: var(--font-mono); }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 8px 12px;
  }
  .controls label { font-size: 12px; color: var(--muted); }
  select, input[type="time"] {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 5px 8px;
    font-size: 12px;
    font-family: var(--font-mono);
  }
  .range-btn, .tab-btn {
    background: var(--elevated);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 11px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .range-btn.active, .tab-btn.active {
    background: rgba(56, 189, 248, 0.18);
    border-color: rgba(56, 189, 248, 0.55);
    color: #7dd3fc;
  }
  .tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 18px;
  }
  .kpis {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
    gap: 12px;
    margin-bottom: 18px;
  }
  .kpi {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
  }
  .kpi .lbl { color: var(--muted); font-size: 12px; font-weight: 600; }
  .kpi .val {
    font-size: 19px;
    font-weight: 700;
    margin-top: 6px;
    color: #f8fafc;
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }
  .charts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(500px, 100%), 1fr));
    gap: 16px;
  }
  .panel {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    position: relative;
  }
  .panel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }
  .panel-title { font-size: 14px; font-weight: 600; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--muted); margin-top: 3px; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }
  button.btn {
    background: rgba(56, 189, 248, 0.14);
    color: #7dd3fc;
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 8px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
  }
  button.btn:hover { background: rgba(56, 189, 248, 0.25); }
  canvas { width: 100%; height: 220px; display: block; cursor: crosshair; }
  .tooltip {
    position: fixed;
    pointer-events: none;
    background: rgba(9, 12, 16, 0.96);
    border: 1px solid rgba(148, 163, 184, 0.28);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    display: none;
    z-index: 50;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  }
  .procs-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(360px, 100%), 1fr));
    gap: 16px;
    margin-top: 16px;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 12px; font-weight: 600;
  }
  .b-err { background: rgba(248, 113, 113, 0.18); color: #fca5a5; }
  .b-warn { background: rgba(251, 191, 36, 0.18); color: #fcd34d; }
  .b-cnt { background: rgba(255, 255, 255, 0.08); color: #d1d5db; margin-left: 6px; }
  input[type="search"] {
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 10px; font-size: 12px; width: 220px;
  }
  .hidden { display: none !important; }
  @media print {
    @page { margin: 12mm; size: landscape; }
    body { background: #ffffff; color: #111827; padding: 0; }
    .controls, .tabs, button.btn, #log-search, .range-btn[data-sev], .tooltip { display: none !important; }
    .kpi, .panel {
      background: #ffffff;
      border: 1px solid #d1d5db;
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .kpi .lbl, .sub, .legend, th { color: #4b5563 !important; }
    .kpi .val, h1, .panel-title, td { color: #111827 !important; }
    .charts { grid-template-columns: repeat(2, 1fr); gap: 12px; }
    canvas { height: 185px; }
  }
</style>
</head>
<body>
<header>
  <div>
    <h1 id="page-title">DSMon Analyzer</h1>
    <div class="sub" id="subtitle"></div>
  </div>
  <div class="controls">
    <label for="date-select">Fecha:</label>
    <select id="date-select"></select>
    <button class="range-btn active" data-mins="0">Todo</button>
    <button class="range-btn" data-mins="5">5m</button>
    <button class="range-btn" data-mins="15">15m</button>
    <button class="range-btn" data-mins="60">1h</button>
    <label for="t-from">Desde</label><input type="time" id="t-from">
    <label for="t-to">Hasta</label><input type="time" id="t-to">
    <button class="btn" id="apply-custom">Filtrar</button>
    <input type="file" id="import-csv" accept=".csv,.gz,.csv.gz" multiple hidden>
    <button class="btn" id="import-btn" title="Cargar CSV o .csv.gz de cualquier servidor">Importar CSV</button>
    <button class="btn hidden" id="back-local" title="Volver a los logs locales">Volver a local</button>
    <button class="btn" id="print-pdf" title="Imprimir o guardar como PDF">Exportar PDF</button>
  </div>
</header>

<section class="kpis" id="kpis"></section>

<nav class="tabs" id="tabs" aria-label="Secciones">
  <button class="tab-btn active" data-tab="all">Todo</button>
  <button class="tab-btn" data-tab="cpu">CPU &amp; Kernel</button>
  <button class="tab-btn" data-tab="mem">Memoria &amp; Swap</button>
  <button class="tab-btn" data-tab="io">Disco I/O &amp; Red</button>
  <button class="tab-btn" data-tab="gpu" id="tab-gpu">GPU</button>
  <button class="tab-btn" data-tab="logs">Procesos &amp; Logs</button>
</nav>

<section class="charts" id="charts"></section>

<section class="procs-grid" id="procs-section">
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Procesos frecuentes en Top CPU</div>
    </div>
    <table>
      <thead><tr><th>Proceso</th><th>Apariciones Top #1</th></tr></thead>
      <tbody id="top-procs"></tbody>
    </table>
  </div>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Procesos frecuentes en Top RAM</div>
    </div>
    <table>
      <thead><tr><th>Proceso</th><th>Apariciones Top #1</th></tr></thead>
      <tbody id="top-ram-procs"></tbody>
    </table>
  </div>
</section>

<section class="panel" id="logs-section" style="margin-top: 16px;">
  <div class="panel-head" style="flex-wrap: wrap; gap: 10px;">
    <div>
      <div class="panel-title">Diagnóstico del Sistema (journalctl)</div>
      <div class="sub" id="log-summary"></div>
    </div>
    <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
      <button class="range-btn active" data-sev="all">Todos</button>
      <button class="range-btn" data-sev="err">Solo Errores</button>
      <button class="range-btn" data-sev="warn">Solo Warnings</button>
      <input type="search" id="log-search" placeholder="Filtrar (ej. nvidia, oom, usb)...">
    </div>
  </div>
  <table>
    <thead><tr><th>Última hora</th><th>Nivel</th><th>Servicio</th><th>Mensaje (agrupado)</th></tr></thead>
    <tbody id="sys-logs"></tbody>
  </table>
</section>

<div class="tooltip" id="tooltip"></div>

<script>
const ALL_DATA = __ALL_DATA_JSON__;
const ALL_LOGS = __ALL_LOGS_JSON__;
const DATES = Object.keys(ALL_DATA).sort();
const LOCAL_DATES = [...DATES];
let currentDate = __DEFAULT_DATE_JSON__;
let currentData = [];
let currentTab = "all";
let logSevFilter = "all";

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, ch => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"}[ch]
  ));
}

const chartDefs = [
  { id: "c-cpu", cat: "cpu", title: "CPU Total (%)", yMax: 100, unit: "%",
    series: [{ key: "cpu", label: "CPU %", color: "#38bdf8" }] },
  { id: "c-cpu-split", cat: "cpu", title: "CPU Desglosado (nmon)", yMax: 100, unit: "%",
    series: [
      { key: "cpu_user", label: "User", color: "#34d399" },
      { key: "cpu_sys", label: "Sys", color: "#f87171" },
      { key: "cpu_iowait", label: "IOWait", color: "#fbbf24" },
      { key: "cpu_steal", label: "Steal", color: "#a78bfa" },
    ] },
  { id: "c-temp", cat: "cpu", title: "Temperatura CPU (°C)", yMax: 105, unit: "°",
    series: [
      { key: "temp", label: "CPU 0 °C", color: "#fbbf24" },
      { key: "temp1", label: "CPU 1 °C", color: "#38bdf8" },
    ] },
  { id: "c-kernel", cat: "cpu", title: "Kernel: Context Switches (k/s) y Load", yMax: null, unit: "",
    series: [
      { key: "ctxt_k", label: "ctxt (k/s)", color: "#38bdf8" },
      { key: "intr_k", label: "intr (k/s)", color: "#a78bfa" },
    ] },
  { id: "c-ram", cat: "mem", title: "Memoria RAM y Swap (%)", yMax: 100, unit: "%",
    series: [
      { key: "ram", label: "RAM %", color: "#34d399" },
      { key: "swap", label: "Swap %", color: "#a78bfa" },
    ] },
  { id: "c-vmstat", cat: "mem", title: "Paginación Swap (páginas/s)", yMax: null, unit: "",
    series: [
      { key: "pswpin", label: "pswpin/s", color: "#38bdf8" },
      { key: "pswpout", label: "pswpout/s", color: "#f87171" },
    ] },
  { id: "c-disk-io", cat: "io", title: "Disco I/O (KB/s)", yMax: null, unit: "",
    series: [
      { key: "disk_r", label: "Lectura", color: "#34d399" },
      { key: "disk_w", label: "Escritura", color: "#f87171" },
    ] },
  { id: "c-disk-iops", cat: "io", title: "Disco IOPS y Uso (%)", yMax: null, unit: "",
    series: [
      { key: "disk_iops", label: "IOPS", color: "#fbbf24" },
      { key: "disk_pct", label: "Uso %", color: "#38bdf8" },
    ] },
  { id: "c-net", cat: "io", title: "Red (KB/s)", yMax: null, unit: "",
    series: [
      { key: "rx", label: "Descarga", color: "#38bdf8" },
      { key: "tx", label: "Subida", color: "#f87171" },
    ] },
  { id: "c-gpu", cat: "gpu", title: "GPU Utilización (%) y Temp (°C)", yMax: 100, unit: "",
    series: [
      { key: "gpu_util", label: "GPU 0 %", color: "#a78bfa" },
      { key: "gpu_temp", label: "GPU 0 °C", color: "#fbbf24" },
      { key: "gpu1_util", label: "GPU 1 %", color: "#34d399" },
      { key: "gpu1_temp", label: "GPU 1 °C", color: "#f87171" },
    ] },
  { id: "c-vram", cat: "gpu", title: "GPU VRAM Usada (MiB)", yMax: null, unit: " MiB",
    series: [
      { key: "gpu_vram", label: "GPU 0 VRAM", color: "#38bdf8" },
      { key: "gpu1_vram", label: "GPU 1 VRAM", color: "#34d399" },
    ] },
];

function maxVal(arr) { return arr.length ? Math.max(...arr) : 0; }
function avgVal(arr) { return arr.length ? arr.reduce((a,b)=>a+b,0)/arr.length : 0; }
function hasGpuData() { return currentData.some(d => d.gpu_util !== null && d.gpu_util !== undefined); }
function toSec(hms) {
  const p = hms.split(":").map(Number);
  return (p[0]||0)*3600 + (p[1]||0)*60 + (p[2]||0);
}

function renderKPIs() {
  const cpu = currentData.map(d => d.cpu);
  const iow = currentData.map(d => d.cpu_iowait || 0);
  const ram = currentData.map(d => d.ram);
  const temp = currentData.map(d => d.temp).filter(v => v > 0);
  const temp1 = currentData.map(d => d.temp1).filter(v => v !== null && v !== undefined && v > 0);
  const load = currentData.map(d => d.load);
  const rx = currentData.map(d => d.rx);
  const diskW = currentData.map(d => d.disk_w || 0);
  const diskR = currentData.map(d => d.disk_r || 0);
  const gpuU = currentData.map(d => d.gpu_util).filter(v => v !== null && v !== undefined);
  let tempTxt = temp.length ? `${maxVal(temp).toFixed(0)}°C` : "N/D";
  if (temp.length && temp1.length) {
    tempTxt = `${maxVal(temp).toFixed(0)}° / ${maxVal(temp1).toFixed(0)}°C`;
  }
  const items = [
    ["Muestras", currentData.length],
    ["CPU Prom / Máx", `${avgVal(cpu).toFixed(1)}% / ${maxVal(cpu).toFixed(1)}%`],
    ["IOWait Máx", `${maxVal(iow).toFixed(1)}%`],
    ["Temp Máx", tempTxt],
    ["RAM Prom / Máx", `${avgVal(ram).toFixed(1)}% / ${maxVal(ram).toFixed(1)}%`],
    ["Disco R / W Máx", `${maxVal(diskR).toFixed(0)} / ${maxVal(diskW).toFixed(0)} KB/s`],
    ["Pico Descarga", `${maxVal(rx).toFixed(0)} KB/s`],
    ["Load Máx (1m)", maxVal(load).toFixed(2)],
  ];
  if (gpuU.length) {
    items.push(["GPU Prom / Máx", `${avgVal(gpuU).toFixed(0)}% / ${maxVal(gpuU).toFixed(0)}%`]);
  }
  document.getElementById("kpis").innerHTML = items.map(([k,v]) =>
    `<div class="kpi"><div class="lbl">${k}</div><div class="val">${v}</div></div>`
  ).join("");
}

function drawChart(canvas, seriesList, yMaxFixed, unit) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const w = rect.width, h = rect.height;
  const pad = { l: 50, r: 14, t: 12, b: 24 };
  const pw = w - pad.l - pad.r;
  const ph = h - pad.t - pad.b;

  let yMax = yMaxFixed;
  if (!yMax) {
    yMax = 10;
    for (const s of seriesList) {
      const vals = currentData.map(d => d[s.key]).filter(v => v !== null && v !== undefined);
      const m = maxVal(vals);
      if (m > yMax) yMax = m;
    }
    yMax = Math.ceil(yMax * 1.15);
  }

  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "rgba(148,163,184,0.12)";
  ctx.fillStyle = "#8b949e";
  ctx.font = "12px JetBrains Mono, monospace";

  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (ph * i / 4);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
    const val = yMax * (1 - i / 4);
    ctx.fillText(`${val.toFixed(0)}${unit}`, 4, y + 4);
  }

  if (currentData.length > 1) {
    const stepIdx = [0, Math.floor(currentData.length / 2), currentData.length - 1];
    for (const idx of stepIdx) {
      const x = pad.l + (pw * idx / (currentData.length - 1));
      ctx.fillText(currentData[idx].t, Math.min(x, w - 66), h - 6);
    }
  }

  for (const s of seriesList) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    currentData.forEach((d, idx) => {
      const val = d[s.key];
      if (val === null || val === undefined) {
        started = false;
        return;
      }
      const x = currentData.length > 1 ? pad.l + (pw * idx / (currentData.length - 1)) : pad.l;
      const y = pad.t + ph * (1 - Math.min(1, Math.max(0, val / yMax)));
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  canvas._meta = { pad, pw, ph, yMax, seriesList, unit };
}

function renderProcTable(targetId, field) {
  const counts = new Map();
  for (const d of currentData) {
    const name = d[field];
    if (name && name !== "—") counts.set(name, (counts.get(name) || 0) + 1);
  }
  const sorted = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 10);
  document.getElementById(targetId).innerHTML = sorted.map(([name, count]) =>
    `<tr><td>${escapeHtml(name)}</td><td>${count}</td></tr>`
  ).join("") || "<tr><td colspan='2' style='color:#8b949e'>Sin datos en el rango</td></tr>";
}

function renderSysLogs() {
  const logs = ALL_LOGS[currentDate] || [];
  const q = (document.getElementById("log-search").value || "").toLowerCase();
  const filtered = logs.filter(item => {
    if (logSevFilter !== "all" && item.sev !== logSevFilter) return false;
    if (q && !(`${item.unit} ${item.msg}`.toLowerCase().includes(q))) return false;
    return true;
  });
  const errCount = logs.filter(i => i.sev === "err").reduce((a,b) => a + b.cnt, 0);
  const warnCount = logs.filter(i => i.sev === "warn").reduce((a,b) => a + b.cnt, 0);
  document.getElementById("log-summary").textContent =
    `${errCount} errores · ${warnCount} warnings (${logs.length} grupos únicos)`;
  document.getElementById("sys-logs").innerHTML = filtered.map(item => {
    const badge = item.sev === "err"
      ? `<span class="badge b-err">ERROR</span>`
      : `<span class="badge b-warn">WARN</span>`;
    const cnt = item.cnt > 1 ? `<span class="badge b-cnt">×${item.cnt}</span>` : "";
    return `<tr>
      <td style="white-space:nowrap;color:#8b949e;font-family:var(--font-mono)">${escapeHtml(item.last)}</td>
      <td>${badge}</td>
      <td style="color:#7dd3fc;font-family:var(--font-mono)">${escapeHtml(item.unit)}</td>
      <td>${escapeHtml(item.msg)}${cnt}</td>
    </tr>`;
  }).join("") || "<tr><td colspan='4' style='color:#8b949e'>Sin errores ni warnings en este filtro</td></tr>";
}

function applyTabVisibility() {
  const gpuPresent = hasGpuData();
  document.getElementById("tab-gpu").classList.toggle("hidden", !gpuPresent);
  if (currentTab === "gpu" && !gpuPresent) {
    currentTab = "all";
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === "all"));
  }
  const showCharts = currentTab !== "logs";
  document.getElementById("charts").classList.toggle("hidden", !showCharts);
  document.getElementById("procs-section").classList.toggle("hidden", currentTab !== "all" && currentTab !== "logs");
  document.getElementById("logs-section").classList.toggle("hidden", currentTab !== "all" && currentTab !== "logs");

  for (const def of chartDefs) {
    const panel = document.getElementById(`panel-${def.id}`);
    if (!panel) continue;
    const hideGpu = def.cat === "gpu" && !gpuPresent;
    const hideTab = currentTab !== "all" && def.cat !== currentTab;
    panel.classList.toggle("hidden", hideGpu || hideTab);
  }
}

function refreshAll() {
  const host = (currentData.find(d => d.host) || {}).host || "";
  const label = host ? `${host} (${currentDate.replace(/^importado:\s*/, "")})` : currentDate;
  document.getElementById("page-title").textContent = `DSMon Analyzer — ${label}`;
  const backBtn = document.getElementById("back-local");
  if (backBtn) {
    backBtn.classList.toggle("hidden", !(LOCAL_DATES.length && currentDate.startsWith("importado:")));
  }
  const rangeTxt = currentData.length
    ? `${currentData[0].t} → ${currentData[currentData.length - 1].t}`
    : "Sin muestras";
  document.getElementById("subtitle").textContent =
    `${currentData.length} muestras · Rango: ${rangeTxt}`;
  renderKPIs();
  applyTabVisibility();
  for (const def of chartDefs) {
    const panel = document.getElementById(`panel-${def.id}`);
    if (panel && !panel.classList.contains("hidden")) {
      drawChart(document.getElementById(def.id), def.series, def.yMax, def.unit);
    }
  }
  renderProcTable("top-procs", "top");
  renderProcTable("top-ram-procs", "top_ram");
  renderSysLogs();
}

function applyPresetMinutes(mins) {
  const dayRows = ALL_DATA[currentDate] || [];
  if (!mins || !dayRows.length) {
    currentData = dayRows;
  } else {
    const lastSec = toSec(dayRows[dayRows.length - 1].t);
    const cutoff = lastSec - mins * 60;
    currentData = dayRows.filter(d => toSec(d.t) >= cutoff);
  }
  if (currentData.length) {
    document.getElementById("t-from").value = currentData[0].t.slice(0, 5);
    document.getElementById("t-to").value = currentData[currentData.length - 1].t.slice(0, 5);
  }
  refreshAll();
}

function mountCharts() {
  const container = document.getElementById("charts");
  const tooltip = document.getElementById("tooltip");

  for (const def of chartDefs) {
    const panel = document.createElement("div");
    panel.className = "panel";
    panel.id = `panel-${def.id}`;
    const legendHtml = def.series.map(s =>
      `<span><span class="dot" style="background:${s.color}"></span>${s.label}</span>`
    ).join("");
    panel.innerHTML = `
      <div class="panel-head">
        <div>
          <div class="panel-title">${def.title}</div>
          <div class="legend">${legendHtml}</div>
        </div>
        <button class="btn" data-save="${def.id}">Guardar PNG</button>
      </div>
      <canvas id="${def.id}"></canvas>
    `;
    container.appendChild(panel);
    const canvas = panel.querySelector("canvas");

    panel.querySelector("[data-save]").addEventListener("click", () => {
      const a = document.createElement("a");
      a.download = `dsmon-${def.id}-${currentDate}.png`;
      a.href = canvas.toDataURL("image/png");
      a.click();
    });

    canvas.addEventListener("mousemove", (e) => {
      if (!currentData.length || !canvas._meta) return;
      const rect = canvas.getBoundingClientRect();
      const rx = e.clientX - rect.left - canvas._meta.pad.l;
      const frac = Math.max(0, Math.min(1, rx / canvas._meta.pw));
      const idx = Math.round(frac * (currentData.length - 1));
      const d = currentData[idx];
      const rows = def.series.map(s => {
        const val = d[s.key];
        const txt = (val === null || val === undefined) ? "N/D" : `${val.toFixed(1)}${def.unit}`;
        return `<div style="color:${s.color}">${s.label}: <b>${txt}</b></div>`;
      }).join("");
      tooltip.innerHTML = `<div style="color:#8b949e;margin-bottom:3px">${escapeHtml(d.t)} (${escapeHtml(d.top)})</div>${rows}`;
      tooltip.style.left = `${e.clientX + 14}px`;
      tooltip.style.top = `${e.clientY + 14}px`;
      tooltip.style.display = "block";
    });
    canvas.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  }

  window.addEventListener("resize", () => {
    for (const def of chartDefs) {
      const panel = document.getElementById(`panel-${def.id}`);
      if (panel && !panel.classList.contains("hidden")) {
        drawChart(document.getElementById(def.id), def.series, def.yMax, def.unit);
      }
    }
  });
}

function splitCsvLine(line) {
  const out = [];
  let cur = "", inQ = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (inQ && line[i + 1] === '"') { cur += '"'; i++; }
      else inQ = !inQ;
    } else if (ch === ',' && !inQ) {
      out.push(cur); cur = "";
    } else {
      cur += ch;
    }
  }
  out.push(cur);
  return out;
}

function rowToSample(cols, idx) {
  const num = (k, def = 0) => {
    const v = cols[idx[k]];
    if (v === undefined || v === "") return def;
    const n = Number(v);
    return Number.isNaN(n) ? def : n;
  };
  const optNum = (k) => {
    const v = cols[idx[k]];
    return (v === undefined || v === "" || Number.isNaN(Number(v))) ? null : Number(v);
  };
  const ts = cols[idx["timestamp"]];
  const cpu = num("cpu_pct", NaN);
  if (!ts || Number.isNaN(cpu)) return null;
  return {
    t: ts.split(" ").pop(),
    cpu,
    cpu_user: num("cpu_user_pct", cpu),
    cpu_sys: num("cpu_sys_pct"),
    cpu_iowait: num("cpu_iowait_pct"),
    cpu_steal: num("cpu_steal_pct"),
    ctxt_k: num("ctxt_per_s") / 1000.0,
    intr_k: num("intr_per_s") / 1000.0,
    temp: num("temp_c"),
    temp1: optNum("temp_cpu1_c"),
    host: cols[idx["hostname"]] || "",
    ram: num("ram_pct"),
    swap: num("swap_pct"),
    pswpin: num("pswpin_per_s"),
    pswpout: num("pswpout_per_s"),
    disk_pct: num("disk_pct"),
    disk_r: num("disk_read_kbps"),
    disk_w: num("disk_write_kbps"),
    disk_iops: num("disk_iops"),
    rx: num("rx_kbps"),
    tx: num("tx_kbps"),
    load: num("load1"),
    top: cols[idx["top_proc"]] || "—",
    top_ram: cols[idx["top_ram_proc"]] || "—",
    gpu_util: optNum("gpu_util_pct"),
    gpu_temp: optNum("gpu_temp_c"),
    gpu_vram: optNum("gpu_vram_used_mb"),
    gpu1_util: optNum("gpu1_util_pct"),
    gpu1_temp: optNum("gpu1_temp_c"),
    gpu1_vram: optNum("gpu1_vram_used_mb"),
  };
}

function parseCsvText(text) {
  const lines = text.split(/\r?\n/).filter(l => l.trim().length > 0);
  if (lines.length < 2) return [];
  const headers = splitCsvLine(lines[0]);
  const idx = Object.fromEntries(headers.map((h, i) => [h.trim(), i]));
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    const sample = rowToSample(splitCsvLine(lines[i]), idx);
    if (sample) rows.push(sample);
  }
  return rows;
}

function populateDateSelect() {
  const sel = document.getElementById("date-select");
  const keys = Object.keys(ALL_DATA).sort();
  sel.innerHTML = keys.map(d => `<option value="${escapeHtml(d)}">${escapeHtml(d)}</option>`).join("");
  if (ALL_DATA[currentDate]) sel.value = currentDate;
}

function resetRangeButtons() {
  document.querySelectorAll(".range-btn[data-mins]").forEach(b => b.classList.remove("active"));
  document.querySelector('.range-btn[data-mins="0"]').classList.add("active");
}

function initControls() {
  const sel = document.getElementById("date-select");
  populateDateSelect();
  sel.addEventListener("change", () => {
    currentDate = sel.value;
    resetRangeButtons();
    applyPresetMinutes(0);
  });

  document.getElementById("print-pdf").addEventListener("click", () => {
    window.print();
  });

  document.getElementById("back-local").addEventListener("click", () => {
    if (!LOCAL_DATES.length) return;
    currentDate = LOCAL_DATES[LOCAL_DATES.length - 1];
    populateDateSelect();
    resetRangeButtons();
    applyPresetMinutes(0);
  });

  const MAX_IMPORT_BYTES = 25 * 1024 * 1024;
  const fileInput = document.getElementById("import-csv");
  document.getElementById("import-btn").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    let lastKey = null;
    const errors = [];
    for (const file of fileInput.files) {
      if (file.size > MAX_IMPORT_BYTES) {
        errors.push(`${file.name}: supera el límite de 25 MB`);
        continue;
      }
      try {
        let text;
        if (file.name.endsWith(".gz")) {
          if (typeof DecompressionStream === "undefined") {
            errors.push(`${file.name}: este navegador no soporta .csv.gz`);
            continue;
          }
          text = await new Response(file.stream().pipeThrough(new DecompressionStream("gzip"))).text();
        } else {
          text = await file.text();
        }
        const rows = parseCsvText(text);
        if (rows.length) {
          const key = `importado: ${file.name.replace(/\.csv(\.gz)?$/i, "")}`;
          ALL_DATA[key] = rows;
          lastKey = key;
        } else {
          errors.push(`${file.name}: sin filas válidas de DSMon`);
        }
      } catch (_err) {
        errors.push(`${file.name}: no se pudo leer`);
      }
    }
    if (lastKey) {
      currentDate = lastKey;
      populateDateSelect();
      resetRangeButtons();
      applyPresetMinutes(0);
    }
    if (errors.length) {
      alert("Aviso al importar:\n" + errors.join("\n"));
    }
    fileInput.value = "";
  });

  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentTab = btn.dataset.tab;
      refreshAll();
    });
  });

  document.querySelectorAll(".range-btn[data-mins]").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".range-btn[data-mins]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      applyPresetMinutes(Number(btn.dataset.mins));
    });
  });

  document.querySelectorAll(".range-btn[data-sev]").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".range-btn[data-sev]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      logSevFilter = btn.dataset.sev;
      renderSysLogs();
    });
  });

  document.getElementById("log-search").addEventListener("input", renderSysLogs);

  document.getElementById("apply-custom").addEventListener("click", () => {
    const f = document.getElementById("t-from").value;
    const t = document.getElementById("t-to").value;
    if (!f || !t) return;
    document.querySelectorAll(".range-btn").forEach(b => b.classList.remove("active"));
    const fSec = toSec(f + ":00"), tSec = toSec(t + ":59");
    const dayRows = ALL_DATA[currentDate] || [];
    currentData = dayRows.filter(d => {
      const s = toSec(d.t);
      return s >= fSec && s <= tSec;
    });
    refreshAll();
  });
}

mountCharts();
initControls();
applyPresetMinutes(0);
</script>
</body>
</html>
"""


def safe_json_for_html(obj: Any) -> str:
    """Serialize obj to JSON with <, >, & escaped so </script> cannot break inline JS."""
    raw = json.dumps(obj)
    return raw.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _float_or(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = row.get(key)
    if val is None or val == "":
        return default
    return float(val)


def _opt_float(row: dict[str, Any], key: str) -> Optional[float]:
    val = row.get(key)
    if val is None or val == "":
        return None
    return float(val)


def _downsample_rows(rows: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if max_points <= 0 or len(rows) <= max_points:
        return rows
    if max_points == 1:
        return [rows[-1]]
    step = (len(rows) - 1) / (max_points - 1)
    return [rows[round(i * step)] for i in range(max_points)]


def load_all_logs(
    max_days: int = 7,
    max_points_per_day: int = 3600,
    log_dir: Optional[str] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Load and downsample DSMon CSV and .csv.gz logs keyed by YYYY-MM-DD."""
    target_dir = log_dir if log_dir is not None else LOG_DIR
    csv_files = glob.glob(os.path.join(target_dir, "*.csv"))
    gz_files = glob.glob(os.path.join(target_dir, "*.csv.gz"))
    by_date = {}
    for path in sorted(gz_files) + sorted(csv_files):
        base = os.path.basename(path)
        date_str = base[:-7] if base.endswith(".csv.gz") else base[:-4]
        by_date[date_str] = path  # prefer uncompressed .csv if both exist

    dates = sorted(by_date)
    if max_days and max_days > 0:
        dates = dates[-max_days:]

    all_data = {}
    for date_str in dates:
        path = by_date[date_str]
        rows = []
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    try:
                        ts = r.get("timestamp")
                        if not ts:
                            continue
                        rows.append({
                            "t": ts.split(" ")[-1],
                            "cpu": float(r["cpu_pct"]),
                            "cpu_user": _float_or(r, "cpu_user_pct", float(r["cpu_pct"])),
                            "cpu_sys": _float_or(r, "cpu_sys_pct"),
                            "cpu_iowait": _float_or(r, "cpu_iowait_pct"),
                            "cpu_steal": _float_or(r, "cpu_steal_pct"),
                            "ctxt_k": _float_or(r, "ctxt_per_s") / 1000.0,
                            "intr_k": _float_or(r, "intr_per_s") / 1000.0,
                            "temp": _float_or(r, "temp_c"),
                            "temp1": _opt_float(r, "temp_cpu1_c"),
                            "host": r.get("hostname") or "",
                            "ram": float(r["ram_pct"]),
                            "swap": float(r["swap_pct"]),
                            "pswpin": _float_or(r, "pswpin_per_s"),
                            "pswpout": _float_or(r, "pswpout_per_s"),
                            "disk_pct": _float_or(r, "disk_pct"),
                            "disk_r": _float_or(r, "disk_read_kbps"),
                            "disk_w": _float_or(r, "disk_write_kbps"),
                            "disk_iops": _float_or(r, "disk_iops"),
                            "rx": float(r["rx_kbps"]),
                            "tx": float(r["tx_kbps"]),
                            "load": float(r["load1"]),
                            "top": r.get("top_proc") or "—",
                            "top_ram": r.get("top_ram_proc") or "—",
                            "gpu_util": _opt_float(r, "gpu_util_pct"),
                            "gpu_temp": _opt_float(r, "gpu_temp_c"),
                            "gpu_vram": _opt_float(r, "gpu_vram_used_mb"),
                            "gpu1_util": _opt_float(r, "gpu1_util_pct"),
                            "gpu1_temp": _opt_float(r, "gpu1_temp_c"),
                            "gpu1_vram": _opt_float(r, "gpu1_vram_used_mb"),
                        })
                    except (KeyError, ValueError, TypeError, AttributeError):
                        continue
        except (OSError, csv.Error, EOFError, zlib.error) as exc:
            logging.warning("Error leyendo log %s: %s", path, exc)
            continue
        if rows:
            all_data[date_str] = _downsample_rows(rows, max_points_per_day)
    return all_data


def load_journal_for_date(date_str):
    """Query systemd journald for warnings/errors on a given YYYY-MM-DD date, grouped cleanly."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        return []
    journalctl_bin = shutil.which("journalctl", path="/usr/bin:/bin:/usr/local/bin")
    if not journalctl_bin:
        return []
    try:
        out = subprocess.check_output(
            [
                journalctl_bin,
                "--since", f"{date_str} 00:00:00",
                "--until", f"{date_str} 23:59:59",
                "-p", "warning..emerg",
                "-n", "250",
                "--no-pager",
                "-o", "json",
            ],
            timeout=3.0,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    grouped = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            prio = int(ev.get("PRIORITY", 4))
            sev = "err" if prio <= 3 else "warn"
            unit = ev.get("_SYSTEMD_UNIT") or ev.get("SYSLOG_IDENTIFIER") or "kernel"
            msg = str(ev.get("MESSAGE", "")).strip()
            if not msg:
                continue
            ts_us = int(ev.get("__REALTIME_TIMESTAMP", 0))
            t_str = datetime.fromtimestamp(ts_us / 1_000_000).strftime("%H:%M:%S") if ts_us else "—"
            key = (sev, unit, msg)
            if key not in grouped:
                grouped[key] = {"sev": sev, "unit": unit, "msg": msg, "cnt": 1, "last": t_str}
            else:
                grouped[key]["cnt"] += 1
                grouped[key]["last"] = t_str
        except (json.JSONDecodeError, ValueError, TypeError, OverflowError):
            continue

    items = list(grouped.values())
    items.sort(key=lambda x: (0 if x["sev"] == "err" else 1, -x["cnt"], x["last"]))
    return items


def build_report(
    all_data: dict[str, list[dict[str, Any]]],
    default_date: str,
    report_dir: Optional[str] = None,
) -> str:
    """Render the self-contained HTML report atomically and return its path."""
    target_dir = report_dir if report_dir is not None else REPORT_DIR
    all_logs = {d: load_journal_for_date(d) for d in all_data}
    replacements = {
        "__ALL_DATA_JSON__": safe_json_for_html(all_data),
        "__ALL_LOGS_JSON__": safe_json_for_html(all_logs),
        "__DEFAULT_DATE_JSON__": safe_json_for_html(default_date),
    }
    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    html = pattern.sub(lambda m: replacements[m.group(0)], HTML_TEMPLATE)
    os.makedirs(target_dir, exist_ok=True)
    out_path = os.path.join(target_dir, "dsmon-report.html")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_dir, prefix=".dsmon-report.", suffix=".tmp", delete=False
        ) as f:
            tmp_path = f.name
            f.write(html)
        os.replace(tmp_path, out_path)
    except OSError:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    return out_path


def main():
    all_data = load_all_logs()
    if not all_data:
        print(f"No se encontraron logs CSV en {LOG_DIR}.")
        sys.exit(1)
    default_date = sorted(all_data.keys())[-1]
    if len(sys.argv) > 1:
        arg_date = os.path.splitext(os.path.basename(sys.argv[1]))[0]
        if arg_date in all_data:
            default_date = arg_date
    report_path = os.path.abspath(build_report(all_data, default_date))
    print(f"Reporte generado: {report_path}")
    webbrowser.open(f"file://{report_path}")


if __name__ == "__main__":
    main()
