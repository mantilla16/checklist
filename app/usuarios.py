"""Alta y mantenimiento de usuarios desde la linea de comandos.

No hay pantalla de registro en la aplicacion a proposito: quien puede crear
cuentas es quien tiene acceso al servidor.

    python app/usuarios.py listar
    python app/usuarios.py crear ana --nombre "Ana Torres"
    python app/usuarios.py clave ana
    python app/usuarios.py desactivar ana
    python app/usuarios.py activar ana

La contrasena se pide por teclado y no se ve al escribirla; no se pasa como
argumento para que no quede en el historial del shell.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autenticacion as auth
import bd


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
    print(f"{'usuario':<16} {'nombre':<24} {'estado':<12} ultimo acceso")
    for f in filas:
        estado = "activo" if f["activo"] else "desactivado"
        if f["bloqueado_hasta"]:
            estado = "bloqueado"
        print(f"{f['usuario']:<16} {(f['nombre'] or '-'):<24} {estado:<12} "
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

    args = partes.parse_args()
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
    except ValueError as exc:
        raise SystemExit(f"Error: {exc}")

    print(f"Base de datos: {bd.RUTA_BD}")


if __name__ == "__main__":
    main()
