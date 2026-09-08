#!/usr/bin/env bash
# Instala la aplicacion en el directorio del usuario.
#
#   cd ~/checklist && bash despliegue/instalar.sh
#
# No usa sudo salvo para las librerias del sistema, que pide aparte.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATOS="${CHECKLIST_BASE_DATOS:-$HOME/checklist-datos}"

echo "==> Aplicacion: $RAIZ"
echo "==> Datos:      $DATOS"
echo

# --------------------------------------------------------------------------- #
# Librerias del sistema que necesitan OpenCV y RapidOCR
# --------------------------------------------------------------------------- #
faltantes=()
for paquete in libgl1 libglib2.0-0; do
    dpkg -s "$paquete" >/dev/null 2>&1 || faltantes+=("$paquete")
done

if [ ${#faltantes[@]} -gt 0 ]; then
    echo "Faltan librerias del sistema. Ejecuta:"
    echo "    sudo apt update && sudo apt install -y ${faltantes[*]}"
    echo
    read -r -p "Ya las instalaste? [s/N] " respuesta
    [[ "$respuesta" =~ ^[sS]$ ]] || { echo "Instalalas y vuelve a correr esto."; exit 1; }
fi

# --------------------------------------------------------------------------- #
# Entorno virtual y dependencias
# --------------------------------------------------------------------------- #
if [ ! -d "$RAIZ/.venv" ]; then
    echo "==> Creando entorno virtual"
    python3 -m venv "$RAIZ/.venv"
fi

echo "==> Instalando dependencias"
"$RAIZ/.venv/bin/pip" install --quiet --upgrade pip
"$RAIZ/.venv/bin/pip" install --quiet -r "$RAIZ/requirements.txt"

# --------------------------------------------------------------------------- #
# Carpetas de datos
# --------------------------------------------------------------------------- #
mkdir -p "$DATOS/datos" "$DATOS/subidas"
echo "==> Carpetas de datos listas"

# --------------------------------------------------------------------------- #
# Comprobacion
# --------------------------------------------------------------------------- #
echo "==> Comprobando que la aplicacion carga"
CHECKLIST_DATOS="$DATOS/datos" \
CHECKLIST_SUBIDAS="$DATOS/subidas" \
"$RAIZ/.venv/bin/python" - <<'PY'
import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
import bd
import server  # noqa: F401

print("    modulos: ok")
print("    base   :", bd.RUTA_BD)
print("    plantilla:", server.plantilla() or "FALTA (subela a $CHECKLIST_PLANTILLA)")
PY

echo
echo "Listo. Siguiente paso:"
echo
echo "  1. Sube la plantilla de formato:"
echo "       scp \"Revision Pagos a Proveedores.xlsx\" $(hostname):$DATOS/plantilla.xlsx"
echo
echo "  2. Instala el servicio (revisa antes el usuario y las rutas):"
echo "       sudo cp despliegue/checklist.service /etc/systemd/system/"
echo "       sudo systemctl daemon-reload"
echo "       sudo systemctl enable --now checklist"
echo
echo "  3. Prueba sin servicio, si quieres verlo antes:"
echo "       CHECKLIST_DATOS=$DATOS/datos CHECKLIST_SUBIDAS=$DATOS/subidas \\"
echo "         CHECKLIST_PLANTILLA=$DATOS/plantilla.xlsx \\"
echo "         .venv/bin/python app/server.py"
