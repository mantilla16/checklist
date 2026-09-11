"""Alta y mantenimiento de usuarios desde la linea de comandos.

No hay pantalla de registro en la aplicacion a proposito: quien puede crear
cuentas es quien tiene acceso al servidor.

    python app/usuarios.py listar
    python app/usuarios.py crear ana --nombre "Ana Torres"
    python app/usuarios.py clave ana
    python app/usuarios.py desactivar ana
    python app/usuarios.py activar ana
    python app/usuarios.py admin ana
    python app/usuarios.py admin ana --quitar
    python app/usuarios.py desbloquear ana
    python app/usuarios.py borrar ana

La gestion tambien esta en la aplicacion, en el boton "Usuarios" de la barra,
para quien tenga permiso de administrador. Esta linea de comandos hace falta
igual: es la unica forma de crear la primera cuenta y de recuperar el acceso
si nadie puede entrar.

La contrasena se pide por teclado y no se ve al escribirla; no se pasa como
argumento para que no quede en el historial del shell.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

UNIDAD = Path("/etc/systemd/system/checklist.service")


def heredar_del_servicio() -> str:
    """Toma las rutas del archivo de systemd si no vienen en el entorno.

    Sin esto la trampa es fea: el servicio guarda en CHECKLIST_DATOS y la
    consola, sin esa variable, abre la base local por omision. El usuario se
    crea en una base que nadie lee y la aplicacion sigue diciendo que no hay
    cuentas.
    """
    if os.environ.get("CHECKLIST_DATOS") or not UNIDAD.exists():
        return ""
    try:
        texto = UNIDAD.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for clave, valor in re.findall(r"^Environment=(CHECKLIST_\w+)=(.+)$",
                                   texto, re.MULTILINE):
        os.environ.setdefault(clave, valor.strip().strip('"'))
    return str(UNIDAD) if os.environ.get("CHECKLIST_DATOS") else ""


_HEREDADO = heredar_del_servicio()

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autenticacion as auth   # noqa: E402  (despues de fijar el entorno)
import bd                      # noqa: E402


def pedir_clave() -> str:
    clave = getpass.getpass("Contrasena: ")
    if len(clave) < auth.CLAVE_MINIMA:
        raise SystemExit(f"La contrasena debe tener al menos {auth.CLAVE_MINIMA} caracteres.")
    if clave != getpass.getpass("Repitela: "):
        raise SystemExit("Las dos contrasenas no coinciden.")
    return clave


def mostrar() -> None:
    filas = auth.listar()
    if not filas:
        print("No hay usuarios. Crea el primero con:  python app/usuarios.py crear <usuario>")
        return
    print(f"{'usuario':<16} {'nombre':<22} {'estado':<24} ultimo acceso")
    for f in filas:
        estado = "activo" if f["activo"] else "desactivado"
        if f["bloqueado"]:
            estado += ", bloqueado"
        if f["admin"]:
            estado += ", admin"
        print(f"{f['usuario']:<16} {(f['nombre'] or '-'):<22} {estado:<24} "
              f"{f['ultimo_acceso'] or 'nunca'}")


def main() -> None:
    partes = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    ordenes = partes.add_subparsers(dest="orden", required=True)

    ordenes.add_parser("listar", help="muestra los usuarios")

    p = ordenes.add_parser("crear", help="crea un usuario")
    p.add_argument("usuario")
    p.add_argument("--nombre", default="", help="nombre para mostrar")

    p = ordenes.add_parser("clave", help="cambia la contrasena")
    p.add_argument("usuario")

    p = ordenes.add_parser("desactivar", help="le quita el acceso sin borrarlo")
    p.add_argument("usuario")

    p = ordenes.add_parser("activar", help="le devuelve el acceso")
    p.add_argument("usuario")

    p = ordenes.add_parser("admin", help="da o quita el permiso de administrar")
    p.add_argument("usuario")
    p.add_argument("--quitar", action="store_true", help="se lo retira")

    p = ordenes.add_parser("desbloquear",
                           help="levanta el bloqueo por intentos fallidos")
    p.add_argument("usuario")

    p = ordenes.add_parser("borrar", help="elimina la cuenta")
    p.add_argument("usuario")

    args = partes.parse_args()
    if _HEREDADO:
        print(f"Rutas tomadas de {_HEREDADO}")
    bd.inicializar()

    try:
        if args.orden == "listar":
            mostrar()
        elif args.orden == "crear":
            auth.crear(args.usuario, pedir_clave(), args.nombre)
            print(f"Usuario '{auth.normalizar(args.usuario)}' creado.")
        elif args.orden == "clave":
            auth.cambiar_clave(args.usuario, pedir_clave())
            print("Contrasena cambiada.")
        elif args.orden in ("activar", "desactivar"):
            auth.activar(args.usuario, args.orden == "activar")
            print(f"Usuario '{auth.normalizar(args.usuario)}' {args.orden.rstrip('r')}do.")
        elif args.orden == "admin":
            auth.cambiar_admin(args.usuario, not args.quitar)
            print(f"Permiso de administrar "
                  f"{'retirado a' if args.quitar else 'concedido a'} "
                  f"'{auth.normalizar(args.usuario)}'.")
        elif args.orden == "desbloquear":
            auth.desbloquear(args.usuario)
            print(f"Usuario '{auth.normalizar(args.usuario)}' desbloqueado.")
        elif args.orden == "borrar":
            confirmar = input(f"Borrar '{auth.normalizar(args.usuario)}'? [s/N] ")
            if confirmar.strip().lower() != "s":
                raise SystemExit("Cancelado.")
            auth.borrar(args.usuario)
            print(f"Usuario '{auth.normalizar(args.usuario)}' borrado.")
    except ValueError as exc:
        raise SystemExit(f"Error: {exc}")

    print(f"Base de datos: {bd.RUTA_BD}")


if __name__ == "__main__":
    main()
