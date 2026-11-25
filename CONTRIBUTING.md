# Contribuir a DSMon

¡Las contribuciones, reportes de bugs y Pull Requests (PRs) son bienvenidos!

## Principios del proyecto

1. **Cero dependencias externas en el núcleo:** `dsmon_core.py` y `dsmon_analyzer.py` deben usar únicamente la librería estándar de Python 3 (el widget GTK3 en `dsmon.py` usa `python3-gi` del sistema solo en modo GUI).
2. **Seguro por defecto:** cualquier cambio que toque archivos, CSV o subprocesos debe mantener escritura atómica (`NamedTemporaryFile` + `os.replace`), sanitización de fórmulas CSV, resolución de binarios en `TRUSTED_PATH` y timeouts explícitos.
3. **Compatible con hardware real:** probar degradación segura cuando falten sensores (VMs sin temperatura, sistemas sin GPU, montajes ausentes).

## Cómo probar localmente

```bash
# 1. Compilación rápida
python3 -m py_compile dsmon_core.py dsmon.py dsmon_analyzer.py

# 2. Suite completa de tests unitarios
python3 -m unittest discover -s tests -v

# 3. Validación de sintaxis en scripts de instalación
bash -n install.sh install-server.sh uninstall.sh
```

## Flujo para enviar un PR

1. Haz un *fork* del repositorio y crea una rama (`git checkout -b fix/mi-mejora`).
2. Agrega tests unitarios en `tests/test_dsmon.py` que cubran tu cambio.
3. Verifica que los 3 comandos anteriores pasen sin errores.
4. Abre el Pull Request describiendo qué hardware o distro probaste.
