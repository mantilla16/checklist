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
#
# Se comprueba la biblioteca, no el nombre del paquete: en Ubuntu 24.04
# libglib2.0-0 pasa a llamarse libglib2.0-0t64 (transicion de time_t), asi que
# `dpkg -s libglib2.0-0` falla aunque la biblioteca este instalada.
# --------------------------------------------------------------------------- #
faltantes=()

comprobar() {   # comprobar <biblioteca> <paquete> <paquete en 24.04>
    ldconfig -p 2>/dev/null | grep -q "$1" && return 0
    if apt-cache show "$3" >/dev/null 2>&1; then
        faltantes+=("$3")
    else
        faltantes+=("$2")
    fi
}

comprobar libGL.so.1       libgl1       libgl1
comprobar libglib-2.0.so.0 libglib2.0-0 libglib2.0-0t64

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

# Sin --quiet: son unos 300-400 MB (onnxruntime, opencv, pypdfium2) y sin la
# barra de progreso el paso parece colgado durante varios minutos.
echo "==> Instalando dependencias (unos 300-400 MB, puede tardar varios minutos)"
"$RAIZ/.venv/bin/pip" install --quiet --upgrade pip
"$RAIZ/.venv/bin/pip" install -r "$RAIZ/requirements.txt"

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
