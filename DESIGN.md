# DSMon Analyzer — DESIGN.md

## Identidad Visual
- **Concepto:** Consola de telemetría Linux de alta densidad (*nmon-inspired*, sobria, sin *AI slop*).
- **Paleta:**
  - Fondo base: `#090c10`
  - Tarjetas / paneles: `#11161d`
  - Controles elevados: `#161c26`
  - Bordes: `rgba(148, 163, 184, 0.14)`
  - Texto principal: `#e6edf3` | Secundario: `#8b949e`
  - Acentos semánticos:
    - Azul `#38bdf8` (CPU total / RX)
    - Verde `#34d399` (RAM / User / Disk Read)
    - Ámbar `#fbbf24` (Temp / IOWait / Warn)
    - Rojo `#f87171` (Sys / TX / Disk Write / Error)
    - Violeta `#a78bfa` (Swap / Steal / GPU)
- **Tipografía:**
  - UI: `"IBM Plex Sans", system-ui, -apple-system, sans-serif`
  - Datos / Métricas: `"JetBrains Mono", ui-monospace, SFMono-Regular, monospace` con `font-variant-numeric: tabular-nums`.
  - Mínimos: `>= 12px` en etiquetas/leyendas/badges, `>= 13px–14px` en cuerpo y tablas, `>= 18px–22px` en KPIs y títulos.

## Arquitectura de Información
1. **Header:** Título con fecha seleccionada, conteo de muestras/rango y barra de filtros (Fecha, presets `Todo/5m/15m/1h`, rango manual).
2. **KPI Strip:** 6–8 tarjetas resumen con métricas clave del rango filtrado.
3. **Navegación por pestañas:** `Todo` | `CPU & Kernel` | `Memoria & Swap` | `Disco & Red` | `GPU` | `Procesos & Logs`.
4. **Gráficos Canvas HiDPI:** Tooltip sincronizado, exportación PNG por gráfico y ocultamiento automático de la pestaña/gráficos GPU cuando no hay muestras GPU.
